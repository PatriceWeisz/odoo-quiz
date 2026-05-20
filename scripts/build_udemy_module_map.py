#!/usr/bin/env python3
"""Phase 5.5.d — Inférence module pour les questions Udemy (one-shot).

Pour chaque question Udemy de la banque (`correct_answer_source == "udemy"`),
trouve le chunk doc Odoo le plus proche par similarité cosinus, puis en extrait
le module (libellé STUDY_MODULES). Le résultat est caché dans
`data/udemy_modules.json` et exploité par `generate_questions.pick_few_shot`
pour faire du few-shot rotatif par module.

Tout numpy, pas d'appel LLM. Sur ~640 titres × 5217 chunks, ~3 sec en local.

Sortie :
  data/udemy_modules.json — { "<qid>": { "module": "...", "score": 0.xx,
                                          "chunk_id": "...", "url": "..." } }
  data/udemy_modules_report.md — répartition par module + tier (humain)

Usage :
  python3 -m scripts.build_udemy_module_map
  python3 -m scripts.build_udemy_module_map --min-score 0.30
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.odoo_docs_rag import db_path  # noqa: E402
from app.study_modules import (  # noqa: E402
    ALL_TIERS,
    STUDY_MODULES,
    tier_of,
    url_paths_for,
)

QUESTIONS_FILE = ROOT / "questions.json"
BANK_EMB_NPZ = ROOT / "data" / "bank_embeddings.npz"
OUT_JSON = ROOT / "data" / "udemy_modules.json"
OUT_MD = ROOT / "data" / "udemy_modules_report.md"

# Seuil minimum de cosine pour valider une inférence. En dessous, la question
# est considérée comme "hors-scope" (probable nettoyage à la main).
DEFAULT_MIN_SCORE = 0.25

# Match URL → module : on capture ce qui suit /applications/ jusqu'à .html ou
# le prochain '/' éventuel. Couvre les deux formes : /applications/sales.html et
# /applications/sales/crm/lead_acquisition.html
_RE_APPS_PATH = re.compile(r"/applications/(.+?)(?:\.html|/|$)")


def _applications_path(url: str) -> str | None:
    """Extrait le path applicatif d'une URL doc Odoo (sans .html final)."""
    m = re.search(r"/applications/([^?#]+?)(?:\.html|$)", url or "")
    if not m:
        return None
    return m.group(1).strip("/")


def build_reverse_path_index() -> list[tuple[str, str]]:
    """Liste (path, module) triée par longueur de path DÉCROISSANTE — premier
    préfixe matchant gagne. Garantit que `sales/point_of_sale` est essayé avant
    `sales`.
    """
    pairs: list[tuple[str, str]] = []
    for tier in ALL_TIERS:
        for module in STUDY_MODULES[tier]:
            for path in url_paths_for(module):
                pairs.append((path.strip("/"), module))
    # Tri par longueur décroissante (plus long = plus spécifique)
    pairs.sort(key=lambda x: -len(x[0]))
    return pairs


def chunk_url_to_module(url: str, reverse: list[tuple[str, str]]) -> str | None:
    """Mappe une URL chunk → module via le préfixe le plus long."""
    apath = _applications_path(url)
    if not apath:
        return None
    for path, module in reverse:
        if apath == path or apath.startswith(path + "/"):
            return module
    return None


def load_chunk_embeddings(conn: sqlite3.Connection) -> tuple[list[str], list[str], np.ndarray]:
    """Charge tous les chunks (id, url, embedding) en mémoire.

    Retourne (chunk_ids, urls, matrix) — matrix L2-normalisée float32 (n, 384).
    """
    rows = conn.execute("SELECT chunk_id, url, embedding FROM chunks").fetchall()
    chunk_ids: list[str] = []
    urls: list[str] = []
    vecs: list[np.ndarray] = []
    for cid, url, blob in rows:
        if not blob:
            continue
        v = np.frombuffer(blob, dtype=np.float32)
        if v.size != 384:
            # défensif : si la dim diffère, skip ce chunk
            continue
        chunk_ids.append(cid)
        urls.append(url)
        vecs.append(v)
    if not vecs:
        return [], [], np.zeros((0, 384), dtype=np.float32)
    mat = np.stack(vecs, axis=0).astype(np.float32, copy=False)
    # L2-normalize en place
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms = np.maximum(norms, 1e-9)
    mat = mat / norms
    return chunk_ids, urls, mat


def load_bank_embeddings() -> tuple[np.ndarray, np.ndarray]:
    """Retourne (ids: int64 [n], matrix: float32 [n, 384] L2-normalisé)."""
    if not BANK_EMB_NPZ.exists():
        raise SystemExit(
            f"❌ {BANK_EMB_NPZ} introuvable — lance d'abord l'app pour générer "
            "le cache (ou rebuild via bank_embeddings.warmup_bank_embeddings)."
        )
    data = np.load(BANK_EMB_NPZ)
    ids = data["ids"].astype(np.int64, copy=False)
    mat = data["matrix"].astype(np.float32, copy=False)
    # Le cache stocke déjà des vecteurs L2-normalisés (cf. _embed_texts).
    return ids, mat


