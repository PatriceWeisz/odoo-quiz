#!/usr/bin/env python3
"""RAG léger : questions similaires dans questions.json pour enrichir les prompts Claude."""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

from import_udemy import norm_title_key

try:
    from bank_embeddings import vector_similar_question_ids
except ImportError:
    vector_similar_question_ids = None  # type: ignore[misc, assignment]

BANK_RAG_TOP_N = 5
BANK_RAG_MIN_SCORE = 0.12
BANK_IDENTICAL_SCORE_DEFAULT = 0.98
BANK_RAG_PROMPT_MIN_DEFAULT = 0.45

_CFG_PATH = Path(__file__).resolve().parent / "config.json"


def _bank_rag_cfg() -> dict[str, Any]:
    try:
        with open(_CFG_PATH, encoding="utf-8") as f:
            br = json.load(f).get("bank_rag")
            return br if isinstance(br, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def duplicate_score_threshold() -> float:
    try:
        return float(_bank_rag_cfg().get("duplicate_score_threshold", BANK_IDENTICAL_SCORE_DEFAULT))
    except (TypeError, ValueError):
        return BANK_IDENTICAL_SCORE_DEFAULT


def rag_prompt_min_score() -> float:
    """Seuil minimal pour inclure une référence dans le prompt Claude."""
    try:
        return float(_bank_rag_cfg().get("rag_prompt_min_score", BANK_RAG_PROMPT_MIN_DEFAULT))
    except (TypeError, ValueError):
        return BANK_RAG_PROMPT_MIN_DEFAULT


_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "are",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "how",
        "does",
        "can",
        "you",
        "your",
        "will",
        "would",
        "have",
        "has",
        "had",
        "into",
        "than",
        "then",
        "them",
        "they",
        "their",
        "there",
        "about",
        "dans",
        "pour",
        "avec",
        "une",
        "des",
        "les",
        "est",
        "sont",
        "que",
        "qui",
        "pas",
        "sur",
    }
)


def _title_tokens(title: str) -> set[str]:
    raw = norm_title_key(title)
    return {w for w in raw.split() if len(w) > 2 and w not in _STOPWORDS}


def _bank_correct_index(q: dict) -> int | None:
    for j, a in enumerate(q.get("answers") or []):
        if isinstance(a, dict) and a.get("is_correct"):
            return j + 1
    return None


def _bank_correct_answer_text(q: dict) -> str:
    ci = _bank_correct_index(q)
    if ci is None:
        return ""
    answers = q.get("answers") or []
    if ci - 1 >= len(answers):
        return ""
    a = answers[ci - 1]
    if isinstance(a, dict):
        return (a.get("value") or "").strip()
    return str(a).strip()


def similarity_score(new_title: str, bank_q: dict) -> float:
    """Score mots-clés 0–1 (titre normalisé identique → 1.0)."""
    bank_title = (bank_q.get("title") or "").strip()
    if not bank_title:
        return 0.0
    tkey_new = norm_title_key(new_title)
    tkey_bank = norm_title_key(bank_title)
    if not tkey_new or not tkey_bank:
        return 0.0
    if tkey_new == tkey_bank:
        return 1.0

    tok_new = _title_tokens(new_title)
    tok_bank = _title_tokens(bank_title)
    if not tok_new or not tok_bank:
        return 0.0

    overlap = len(tok_new & tok_bank)
    keyword_part = overlap / max(len(tok_new), 1)
    ratio_part = difflib.SequenceMatcher(None, tkey_new, tkey_bank).ratio()
    contain_bonus = 0.15 if tkey_new in tkey_bank or tkey_bank in tkey_new else 0.0
    return min(1.0, keyword_part * 0.55 + ratio_part * 0.45 + contain_bonus)


def _combined_score(vec_sc: float | None, kw_sc: float) -> float:
    """Score affiché / tri : le meilleur signal entre vecteur et mots."""
    kw = float(kw_sc or 0.0)
    if vec_sc is not None:
        return max(float(vec_sc), kw)
    return kw


def _keyword_scored_candidates(
    title: str,
    question_bank: list[dict],
    *,
    min_score: float,
) -> dict[int, tuple[float, dict]]:
    out: dict[int, tuple[float, dict]] = {}
    for q in question_bank:
        if not isinstance(q, dict):
            continue
        qid = q.get("id")
        if qid is None or _bank_correct_index(q) is None:
            continue
        sc = similarity_score(title, q)
        if sc >= min_score:
            out[int(qid)] = (sc, q)
    return out


def _row_from_question(
    q: dict,
    *,
    score: float,
    score_vector: float | None,
    score_keyword: float | None,
    is_duplicate: bool = False,
) -> dict[str, Any]:
    opts: list[str] = []
    for a in q.get("answers") or []:
        if isinstance(a, dict):
            v = (a.get("value") or "").strip()
            if v:
                opts.append(v)
    return {
        "id": q.get("id"),
        "score": round(score, 3),
        "score_vector": round(score_vector, 3) if score_vector is not None else None,
        "score_keyword": round(score_keyword, 3) if score_keyword is not None else None,
        "is_duplicate": is_duplicate,
        "title": (q.get("title") or "").strip(),
        "title_fr": (q.get("title_fr") or "").strip(),
        "options": opts,
        "correct_index": _bank_correct_index(q),
        "correct_text": _bank_correct_answer_text(q),
        "explication": (q.get("explication_claude") or q.get("explication_udemy") or "").strip()[:400],
    }


