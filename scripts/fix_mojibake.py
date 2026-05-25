#!/usr/bin/env python3
"""
Corrige le mojibake (UTF-8 mal décodé : « â€™ », « â€œ », « Ã© », …) dans tous les
champs texte de questions.json, via ftfy.

Dry-run par défaut (montre l'ampleur + des exemples avant/après) ; --apply écrit
le fichier (sauvegarde horodatée automatique). Idempotent.

Usage :
    python3 -m scripts.fix_mojibake               # dry-run (n'écrit rien)
    python3 -m scripts.fix_mojibake --apply       # corrige + backup
    python3 -m scripts.fix_mojibake --apply --file questions.json
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Clés à NE PAS toucher (identifiants / URLs / horodatages : pas de mojibake utile).
SKIP_KEYS = {"id", "source_chunk_id", "source_chunk_url", "question_image",
             "created_at", "_qid_before_insertion", "status", "type",
             "correct_answer_source", "source", "target_version", "tier"}


def _fix_walk(obj, fixer, changes: list, path: str = ""):
    """Parcourt récursivement ; applique `fixer` aux chaînes (hors SKIP_KEYS)."""
    if isinstance(obj, dict):
        return {k: (v if k in SKIP_KEYS else _fix_walk(v, fixer, changes, f"{path}.{k}"))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fix_walk(v, fixer, changes, f"{path}[{i}]") for i, v in enumerate(obj)]
    if isinstance(obj, str):
        fixed = fixer(obj)
        if fixed != obj:
            changes.append((path, obj, fixed))
        return fixed
    return obj


def main() -> None:
    ap = argparse.ArgumentParser(description="Corrige le mojibake de questions.json (ftfy).")
    ap.add_argument("--file", default=str(ROOT / "questions.json"))
    ap.add_argument("--apply", action="store_true", help="écrit le fichier (sinon dry-run)")
    ap.add_argument("--show", type=int, default=25, help="nombre d'exemples avant/après affichés")
    args = ap.parse_args()

    try:
        from ftfy import fix_encoding
    except ImportError:
        sys.exit("❌ ftfy absent. Installer : pip install ftfy --break-system-packages")

    path = Path(args.file)
    data = json.loads(path.read_text(encoding="utf-8"))

    # fix_encoding RÉPARE uniquement l'encodage cassé (mojibake) sans toucher à la
    # typographie valide (guillemets courbes, apostrophes typographiques conservés).
    changes: list[tuple[str, str, str]] = []
    fixed_data = _fix_walk(data, fix_encoding, changes)

    # Regroupe par question pour le décompte
    qids = set()
    for p, _, _ in changes:
        # chemin type .questions[12].title  /  .questions[12].answers[2].value
        if ".questions[" in p:
            idx = p.split(".questions[", 1)[1].split("]", 1)[0]
            qids.add(idx)

    print(f"Fichier : {path}")
    print(f"Chaînes corrigées : {len(changes)}  (≈ {len(qids)} questions concernées)")
    if changes:
        print(f"\n--- Exemples (max {args.show}) ---")
        for p, b, a in changes[: args.show]:
            print(f"[{p}]")
            print(f"  AVANT : {b[:110]}")
            print(f"  APRÈS : {a[:110]}")

    if not args.apply:
        print("\n(DRY-RUN — rien écrit. Relancer avec --apply pour corriger.)")
        return
    if not changes:
        print("\nRien à corriger.")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(path.name + f".bak.{stamp}")
    shutil.copy2(path, backup)
    path.write_text(json.dumps(fixed_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ Corrigé : {len(changes)} chaînes. Sauvegarde : {backup.name}")
    print("⚠️ Le fingerprint de la banque change → l'index vectoriel sera reconstruit "
          "au prochain redémarrage (warmup mxbai, ~10 min en tâche de fond).")


if __name__ == "__main__":
    main()
