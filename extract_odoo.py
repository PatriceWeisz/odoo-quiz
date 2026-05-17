#!/usr/bin/env python3
"""
Extrait les questions d'un sondage Odoo (module Survey) via XML-RPC
et les sauvegarde dans questions.json.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import xmlrpc.client
from pathlib import Path
from typing import Any, Optional

CONFIG_FILE = Path(__file__).parent / "config.json"


class OdooExtractError(Exception):
    """Erreur lisible pour l'UI ou la CLI."""


def load_config(path: Optional[Path] = None) -> dict:
    cfg_path = path or CONFIG_FILE
    if not cfg_path.exists():
        raise OdooExtractError("config.json introuvable. Copiez config.example.json et remplissez la section odoo.")
    with open(cfg_path, encoding="utf-8") as f:
        return json.load(f)


def connect(cfg: dict) -> tuple[str, str, int, Any]:
    odoo = cfg.get("odoo") or {}
    url = (odoo.get("url") or "").rstrip("/")
    db = odoo.get("db") or ""
    login = odoo.get("login") or ""
    api_key = odoo.get("api_key") or ""
    if not all([url, db, login, api_key]):
        raise OdooExtractError("Section odoo incomplète dans config.json (url, db, login, api_key).")

    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True)
    try:
        uid = common.authenticate(db, login, api_key, {})
    except Exception as e:
        raise OdooExtractError(f"Connexion Odoo échouée : {e}") from e

    if not uid:
        raise OdooExtractError("Authentification Odoo échouée. Vérifiez login et api_key.")

    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)
    return db, api_key, uid, models


def list_surveys(
    db: str,
    api_key: str,
    uid: int,
    models: Any,
    *,
    search: str = "",
    limit: int = 40,
) -> list[dict]:
    domain: list = []
    if search and search.strip():
        domain = [["title", "ilike", search.strip()]]
    rows = models.execute_kw(
        db,
        uid,
        api_key,
        "survey.survey",
        "search_read",
        [domain],
        {"fields": ["id", "title", "question_ids"], "limit": limit, "order": "title asc"},
    )
    out = []
    for s in rows:
        qids = s.get("question_ids") or []
        out.append(
            {
                "id": s["id"],
                "title": s.get("title") or "",
                "question_count": len(qids),
            }
        )
    return out


def resolve_survey(
    db: str,
    api_key: str,
    uid: int,
    models: Any,
    *,
    survey_name: Optional[str] = None,
    survey_id: Optional[int] = None,
) -> dict:
    if survey_id is not None:
        surveys = models.execute_kw(
            db,
            uid,
            api_key,
            "survey.survey",
            "search_read",
            [[["id", "=", int(survey_id)]]],
            {"fields": ["id", "title", "question_ids"], "limit": 1},
        )
        if not surveys:
            raise OdooExtractError(f"Aucun sondage avec l'id {survey_id}.")
        return surveys[0]

    name = (survey_name or "certification").strip()
    surveys = models.execute_kw(
        db,
        uid,
        api_key,
        "survey.survey",
        "search_read",
        [[["title", "ilike", name]]],
        {"fields": ["id", "title", "question_ids"], "limit": 10},
    )
    if not surveys:
        raise OdooExtractError(f"Aucun sondage trouvé avec le nom « {name} ».")
    if len(surveys) > 1:
        titles = ", ".join(f"[{s['id']}] {s['title']}" for s in surveys[:5])
        raise OdooExtractError(
            f"Plusieurs sondages correspondent à « {name} » : {titles}. "
            "Précisez survey_name ou utilisez --survey-id."
        )
    return surveys[0]