def find_similar_bank_questions(
    new_title: str,
    question_bank: list[dict],
    *,
    top_n: int = BANK_RAG_TOP_N,
    min_score: float = BANK_RAG_MIN_SCORE,
    pin_bank_id: int | None = None,
) -> list[dict[str, Any]]:
    """
    Questions banque les plus proches (réponse connue).
    Tri par max(vecteur, mots) ; fiche doublon épinglée en tête à 100 % si pin_bank_id.
    """
    title = (new_title or "").strip()
    if not title or not question_bank:
        return []

    id_to_q: dict[int, dict] = {}
    for q in question_bank:
        if isinstance(q, dict) and q.get("id") is not None and _bank_correct_index(q) is not None:
            id_to_q[int(q["id"])] = q

    parts: dict[int, dict[str, float | None]] = {}
    kw_map = _keyword_scored_candidates(title, question_bank, min_score=min_score)
    for qid, (kw_sc, _) in kw_map.items():
        parts[qid] = {"kw": kw_sc, "vec": None}

    if vector_similar_question_ids is not None:
        try:
            vec_hits = vector_similar_question_ids(title, top_n=max(top_n * 4, 16))
        except Exception:
            vec_hits = []
        for qid, cos in vec_hits:
            if qid not in id_to_q:
                continue
            if qid not in parts:
                parts[qid] = {"kw": 0.0, "vec": cos}
            else:
                parts[qid]["vec"] = cos

    combined: list[tuple[int, float, float | None, float]] = []
    pin_int = int(pin_bank_id) if pin_bank_id is not None else None
    for qid, p in parts.items():
        if pin_int is not None and qid == pin_int:
            continue
        kw_sc = float(p.get("kw") or 0.0)
        vec_raw = p.get("vec")
        vec_sc = float(vec_raw) if vec_raw is not None else None
        sc = _combined_score(vec_sc, kw_sc)
        if sc >= min_score:
            combined.append((qid, sc, vec_sc, kw_sc))

    combined.sort(key=lambda x: (-x[1], -float(x[2] or 0), len(id_to_q.get(x[0], {}).get("title", ""))))

    others_n = max(0, top_n - 1) if pin_int is not None else top_n

    out: list[dict[str, Any]] = []
    if pin_int is not None and pin_int in id_to_q:
        q_pin = id_to_q[pin_int]
        vec_pin = parts.get(pin_int, {}).get("vec")
        vec_f = float(vec_pin) if vec_pin is not None else None
        kw_pin = float(parts.get(pin_int, {}).get("kw") or 1.0)
        out.append(
            _row_from_question(
                q_pin,
                score=1.0,
                score_vector=vec_f if vec_f is not None else 1.0,
                score_keyword=max(kw_pin, 1.0),
                is_duplicate=True,
            )
        )

    for qid, sc, vec_sc, kw_sc in combined[:others_n]:
        q = id_to_q.get(qid)
        if not q:
            continue
        out.append(
            _row_from_question(
                q,
                score=sc,
                score_vector=vec_sc,
                score_keyword=kw_sc,
                is_duplicate=False,
            )
        )

    return out[:top_n]


def filter_similar_for_prompt(similar: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Références envoyées à Claude : doublon toujours inclus, sinon score >= seuil prompt."""
    floor = rag_prompt_min_score()
    kept = [r for r in similar if r.get("is_duplicate") or float(r.get("score") or 0) >= floor]
    return kept


def rag_search_mode_label() -> str:
    try:
        from bank_embeddings import get_bank_vector_index

        return "vecteur prioritaire" if get_bank_vector_index() else "texte"
    except ImportError:
        return "texte"


def format_bank_rag_prompt_block(
    new_title: str,
    question_bank: list[dict] | None,
    *,
    top_n: int = BANK_RAG_TOP_N,
    pin_bank_id: int | None = None,
) -> str:
    """Bloc texte injecté dans le prompt Claude (RAG)."""
    similar = find_similar_bank_questions(
        new_title,
        question_bank or [],
        top_n=top_n,
        pin_bank_id=pin_bank_id,
    )
    for_prompt = filter_similar_for_prompt(similar)
    if not for_prompt:
        return (
            "\n=== Banque RAG ===\n"
            "Aucune question proche avec réponse validée trouvée — raisonne avec la doc Odoo et la capture.\n"
        )

    rag_mode = rag_search_mode_label()
    parts = [
        f"\n=== Banque RAG ({rag_mode}) : {len(for_prompt)} référence(s) (seuil similarité {int(rag_prompt_min_score() * 100)} %) ===",
        "Exemples validés en banque — raisonne par analogie ; ne copie pas si les options diffèrent.\n",
    ]
    for i, row in enumerate(for_prompt, 1):
        opts = row.get("options") or []
        opt_lines = "\n".join(f"     {j}. {o[:200]}" for j, o in enumerate(opts[:6], 1))
        ci = row.get("correct_index")
        ct = row.get("correct_text") or ""
        expl = row.get("explication") or ""
        qid = row.get("id")
        sc = row.get("score")
        if row.get("is_duplicate"):
            head = f"\n--- Référence {i} : MÊME question en banque (id {qid}, similarité {sc}) ---"
        else:
            head = f"\n--- Référence {i} (banque id {qid}, similarité {sc}) ---"
        parts.append(
            f"{head}\n"
            f"Q : {row.get('title')}\n"
            f"{opt_lines}\n"
            f"Bonne réponse enregistrée : option n°{ci} — {ct[:220]}"
        )
        if expl:
            parts.append(f"Explication banque : {expl}")
    parts.append("")
    return "\n".join(parts)