def load_udemy_questions() -> list[dict]:
    if not QUESTIONS_FILE.exists():
        raise SystemExit(f"❌ {QUESTIONS_FILE} introuvable")
    data = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))
    return [
        q for q in data.get("questions") or []
        if isinstance(q, dict)
        and q.get("correct_answer_source") == "udemy"
        and isinstance(q.get("id"), int)
        and q.get("id") > 0  # exclut les ids négatifs (placeholders système)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--min-score", type=float, default=DEFAULT_MIN_SCORE,
        help=f"Cosine sim minimum pour valider l'inférence (défaut : {DEFAULT_MIN_SCORE})",
    )
    parser.add_argument("--output-json", type=Path, default=OUT_JSON)
    parser.add_argument("--output-md", type=Path, default=OUT_MD)
    args = parser.parse_args()

    print("→ Chargement des questions Udemy…")
    udemy = load_udemy_questions()
    udemy_by_id = {q["id"]: q for q in udemy}
    print(f"  {len(udemy)} questions Udemy retenues (ids > 0)")

    print("→ Chargement bank_embeddings.npz…")
    bank_ids, bank_mat = load_bank_embeddings()
    print(f"  {bank_ids.size} embeddings (dim {bank_mat.shape[1]})")

    # Filtrer le bank_emb pour ne garder que les ids Udemy
    udemy_id_set = set(udemy_by_id.keys())
    mask = np.array([int(i) in udemy_id_set for i in bank_ids], dtype=bool)
    sel_ids = bank_ids[mask]
    sel_mat = bank_mat[mask]
    missing = udemy_id_set - {int(i) for i in sel_ids}
    if missing:
        print(f"  ⚠️  {len(missing)} ids Udemy absents du cache embeddings — "
              "ces questions seront ignorées (warmup le cache et relance).",
              file=sys.stderr)
    print(f"  {sel_ids.size} embeddings Udemy utilisables")

    if sel_ids.size == 0:
        print("❌ Aucun embedding Udemy disponible.", file=sys.stderr)
        return 1

    print("→ Chargement chunks embeddings depuis SQLite…")
    conn = sqlite3.connect(db_path())
    chunk_ids, urls, chunk_mat = load_chunk_embeddings(conn)
    conn.close()
    print(f"  {len(chunk_ids)} chunks (dim {chunk_mat.shape[1] if chunk_mat.size else '?'})")

    if not chunk_ids:
        print("❌ Aucun chunk embedding.", file=sys.stderr)
        return 1

    print("→ Matmul cosine sim (Udemy × chunks)…")
    # sel_mat : (n_q, 384) ; chunk_mat.T : (384, n_chunks)
    sims = sel_mat @ chunk_mat.T  # (n_q, n_chunks)
    top_idx = np.argmax(sims, axis=1)  # (n_q,)
    top_score = sims[np.arange(sims.shape[0]), top_idx]  # (n_q,)
    print(f"  done — distribution scores : "
          f"p10={np.percentile(top_score, 10):.2f} "
          f"p50={np.percentile(top_score, 50):.2f} "
          f"p90={np.percentile(top_score, 90):.2f}")

    print("→ Mapping URL → module (préfixe le plus long)…")
    reverse = build_reverse_path_index()

    out: dict[str, dict] = {}
    stats_below_threshold = 0
    stats_no_module = 0
    module_counter: Counter[str] = Counter()
    tier_counter: Counter[str] = Counter()
    no_module_urls: list[str] = []

    for i, qid in enumerate(sel_ids):
        qid_int = int(qid)
        idx = int(top_idx[i])
        score = float(top_score[i])
        url = urls[idx]
        chunk_id = chunk_ids[idx]
        module = chunk_url_to_module(url, reverse)

        record = {
            "module": module,
            "score": round(score, 4),
            "chunk_id": chunk_id,
            "url": url,
            "title": udemy_by_id[qid_int]["title"][:120],
        }
        if score < args.min_score:
            stats_below_threshold += 1
            record["below_threshold"] = True
        if module is None:
            stats_no_module += 1
            no_module_urls.append(url)
        else:
            module_counter[module] += 1
            t = tier_of(module)
            if t:
                tier_counter[t] += 1

        out[str(qid_int)] = record

    # --- Écriture JSON ---
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(f"\n→ JSON : {args.output_json} ({args.output_json.stat().st_size:,} bytes)")

    # --- Écriture MD ---
    lines = [
        "# Inférence module pour questions Udemy",
        "",
        f"- Questions Udemy traitées : **{sel_ids.size}**",
        f"- Score moyen top-1 : {float(top_score.mean()):.3f}",
        f"- Sous le seuil ({args.min_score}) : {stats_below_threshold} "
        f"({stats_below_threshold/sel_ids.size*100:.1f} %)",
        f"- Sans module mapping : {stats_no_module}",
        "",
        "## Distribution par tier",
        "",
        "| Tier | n questions |",
        "|---|---:|",
    ]
    for t in ALL_TIERS:
        lines.append(f"| `{t}` | {tier_counter.get(t, 0)} |")
    unknown = sel_ids.size - sum(tier_counter.values())
    lines.append(f"| (hors-scope / `?`) | {unknown} |")
    lines += [
        "",
        "## Distribution par module",
        "",
        "| Module | tier | n questions |",
        "|---|---|---:|",
    ]
    for module, n in module_counter.most_common():
        lines.append(f"| `{module}` | {tier_of(module) or '?'} | {n} |")

    if no_module_urls:
        lines += [
            "",
            f"## URLs sans mapping module ({len(no_module_urls)})",
            "",
            "10 premières :",
            "",
        ]
        for u in sorted(set(no_module_urls))[:10]:
            lines.append(f"- {u}")

    args.output_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"→ MD   : {args.output_md}")
    print(f"\n→ Top modules :")
    for module, n in module_counter.most_common(8):
        print(f"    {module:40} ({tier_of(module) or '?':5})  {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