def fetch_questions(db: str, api_key: str, uid: int, models: Any, survey: dict) -> list[dict]:
    qids = survey.get("question_ids") or []
    if not qids:
        raise OdooExtractError("Ce sondage ne contient aucune question.")

    questions_raw = models.execute_kw(
        db,
        uid,
        api_key,
        "survey.question",
        "read",
        [qids],
        {
            "fields": [
                "id",
                "title",
                "question_type",
                "description",
                "suggested_answer_ids",
                "is_scored_question",
                "answer_score",
                "sequence",
            ]
        },
    )

    answer_ids: list[int] = []
    for q in questions_raw:
        answer_ids.extend(q.get("suggested_answer_ids") or [])
    answer_ids = list(set(answer_ids))

    answers_map: dict[int, dict] = {}
    if answer_ids:
        available_fields = models.execute_kw(
            db,
            uid,
            api_key,
            "survey.question.answer",
            "fields_get",
            [],
            {"attributes": ["string"]},
        )
        has_is_correct = "is_correct" in available_fields
        has_answer_score = "answer_score" in available_fields

        fields = ["id", "value"]
        if has_is_correct:
            fields.append("is_correct")
        if has_answer_score:
            fields.append("answer_score")

        answers_raw = models.execute_kw(
            db,
            uid,
            api_key,
            "survey.question.answer",
            "read",
            [answer_ids],
            {"fields": fields},
        )
        for a in answers_raw:
            answers_map[a["id"]] = {
                "id": a["id"],
                "value": a["value"],
                "is_correct": a.get("is_correct", False),
                "score": a.get("answer_score", 0),
            }

    questions = []
    for q in sorted(questions_raw, key=lambda x: x.get("sequence", 0)):
        answers = [answers_map[aid] for aid in q.get("suggested_answer_ids", []) if aid in answers_map]
        raw_desc = q.get("description") or ""
        expl_senedoo = re.sub(r"<[^>]+>", " ", raw_desc).strip() if raw_desc else ""
        row = {
            "id": q["id"],
            "title": q["title"],
            "type": q["question_type"],
            "answers": answers,
            "is_scored": q.get("is_scored_question", False),
            "explication_senedoo": expl_senedoo,
            "explication_claude": "",
        }
        if any(a.get("is_correct") for a in answers):
            row["correct_answer_source"] = "odoo"
        questions.append(row)

    return questions


def extract_survey_to_file(
    cfg: Optional[dict] = None,
    *,
    survey_name: Optional[str] = None,
    survey_id: Optional[int] = None,
    out_path: Optional[Path] = None,
    backup: bool = True,
) -> dict:
    """Extrait un sondage Odoo vers questions.json. Retourne un résumé (stats)."""
    cfg = cfg or load_config()
    db, api_key, uid, models = connect(cfg)
    survey = resolve_survey(
        db,
        api_key,
        uid,
        models,
        survey_name=survey_name or cfg.get("survey_name"),
        survey_id=survey_id,
    )
    questions = fetch_questions(db, api_key, uid, models, survey)

    root = Path(__file__).parent
    out_file = out_path or (root / cfg.get("questions_file", "questions.json"))
    if backup and out_file.is_file():
        bak = out_file.with_suffix(out_file.suffix + ".bak")
        shutil.copy2(out_file, bak)

    payload = {"survey": survey["title"], "questions": questions}
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    with_answers = sum(1 for q in questions if q.get("answers"))
    with_correct = sum(1 for q in questions if any(a.get("is_correct") for a in q.get("answers", [])))
    return {
        "ok": True,
        "survey_id": survey["id"],
        "survey_title": survey["title"],
        "question_count": len(questions),
        "with_answers": with_answers,
        "with_correct": with_correct,
        "out_file": str(out_file),
        "backup": str(out_file.with_suffix(out_file.suffix + ".bak")) if backup and out_file.is_file() else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Extraire un quiz Odoo (Survey) vers questions.json")
    parser.add_argument("--list", action="store_true", help="Lister les sondages disponibles")
    parser.add_argument("--search", default="", help="Filtre titre (avec --list)")
    parser.add_argument("--survey-id", type=int, help="Id exact du sondage Odoo")
    parser.add_argument("--survey-name", help="Recherche ilike sur le titre (défaut : survey_name dans config.json)")
    parser.add_argument("--no-backup", action="store_true", help="Ne pas créer questions.json.bak")
    args = parser.parse_args()

    try:
        cfg = load_config()
        db, api_key, uid, models = connect(cfg)
        print(f"✅ Connecté à {cfg['odoo']['url']} (uid={uid})")

        if args.list:
            rows = list_surveys(db, api_key, uid, models, search=args.search, limit=50)
            if not rows:
                print("Aucun sondage trouvé.")
                return
            for s in rows:
                print(f"  [{s['id']}] {s['title']} — {s['question_count']} question(s)")
            return

        summary = extract_survey_to_file(
            cfg,
            survey_id=args.survey_id,
            survey_name=args.survey_name,
            backup=not args.no_backup,
        )
        print(f"📋 Sondage : [{summary['survey_id']}] {summary['survey_title']}")
        print(f"✅ {summary['question_count']} questions → {summary['out_file']}")
        print(f"   • {summary['with_answers']} avec réponses proposées")
        print(f"   • {summary['with_correct']} avec bonne réponse identifiée dans Odoo")
        if summary.get("backup"):
            print(f"   • Sauvegarde : {summary['backup']}")
        if summary["with_correct"] == 0:
            print("   ℹ️  Lancez generate_explanations.py pour traductions FR et explications.")
    except OdooExtractError as e:
        sys.exit(f"❌ {e}")


if __name__ == "__main__":
    main()
