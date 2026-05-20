#!/usr/bin/env python3
"""Phase 5.5.c — Dédup vectorisée des questions pending vs banque + intra-pending.

Pour chaque question pending acceptée (status `verified_by_judge` ou
`unverified`), calcule l'embedding du titre puis :

  1. cosine similarity vs **tous les titres existants dans `questions.json`**
     (via `bank_embeddings.npz`) — matrice (n_pending × n_bank)
  2. cosine similarity **intra-pending** — matrice triangulaire (n_pending × n_pending)

Si une question pending a max-sim > seuil (`--threshold`, défaut 0.92), elle
est marquée comme doublon :
  - vs banque  → `status = "flagged"`, `dedup_against = qid_banque`
  - vs pending → `status = "flagged"`, `dedup_against = qid_pending_premier`

Le log détaillé est écrit dans `data/dedup_log.jsonl`.

Tout en numpy, pas d'appel LLM. ~5-10 sec sur 3000 questions pending.

Usage :
  python3 -m scripts.dedupe_pending --dry-run                    # diagnostic
  python3 -m scripts.dedupe_pending                              # applique (update JSONL en place)
  python3 -m scripts.dedupe_pending --threshold 0.95             # seuil custom
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bank_embeddings  # noqa: E402

PENDING_DIR = ROOT / "data" / "generated_pending"
DEDUP_LOG = ROOT / "data" / "dedup_log.jsonl"
QUESTIONS_FILE = ROOT / "questions.json"
BANK_EMB_NPZ = ROOT / "data" / "bank_embeddings.npz"

DEFAULT_THRESHOLD = 0.92


# --- Chargement -------------------------------------------------------------


def load_pending_acceptable() -> list[tuple[Path, dict]]:
    """Liste les (file_path, question) pour les pending avec status acceptable.

    Acceptable = verified_by_judge OU unverified. On exclut explicitement
    flagged / déjà dedup, pour qu'un re-run soit idempotent.
    """
    out: list[tuple[Path, dict]] = []
    pending_dir = PENDING_DIR.resolve()
    if not pending_dir.exists():
        return out
    for p in sorted(pending_dir.glob("*.jsonl")):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            q = json.loads(line)
            if q.get("status") in ("verified_by_judge", "unverified"):
                out.append((p.resolve(), q))
    return out


def load_bank_embeddings_matrix() -> tuple[np.ndarray, np.ndarray]:
    """Retourne (qids_int64, matrix_float32_L2norm) depuis le cache npz."""
    if not BANK_EMB_NPZ.exists():
        raise SystemExit(
            f"❌ {BANK_EMB_NPZ} introuvable — l'app doit avoir tourné une fois "
            "pour warmup le cache (build_bank_vector_index)."
        )
    data = np.load(BANK_EMB_NPZ)
    ids = data["ids"].astype(np.int64, copy=False)
    mat = data["matrix"].astype(np.float32, copy=False)
    return ids, mat


# --- Dédup ------------------------------------------------------------------


def cosine_max_against_bank(
    pending_vecs: np.ndarray, bank_mat: np.ndarray, bank_ids: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Pour chaque pending (n_p, d) vs banque (n_b, d) — retourne :
       (max_scores[n_p], argmax_qids[n_p]).
    """
    sims = pending_vecs @ bank_mat.T  # (n_p, n_b)
    arg = np.argmax(sims, axis=1)
    sc = sims[np.arange(sims.shape[0]), arg]
    return sc, bank_ids[arg]


def cosine_triangular_intra(pending_vecs: np.ndarray) -> np.ndarray:
    """Retourne la matrice de sim (n, n), triangulaire stricte supérieure mise à -1
    pour permettre argmax sur chaque ligne sans tomber sur la diag ni sur les
    indices antérieurs.

    Logique : pour i, on regarde sim contre j > i. Si on a un match, c'est i qui
    sera marqué doublon (pas j), car j est traité comme "premier rencontré".
    Pour ça, on regarde sim contre j < i (strict).
    """
    n = pending_vecs.shape[0]
    sims = pending_vecs @ pending_vecs.T  # (n, n)
    # Zone à ignorer : j >= i  (diag + supérieur). On la met à -inf.
    mask = np.triu(np.ones((n, n), dtype=bool), k=0)
    sims[mask] = -1.0
    return sims


