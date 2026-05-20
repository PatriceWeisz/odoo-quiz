#!/usr/bin/env python3
"""Phase 5.6 — Insertion atomique des questions pending dans questions.json.

Insère dans la banque toutes les questions pending avec status
`verified_by_judge` ou `unverified`. Les autres (`flagged` : judge reject
ou dedup duplicate) sont ignorées.

Sécurité :
  - sanity check avant écriture : `validate_generated_question` doit retourner []
  - ré-attribution séquentielle des qids/aids pour éviter toute collision
    (les pendings ont des qids/aids attribués sur la base de la banque AVANT
    le full run ; ré-attribuer garantit l'unicité)
  - backup horodaté de questions.json AVANT écriture
  - écriture atomique (.tmp + rename) via la même méthode que app.py
  - invalidation du cache embeddings (Phase 5.8 implicite)
  - log dans data/insertion_log.jsonl (qid_old → qid_new, batch_id, etc.)

Idempotent : si on relance après une insertion, le filtre sur les pendings
n'inclut PAS les questions déjà insérées (on les détecte via un champ
`inserted_at` qu'on ajoute à chaque question pending insérée).

Usage :
  python3 -m scripts.insert_pending_questions --dry-run
  python3 -m scripts.insert_pending_questions
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.question_schema import validate_generated_question  # noqa: E402

PENDING_DIR = ROOT / "data" / "generated_pending"
QUESTIONS_FILE = ROOT / "questions.json"
INSERTION_LOG = ROOT / "data" / "insertion_log.jsonl"


# --- Helpers ---------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _max_qid(bank_qs: list[dict]) -> int:
    return max((int(q.get("id") or 0) for q in bank_qs), default=0)


def _max_aid(bank_qs: list[dict]) -> int:
    out = 0
    for q in bank_qs:
        for a in q.get("answers") or []:
            aid = a.get("id")
            if isinstance(aid, int) and aid > out:
                out = aid
    return out


def load_pending_to_insert() -> list[tuple[Path, dict]]:
    """Liste (file, q) pour les pendings à insérer.

    Filtre : status in (verified_by_judge, unverified) ET `inserted_at` absent.
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
            if q.get("inserted_at"):
                continue  # déjà inséré
            if q.get("status") in ("verified_by_judge", "unverified"):
                out.append((p.resolve(), q))
    return out