def run_dedup(threshold: float, dry_run: bool) -> int:
    print(f"→ Seuil cosine : {threshold}")
    print("→ Chargement pending acceptable…")
    pending = load_pending_acceptable()
    if not pending:
        print(f"❌ Aucune pending acceptable dans {PENDING_DIR}", file=sys.stderr)
        return 1
    n = len(pending)
    print(f"  {n} questions à vérifier")

    titles = [q["title"] for _, q in pending]
    qids = np.array([q["id"] for _, q in pending], dtype=np.int64)

    print("→ Embedding des titres pending (fastembed local)…")
    pending_mat = bank_embeddings._embed_texts(titles)
    if pending_mat is None:
        print("❌ embeddings indisponibles (fastembed absent ?)", file=sys.stderr)
        return 1
    # _embed_texts retourne déjà L2-normalisé
    print(f"  shape: {pending_mat.shape}")

    print("→ Chargement bank_embeddings.npz…")
    bank_ids, bank_mat = load_bank_embeddings_matrix()
    print(f"  banque : {bank_ids.size} embeddings")

    print("→ Cosine sim pending × banque…")
    sc_bank, arg_bank_qids = cosine_max_against_bank(pending_mat, bank_mat, bank_ids)
    n_dup_bank = int((sc_bank > threshold).sum())
    print(f"  doublons vs banque : {n_dup_bank}/{n} (max sim > {threshold})")

    print("→ Cosine sim intra-pending…")
    sims_intra = cosine_triangular_intra(pending_mat)
    arg_intra_idx = np.argmax(sims_intra, axis=1)
    sc_intra = sims_intra[np.arange(n), arg_intra_idx]
    n_dup_intra = int((sc_intra > threshold).sum())
    print(f"  doublons intra-pending : {n_dup_intra}/{n} (max sim > {threshold})")

    # Décision : prend la pire des deux sources (vs banque vs intra)
    decisions: list[dict] = []
    for i in range(n):
        flag_bank = bool(sc_bank[i] > threshold)
        flag_intra = bool(sc_intra[i] > threshold)
        if flag_bank or flag_intra:
            # Source du doublon : la plus forte sim
            if sc_bank[i] >= sc_intra[i]:
                src = "bank"
                against = int(arg_bank_qids[i])
                sim = float(sc_bank[i])
            else:
                src = "pending"
                against = int(qids[int(arg_intra_idx[i])])
                sim = float(sc_intra[i])
            decisions.append({
                "qid": int(qids[i]),
                "title": titles[i][:120],
                "source_doublon": src,
                "against_qid": against,
                "similarity": round(sim, 4),
            })

    print(f"\n→ Doublons à flagger : {len(decisions)} ({len(decisions)/n*100:.1f} %)")
    if decisions[:5]:
        print("  5 premiers :")
        for d in decisions[:5]:
            print(f"    qid={d['qid']:>5} sim={d['similarity']:.3f}  "
                  f"vs {d['source_doublon']}:{d['against_qid']:>5}  "
                  f"| {d['title'][:60]}")

    if dry_run:
        print("\n(dry-run — aucune écriture)")
        return 0

    # --- Apply : update JSONL + log ---
    print("\n→ Application des décisions…")
    qid_to_decision = {d["qid"]: d for d in decisions}
    touched_files = sorted(set(str(p) for p, _ in pending))
    n_updated = 0
    for f in touched_files:
        path = Path(f)
        qs = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
        for q in qs:
            d = qid_to_decision.get(q.get("id"))
            if not d:
                continue
            q["status"] = "flagged"
            q["dedup"] = {
                "against_qid": d["against_qid"],
                "source": d["source_doublon"],
                "similarity": d["similarity"],
                "threshold": threshold,
                "flagged_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            n_updated += 1
        tmp = path.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as fp:
            for q in qs:
                fp.write(json.dumps(q, ensure_ascii=False) + "\n")
        tmp.replace(path)
    print(f"  {n_updated} questions taggées flagged dans {len(touched_files)} fichiers")

    if decisions:
        DEDUP_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(DEDUP_LOG, "a", encoding="utf-8") as f:
            for d in decisions:
                d["threshold"] = threshold
                d["logged_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
        print(f"  log → {DEDUP_LOG} (+{len(decisions)} entrées)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help=f"Seuil cosine (défaut : {DEFAULT_THRESHOLD})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Diagnostic seul, aucune écriture")
    args = parser.parse_args()
    return run_dedup(args.threshold, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