def load_bank() -> dict:
    if not QUESTIONS_FILE.exists():
        raise SystemExit(f"❌ {QUESTIONS_FILE} introuvable")
    with open(QUESTIONS_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_bank_atomic(bank: dict) -> None:
    """Écriture atomique + invalidate cache (cf. _save_questions_file_raw)."""
    tmp = QUESTIONS_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(bank, f, ensure_ascii=False, indent=2)
    tmp.replace(QUESTIONS_FILE)
    try:
        from bank_embeddings import invalidate_embedding_cache  # noqa: E402
        invalidate_embedding_cache()
    except Exception as e:
        print(f"⚠️  invalidate_embedding_cache : {e}", file=sys.stderr)


def backup_bank() -> Path:
    """Copie horodatée AVANT écriture. Briefing : règle de pilotage."""
    backup = QUESTIONS_FILE.parent / f"questions.json.bak.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    if QUESTIONS_FILE.exists():
        backup.write_bytes(QUESTIONS_FILE.read_bytes())
    return backup


def reassign_ids(q: dict, new_qid: int, aid_start: int) -> tuple[dict, int]:
    """Renumérote q.id et q.answers[*].id. Retourne (q, next_aid)."""
    old_qid = q.get("id")
    q["id"] = int(new_qid)
    q["_qid_before_insertion"] = old_qid
    aid = aid_start
    new_answers = []
    for a in q.get("answers") or []:
        new_a = dict(a)
        new_a["id"] = aid
        aid += 1
        new_answers.append(new_a)
    q["answers"] = new_answers
    return q, aid


def update_pending_file_with_insertion(file_path: Path, qid_old_to_new: dict[int, dict]) -> None:
    """Marque les questions pending comme insérées (inserted_at + new_qid)."""
    qs = [json.loads(line) for line in open(file_path, encoding="utf-8") if line.strip()]
    for q in qs:
        old_qid = q.get("id")
        if old_qid in qid_old_to_new:
            info = qid_old_to_new[old_qid]
            q["inserted_at"] = info["inserted_at"]
            q["inserted_as_qid"] = info["new_qid"]
    tmp = file_path.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for q in qs:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")
    tmp.replace(file_path)


# --- Main ---


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="Affiche le plan sans écrire questions.json")
    args = parser.parse_args()

    print("→ Chargement banque…")
    bank = load_bank()
    bank_qs = bank.get("questions") or []
    print(f"  Banque actuelle : {len(bank_qs)} questions")

    print("→ Chargement pendings à insérer…")
    pending = load_pending_to_insert()
    print(f"  Pendings éligibles : {len(pending)}")
    if not pending:
        print("→ Rien à insérer (idempotence).")
        return 0

    # Compute next qid/aid
    next_qid = _max_qid(bank_qs) + 1
    next_aid = _max_aid(bank_qs) + 1
    print(f"  next_qid de départ : {next_qid}")
    print(f"  next_aid de départ : {next_aid}")

    # Sanity check + assemble
    print("→ Validation + ré-attribution IDs…")
    to_insert: list[dict] = []
    n_invalid = 0
    invalid_details: list[str] = []
    per_file_qid_mapping: dict[Path, dict[int, dict]] = {}

    for file_path, q in pending:
        errs = validate_generated_question(q)
        if errs:
            n_invalid += 1
            invalid_details.append(f"qid={q.get('id')}: {'; '.join(errs)}")
            continue
        old_qid = q["id"]
        q_renum, next_aid_new = reassign_ids(dict(q), next_qid, next_aid)
        to_insert.append(q_renum)
        per_file_qid_mapping.setdefault(file_path, {})[old_qid] = {
            "new_qid": next_qid,
            "inserted_at": _now_iso(),
        }
        next_qid += 1
        next_aid = next_aid_new

    print(f"  Valides    : {len(to_insert)}")
    print(f"  Invalides  : {n_invalid}")
    if invalid_details[:5]:
        print(f"  5 premières erreurs :")
        for d in invalid_details[:5]:
            print(f"    - {d}")

    if not to_insert:
        print("❌ Aucune question valide à insérer.", file=sys.stderr)
        return 1

    # Stats
    from collections import Counter
    statuses = Counter(q.get("status") for q in to_insert)
    tiers = Counter(q.get("tier") for q in to_insert)
    versions = Counter(q.get("target_version") for q in to_insert)
    print(f"\n  Statuses   : {dict(statuses)}")
    print(f"  Tiers      : {dict(tiers)}")
    print(f"  Versions   : {dict(versions)}")

    print(f"\n→ Banque finale aura : {len(bank_qs) + len(to_insert)} questions "
          f"({len(bank_qs)} actuelles + {len(to_insert)} nouvelles)")
    print(f"  qid range nouveaux : [{to_insert[0]['id']}, {to_insert[-1]['id']}]")
    print(f"  aid max nouveau    : {next_aid - 1}")

    if args.dry_run:
        print("\n(dry-run — aucune écriture)")
        return 0

    # Backup
    print("\n→ Backup horodaté de questions.json…")
    backup = backup_bank()
    print(f"  {backup}")

    # Insertion
    print("→ Insertion dans banque (mémoire)…")
    bank["questions"] = bank_qs + to_insert

    # Save atomic
    print("→ Save atomic + invalidate embedding cache…")
    save_bank_atomic(bank)
    print(f"  {QUESTIONS_FILE} mis à jour : {len(bank['questions'])} questions au total")

    # Update pending files (inserted_at)
    print("→ Marquage des pendings comme insérés…")
    for file_path, mapping in per_file_qid_mapping.items():
        update_pending_file_with_insertion(file_path, mapping)
    print(f"  {len(per_file_qid_mapping)} fichiers pending mis à jour")

    # Log
    print("→ Écriture insertion_log.jsonl…")
    INSERTION_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(INSERTION_LOG, "a", encoding="utf-8") as f:
        for q in to_insert:
            f.write(json.dumps({
                "qid_new": q["id"],
                "qid_old": q.get("_qid_before_insertion"),
                "module": q.get("module"),
                "tier": q.get("tier"),
                "version": q.get("target_version"),
                "status": q.get("status"),
                "inserted_at": _now_iso(),
            }, ensure_ascii=False) + "\n")
    print(f"  +{len(to_insert)} entrées dans {INSERTION_LOG}")

    print(f"\n✓ Insertion atomique terminée.")
    print(f"  Backup : {backup}")
    print(f"  Banque : {len(bank['questions'])} questions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
