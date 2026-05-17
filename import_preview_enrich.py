#!/usr/bin/env python3
"""Avant validation capture : FR + bonne réponse + explication (banque existante, sinon API Claude)."""

from __future__ import annotations

import difflib
import unicodedata
from typing import Any

from markupsafe import Markup, escape

from bank_rag import (
    find_similar_bank_questions,
    format_bank_rag_prompt_block,
    rag_prompt_min_score,
    rag_search_mode_label,
)
from import_udemy import norm_title_key

# Guillemets / apostrophes / tirets / espaces typographiques → forme ASCII pour la comparaison seule.
_TYPO_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u2032": "'",
        "\u00b4": "'",
        "\u02bc": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u00ab": '"',
        "\u00bb": '"',
        "\u2039": "'",
        "\u203a": "'",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u00ad": "",
    }
)


def normalize_text_for_merge_compare(s: str, *, multiline: bool = False) -> str:
    """Chaîne normalisée : les écarts typographiques ne comptent pas (titres, réponses, explications)."""
    t = unicodedata.normalize("NFC", s or "")
    t = t.translate(_TYPO_TRANSLATION)
    buf: list[str] = []
    for ch in t:
        if unicodedata.category(ch) == "Zs" and ch != " ":
            buf.append(" ")
        else:
            buf.append(ch)
    t = "".join(buf)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    # Guillemets droits ASCII : même rôle typographique que ' pour la comparaison.
    t = t.replace('"', "'")
    if multiline:
        lines = [" ".join(line.split()) for line in t.split("\n")]
        return "\n".join(lines).strip()
    return " ".join(t.split())


def _pair_diff_html(left: str, right: str, *, multiline: bool = False) -> tuple[Markup, Markup]:
    """Deux chaînes côte à côte : surlignage des segments divergents (hors pure typographie)."""
    if left == right:
        e = escape(left)
        return e, e
    if normalize_text_for_merge_compare(left, multiline=multiline) == normalize_text_for_merge_compare(
        right, multiline=multiline
    ):
        return escape(left), escape(right)
    sm = difflib.SequenceMatcher(None, left, right, autojunk=False)
    l_parts: list[Markup] = []
    r_parts: list[Markup] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        sl, sr = left[i1:i2], right[j1:j2]
        if tag == "equal":
            l_parts.append(escape(sl))
            r_parts.append(escape(sr))
        elif tag == "delete":
            l_parts.append(Markup('<span class="diff-token diff-bank-only">{}</span>').format(escape(sl)))
        elif tag == "insert":
            r_parts.append(Markup('<span class="diff-token diff-cap-only">{}</span>').format(escape(sr)))
        else:
            l_parts.append(Markup('<span class="diff-token diff-bank-only">{}</span>').format(escape(sl)))
            r_parts.append(Markup('<span class="diff-token diff-cap-only">{}</span>').format(escape(sr)))
    return Markup("").join(l_parts), Markup("").join(r_parts)


def _answers_en_normalized(answers: list) -> list[str]:
    out: list[str] = []
    for a in answers or []:
        if isinstance(a, dict):
            out.append(normalize_text_for_merge_compare((a.get("value") or "").strip()))
        else:
            out.append(normalize_text_for_merge_compare(str(a or "").strip()))
    return out


def _options_en_match(capture_answers: list, bank_q: dict) -> bool:
    bank_ans = bank_q.get("answers") or []
    if len(capture_answers) != len(bank_ans):
        return False
    bank_vals = [
        (a.get("value") or "").strip() if isinstance(a, dict) else str(a or "").strip() for a in bank_ans
    ]
    return _answers_en_normalized(capture_answers) == _answers_en_normalized(bank_vals)


def bank_identical_meta(
    title_en: str,
    capture_answers: list,
    questions: list[dict],
) -> tuple[bool, Any, float, str]:
    """
    Doublon strict (question déjà en banque) :
    - titre normalisé identique + mêmes options EN, ou
    - score RAG >= seuil (défaut 0,98) + mêmes options sur la fiche trouvée.
    """
    from bank_rag import duplicate_score_threshold, find_similar_bank_questions

    title = (title_en or "").strip()
    answers = capture_answers or []
    if not title or not questions:
        return False, None, 0.0, ""

    key = norm_title_key(title)
    if key:
        for q in questions:
            if not isinstance(q, dict):
                continue
            if norm_title_key(q.get("title", "")) == key:
                if _options_en_match(answers, q):
                    return True, q.get("id"), 1.0, "titre_options"
                break

    thresh = duplicate_score_threshold()
    for ref in find_similar_bank_questions(title, questions, top_n=5, min_score=thresh):
        sc = float(ref.get("score") or 0)
        if sc < thresh:
            continue
        qid = ref.get("id")
        bank_q = next((q for q in questions if isinstance(q, dict) and q.get("id") == qid), None)
        if bank_q and _options_en_match(answers, bank_q):
            return True, qid, sc, "score_rag"

    return False, None, 0.0, ""


def duplicate_bank_meta(title_en: str, questions: list[dict]) -> tuple[bool, Any]:
    """(True, id) si titre normalisé identique (sans contrôle des options — legacy)."""
    key = norm_title_key(title_en)
    if not key:
        return False, None
    for q in questions:
        if norm_title_key(q.get("title", "")) == key:
            return True, q.get("id")
    return False, None


def _find_bank_question(title_en: str, questions: list[dict]) -> dict | None:
    key = norm_title_key(title_en)
    if not key:
        return None
    for q in questions:
        if norm_title_key(q.get("title", "")) == key:
            return q
    return None


def bank_capture_merge_context(item: dict, questions: list[dict]) -> dict[str, Any]:
    """Comparaison banque / capture : écarts détaillés + indicateur merge_conflict (nombre / image)."""
    from question_images import has_valid_question_image

    base: dict[str, Any] = {
        "show_bank_diff": False,
        "merge_conflict": False,
        "has_any_diff": False,
        "merge_count_mismatch": False,
        "merge_image_gap": False,
        "bank_title_en": "",
        "bank_title_fr": "",
        "bank_answers_en": [],
        "bank_n_answers": 0,
        "bank_has_question_image": False,
        "capture_n_answers": len(item.get("answers") or []),
        "title_en_differs_visible": False,
        "title_fr_differs_visible": False,
        "correct_differs": False,
        "correct_index_bank": None,
        "correct_index_capture": None,
        "explication_claude_differs": False,
        "explication_udemy_differs": False,
        "diff_rows": [],
        "diff_rows_fr": [],
        "diff_rows_en_changed": [],
        "diff_rows_fr_changed": [],
        "show_title_compare": False,
        "show_en_diff_table": False,
        "show_fr_diff_table": False,
        "image_status_differs": False,
        "image_overwrite_desired": False,
    }
    if not item.get("in_banque"):
        return base
    bank = _find_bank_question(item.get("title", ""), questions)
    if not bank:
        return base
    cap_answers = [str(x).strip() for x in (item.get("answers") or [])]
    bank_answers = [(a.get("value") or "").strip() for a in (bank.get("answers") or [])]
    cap_fr = [str(x).strip() for x in (item.get("answers_fr") or [])]
    bank_fr = [(a.get("value_fr") or "").strip() for a in (bank.get("answers") or [])]
    bank_n = len(bank_answers)
    cap_n = len(cap_answers)
    count_mm = cap_n != bank_n
    bank_has_img = has_valid_question_image(bank)
    cap_wants_img = bool(item.get("needs_question_image"))
    img_gap = cap_wants_img and not bank_has_img
    image_status_differs = cap_wants_img != bank_has_img
    image_overwrite_desired = cap_wants_img and bank_has_img
    bank_t_en = (bank.get("title") or "").strip()
    cap_t_en = (item.get("title") or "").strip()
    bank_t_fr = (bank.get("title_fr") or "").strip()
    cap_t_fr = (item.get("title_fr") or "").strip()
    title_en_diff = normalize_text_for_merge_compare(bank_t_en) != normalize_text_for_merge_compare(cap_t_en)
    title_fr_diff = normalize_text_for_merge_compare(bank_t_fr) != normalize_text_for_merge_compare(cap_t_fr)
    ci_bank = None
    for j, a in enumerate(bank.get("answers") or []):
        if a.get("is_correct"):
            ci_bank = j + 1
            break
    ci_cap = item.get("correct_index")
    if not isinstance(ci_cap, int):
        ci_cap = None
    correct_diff = ci_bank != ci_cap
    expl_c_bank = (bank.get("explication_claude") or "").strip()
    expl_c_cap = (item.get("explication_claude") or "").strip()
    expl_claude_diff = normalize_text_for_merge_compare(
        expl_c_bank, multiline=True
    ) != normalize_text_for_merge_compare(expl_c_cap, multiline=True)
    expl_u_bank = (bank.get("explication_senedoo") or "").strip()
    expl_u_cap = (item.get("explication_udemy") or "").strip()
    expl_udemy_diff = normalize_text_for_merge_compare(
        expl_u_bank, multiline=True
    ) != normalize_text_for_merge_compare(expl_u_cap, multiline=True)

    rows: list[dict[str, Any]] = []
    rows_fr: list[dict[str, Any]] = []
    for j in range(max(bank_n, cap_n)):
        b_txt = bank_answers[j] if j < bank_n else ""
        c_txt = cap_answers[j] if j < cap_n else ""
        rows.append(
            {
                "idx": j + 1,
                "bank": b_txt,
                "cap": c_txt,
                "row_diff": normalize_text_for_merge_compare(b_txt)
                != normalize_text_for_merge_compare(c_txt),
            }
        )
        bf = bank_fr[j] if j < len(bank_fr) else ""
        cf = cap_fr[j] if j < len(cap_fr) else ""
        rows_fr.append(
            {
                "idx": j + 1,
                "bank": bf,
                "cap": cf,
                "row_diff": normalize_text_for_merge_compare(bf) != normalize_text_for_merge_compare(cf),
            }
        )
    for r in rows:
        r["bank_html"], r["cap_html"] = _pair_diff_html(r["bank"], r["cap"])
    for r in rows_fr:
        r["bank_html"], r["cap_html"] = _pair_diff_html(r["bank"], r["cap"])

    any_row_en = any(r["row_diff"] for r in rows)
    any_row_fr = any(r["row_diff"] for r in rows_fr)
    merge_conflict = count_mm or img_gap
    has_any = (
        count_mm
        or img_gap
        or image_status_differs
        or image_overwrite_desired
        or title_en_diff
        or title_fr_diff
        or correct_diff
        or expl_claude_diff
        or expl_udemy_diff
        or any_row_en
        or any_row_fr
    )
    rows_en_changed = [r for r in rows if r["row_diff"]] if not count_mm else rows
    rows_fr_changed = [r for r in rows_fr if r["row_diff"]] if not count_mm else rows_fr
    show_title_compare = title_en_diff or title_fr_diff
    show_en_diff_table = count_mm or bool(rows_en_changed)
    show_fr_diff_table = count_mm or bool(rows_fr_changed)
    merge_payload: dict[str, Any] = {
            "show_bank_diff": True,
            "merge_conflict": merge_conflict,
            "has_any_diff": has_any,
            "merge_count_mismatch": count_mm,
            "merge_image_gap": img_gap,
            "bank_title_en": (bank.get("title") or "").strip(),
            "bank_title_fr": (bank.get("title_fr") or "").strip(),
            "bank_answers_en": bank_answers,
            "bank_n_answers": bank_n,
            "bank_has_question_image": bank_has_img,
            "capture_n_answers": cap_n,
            "title_en_differs_visible": title_en_diff,
            "title_fr_differs_visible": title_fr_diff,
            "correct_differs": correct_diff,
            "correct_index_bank": ci_bank,
            "correct_index_capture": ci_cap,
            "explication_claude_differs": expl_claude_diff,
            "explication_udemy_differs": expl_udemy_diff,
            "diff_rows": rows,
            "diff_rows_fr": rows_fr,
            "diff_rows_en_changed": rows_en_changed,
            "diff_rows_fr_changed": rows_fr_changed,
            "show_title_compare": show_title_compare,
            "show_en_diff_table": show_en_diff_table,
            "show_fr_diff_table": show_fr_diff_table,
            "image_status_differs": image_status_differs,
            "image_overwrite_desired": image_overwrite_desired,
    }
    if show_title_compare:
        ben, cen = _pair_diff_html(bank_t_en, cap_t_en)
        bfr, cfr = _pair_diff_html(bank_t_fr, cap_t_fr)
        merge_payload["bank_title_en_html"] = ben
        merge_payload["capture_title_en_html"] = cen
        merge_payload["bank_title_fr_html"] = bfr
        merge_payload["capture_title_fr_html"] = cfr
    base.update(merge_payload)
    return base


def _norm_correct_index(ci: Any, n_answers: int) -> int | None:
    if isinstance(ci, int) and 1 <= ci <= n_answers:
        return ci
    return None


def _overlay_from_bank(item: dict, bank_q: dict) -> dict[str, Any] | None:
    """Alignement par indice : même nombre de réponses que sur la capture."""
    n = len(item["answers"])
    bank_ans = bank_q.get("answers") or []
    if len(bank_ans) != n:
        return None
    title_fr = (bank_q.get("title_fr") or "").strip()
    answers_fr = []
    for a in bank_ans:
        answers_fr.append((a.get("value_fr") or "").strip())
    ci = None
    for j, a in enumerate(bank_ans):
        if a.get("is_correct"):
            ci = j + 1
            break
    expl = (bank_q.get("explication_claude") or "").strip()
    prov = bank_q.get("correct_answer_source")
    if prov not in ("udemy", "claude", "user"):
        prov = (
            "udemy"
            if any(a.get("is_correct") for a in bank_ans)
            else None
        )
    return {
        "title_fr": title_fr,
        "answers_fr": answers_fr,
        "correct_index": ci if ci is not None else item.get("correct_index"),
        "explication_claude": expl,
        "match_source": "banque",
        "bank_answer_provenance": prov,
    }


CLAUDE_FAIL_SOURCES = frozenset(
    {"sans_api", "claude_api_erreur", "claude_api_timeout", "claude_api_surcharge", "claude_incertain"}
)
CLAUDE_OK_SOURCES = frozenset({"claude_api"})

_IDK_PHRASES = (
    "i don't know",
    "i do not know",
    "je ne sais pas",
    "don't know",
    "do not know",
)


def is_idont_know_option(text: str) -> bool:
    """Option type « I don't know » (à ne jamais proposer comme bonne réponse)."""
    t = normalize_text_for_merge_compare(text or "").lower().strip(" .!?")
    if not t:
        return False
    for p in _IDK_PHRASES:
        if t == p or t.startswith(p + " ") or t.endswith(" " + p):
            return True
    return len(t) <= 22 and "don't know" in t


def correct_index_points_to_idk(item: dict, ci: int | None) -> bool:
    if ci is None or not isinstance(ci, int):
        return False
    answers = item.get("answers") or []
    if ci < 1 or ci > len(answers):
        return False
    return is_idont_know_option(str(answers[ci - 1]))


def _title_fr_seems_incomplete(title_en: str, title_fr: str) -> bool:
    te = (title_en or "").strip()
    tf = (title_fr or "").strip()
    if not te:
        return False
    if not tf:
        return True
    if len(tf) < int(len(te) * 0.55):
        return True
    return False


def _rag_pin_bank_id(item: dict) -> int | None:
    if not item.get("in_banque"):
        return None
    eid = item.get("existing_id")
    if eid is None:
        return None
    try:
        return int(eid)
    except (TypeError, ValueError):
        return None


def _attach_rag_similar(item: dict, all_questions: list[dict]) -> None:
    pin = _rag_pin_bank_id(item)
    similar = find_similar_bank_questions(
        item.get("title") or "",
        all_questions,
        pin_bank_id=pin,
    )
    floor = rag_prompt_min_score()
    for row in similar:
        row["in_prompt"] = bool(row.get("is_duplicate")) or float(row.get("score") or 0) >= floor
    item["rag_similar"] = similar
    item["rag_search_mode"] = rag_search_mode_label()
    item["rag_prompt_min_score"] = floor


def _capture_ui_label(item: dict) -> str:
    src = (item.get("_capture_source") or "udemy").strip().lower()
    return "site Odoo (eLearning / quiz)" if src in ("odoo", "odoo_web", "website") else "capture Udemy"


def _build_api_prompt(item: dict, all_questions: list[dict] | None = None) -> str:
    lines = "\n".join(f"  {i + 1}. {opt}" for i, opt in enumerate(item["answers"]))
    ci_hint = item.get("correct_index")
    ui_name = _capture_ui_label(item)
    if isinstance(ci_hint, int) and 1 <= ci_hint <= len(item["answers"]):
        if item.get("_vision_hint_without_ui"):
            hint = (
                f"Un modèle d’analyse d’image ({ui_name}) a proposé l’option n°{ci_hint} (1-based), mais **rien sur la capture ne montre cette réponse "
                "comme sélectionnée ou comme correction affichée**. "
                "Par ton expertise certification Odoo, détermine la bonne option ; tu peux **confirmer ou corriger** cet indice.\n"
            )
        else:
            hint = f"Indice depuis la capture ({ui_name}) : bonne réponse = option n°{ci_hint} (1-based). "
    elif (item.get("match_source") or "") == "banque_sans_bonne_reponse":
        hint = (
            "La banque contient cette question mais aucune bonne réponse n'y est enregistrée ; "
            "la capture ne fournit pas la réponse. Tu dois déterminer la bonne option. "
        )
    else:
        hint = (
            "La bonne réponse n'est pas indiquée sur la capture : déduis-la par expertise Odoo. "
            f"Fournis obligatoirement correct_index (entier de 1 à {len(item['answers'])}). "
            "INTERDIT : choisir une option « I don't know » / « Je ne sais pas » — sélectionne une réponse technique.\n"
        )
    idk_note = ""
    if any(is_idont_know_option(a) for a in item["answers"]):
        idk_note = (
            "\nCertaines options sont « I don't know » : conserve-les dans answers_fr si présentes en EN, "
            "mais correct_index doit pointer vers une option **technique** (pas I don't know).\n"
        )
    bank_ctx = (
        format_bank_rag_prompt_block(item["title"], all_questions, pin_bank_id=_rag_pin_bank_id(item))
        if all_questions
        else ""
    )
    img_note = ""
    if item.get("needs_question_image"):
        img_note = (
            "\nUne **capture d’écran** est jointe : lis tableaux Odoo, onglets Achats/Inventaire, "
            "lignes fournisseurs/prix/quantités — la bonne réponse doit s’appuyer sur ces données visibles.\n"
        )
    return f"""Tu es expert certifié Odoo (certification officielle, logique métier Inventory / Purchase / MRP / Accounting).

Méthode (obligatoire avant de répondre) :
0) Lire le bloc **Banque RAG** ci-dessous : questions similaires déjà validées — raisonne par analogie, sans copier si les options diffèrent.
1) Identifier le module Odoo concerné (stock, achat, reordering rule, AVCO, lead time, etc.).
2) Si une image est fournie : extraire les chiffres et libellés **visibles** (ne pas inventer).
3) Éliminer les options manifestement fausses.
4) Choisir l’option restante la plus cohérente avec la doc Odoo ; si doute sérieux → confidence "low".

Question (EN) :
{item["title"]}

Options (EN), dans l'ordre :
{lines}

{hint}{idk_note}{img_note}{bank_ctx}

Réponds UNIQUEMENT avec un objet JSON valide (sans markdown), clés exactes :
{{
  "title_fr": "traduction française du titre",
  "answers_fr": ["traduction option 1", "..."],
  "correct_index": <entier 1 à {len(item["answers"])} ou null si confidence low>,
  "confidence": "high" ou "low",
  "explication_claude": "explication pédagogique en français (6 à 12 lignes) : pourquoi la bonne réponse, contexte Odoo."
}}

Contraintes :
- "title_fr" : traduction française **complète** du titre EN (tous les détails, chiffres, noms de champs Odoo, min/max, règles — **ne pas résumer** en une phrase générique).
- "answers_fr" : exactement {len(item["answers"])} chaînes, même ordre que les options EN ; traduction **fidèle** (ex. « I don't know » → « Je ne sais pas »).
- "correct_index" : jamais l'index d'une option « I don't know » ; null si confidence est "low".
- "confidence" : "high" seulement si tu es confiant ; sinon "low" et correct_index null (l’utilisateur choisira).
- Pas de texte hors du JSON."""


def _build_pick_answer_prompt(item: dict, all_questions: list[dict] | None = None) -> str:
    valid = [(i + 1, a) for i, a in enumerate(item["answers"]) if not is_idont_know_option(a)]
    if not valid:
        return ""
    nums = ", ".join(str(i) for i, _ in valid)
    lines = "\n".join(f"  {i}. {a}" for i, a in valid)
    bank_ctx = (
        format_bank_rag_prompt_block(item["title"], all_questions, pin_bank_id=_rag_pin_bank_id(item))
        if all_questions
        else ""
    )
    return f"""Tu es expert certifié Odoo. Choisis la bonne réponse (certification).

Consulte d’abord le bloc Banque RAG (questions similaires validées) pour t’en inspirer.

Question (EN) :
{item["title"]}

Options valides (numéros d'origine sur la capture) :
{lines}
{bank_ctx}
Réponds UNIQUEMENT avec : {{"correct_index": <un entier parmi : {nums}>, "confidence": "high" ou "low"}}
INTERDIT : « I don't know » / « Je ne sais pas ». Si doute sérieux : confidence "low" et correct_index null."""


def _answer_image_paths(item: dict, screenshot_path: str | None) -> list[str]:
    if not screenshot_path or not item.get("needs_question_image"):
        return []
    p = str(screenshot_path).strip()
    return [p] if p else []


def _apply_confidence_gate(item: dict, out: dict[str, Any]) -> None:
    conf = (out.get("confidence") or "").strip().lower()
    if conf == "low":
        out["correct_index"] = None
        out["match_source"] = "claude_incertain"


def _api_enrich(
    item: dict,
    screenshot_path: str | None = None,
    all_questions: list[dict] | None = None,
) -> dict[str, Any]:
    from quiz_llm import ANTHROPIC_REQUEST_TIMEOUT_S, api_available, parse_json_value, run_answer_prompt

    ANSWER_TIMEOUT = 60

    n = len(item["answers"])
    seed_afr = item.get("answers_fr")
    if isinstance(seed_afr, list) and len(seed_afr) == n:
        answers_fr = [str(x or "").strip() for x in seed_afr]
    else:
        answers_fr = [""] * n
    out: dict[str, Any] = {
        "title_fr": (item.get("title_fr") or "").strip(),
        "answers_fr": answers_fr,
        "correct_index": item.get("correct_index"),
        "explication_claude": (item.get("explication_claude") or "").strip(),
        "match_source": "claude_api",
    }
    if not api_available():
        out["match_source"] = "sans_api"
        return out
    img_paths = _answer_image_paths(item, screenshot_path)
    try:
        raw = run_answer_prompt(
            _build_api_prompt(item, all_questions),
            image_paths=img_paths,
            timeout=ANSWER_TIMEOUT,
        )
        data = parse_json_value(raw)
        if not isinstance(data, dict):
            raise ValueError("réponse non objet")
        out["confidence"] = (data.get("confidence") or "").strip().lower()
        tf = (data.get("title_fr") or "").strip()
        if tf:
            out["title_fr"] = tf
        frs = data.get("answers_fr")
        if isinstance(frs, list):
            for j in range(len(item["answers"])):
                if j < len(frs) and str(frs[j] or "").strip():
                    out["answers_fr"][j] = str(frs[j] or "").strip()
        ci = data.get("correct_index")
        if isinstance(ci, int) and 1 <= ci <= len(item["answers"]):
            out["correct_index"] = ci
        else:
            out["correct_index"] = None
        if correct_index_points_to_idk(item, out.get("correct_index")):
            out["correct_index"] = None
        _apply_confidence_gate(item, out)
        expl = (data.get("explication_claude") or "").strip()
        if expl:
            out["explication_claude"] = expl
        if out.get("match_source") == "claude_api" and _title_fr_seems_incomplete(
            item.get("title", ""), out.get("title_fr", "")
        ):
            try:
                raw2 = run_answer_prompt(
                    _build_api_prompt(item, all_questions)
                    + "\n\nLa traduction FR du titre était trop courte : retraduis le titre EN **en entier**, sans le résumer.",
                    image_paths=img_paths,
                    timeout=ANSWER_TIMEOUT,
                )
                data2 = parse_json_value(raw2)
                if isinstance(data2, dict):
                    tf2 = (data2.get("title_fr") or "").strip()
                    if tf2 and not _title_fr_seems_incomplete(item.get("title", ""), tf2):
                        out["title_fr"] = tf2
                    frs2 = data2.get("answers_fr")
                    if isinstance(frs2, list):
                        for j in range(len(item["answers"])):
                            if j < len(frs2) and str(frs2[j] or "").strip():
                                out["answers_fr"][j] = str(frs2[j] or "").strip()
            except Exception:
                pass
        if out.get("match_source") in ("claude_api", "claude_incertain") and out.get("correct_index") is None:
            picked = _api_pick_correct_index(item, screenshot_path, all_questions)
            if picked is not None:
                out["correct_index"] = picked
                out["match_source"] = "claude_api"
                out["confidence"] = "high"
    except RuntimeError as e:
        msg = str(e)
        low = msg.lower()
        if "dépasse le délai" in msg or "délai de" in low:
            out["match_source"] = "claude_api_timeout"
        elif "529" in msg or "satur" in low or "overloaded" in low:
            out["match_source"] = "claude_api_surcharge"
        else:
            out["match_source"] = "claude_api_erreur"
    except (ValueError, OSError):
        out["match_source"] = "claude_api_erreur"
    except Exception:
        out["match_source"] = "claude_api_erreur"
    return out


def _api_pick_correct_index(
    item: dict,
    screenshot_path: str | None = None,
    all_questions: list[dict] | None = None,
) -> int | None:
    from quiz_llm import api_available, parse_json_value, run_answer_prompt

    if not api_available():
        return None
    prompt = _build_pick_answer_prompt(item, all_questions)
    if not prompt:
        return None
    try:
        raw = run_answer_prompt(
            prompt,
            image_paths=_answer_image_paths(item, screenshot_path),
            timeout=60,
        )
        data = parse_json_value(raw)
        if not isinstance(data, dict):
            return None
        if (data.get("confidence") or "").strip().lower() == "low":
            return None
        ci = data.get("correct_index")
        if isinstance(ci, int) and 1 <= ci <= len(item["answers"]):
            if not correct_index_points_to_idk(item, ci):
                return ci
    except Exception:
        return None
    return None


def _bank_has_marked_correct(bank: dict | None) -> bool:
    if not bank:
        return False
    return any(a.get("is_correct") for a in (bank.get("answers") or []))


def _bank_answer_sentence_fr(prov: str | None) -> str:
    lab = (
        "site Odoo (capture corrigée)"
        if prov == "odoo"
        else "Udemy (capture corrigée)"
        if prov == "udemy"
        else "Claude"
        if prov == "claude"
        else "vous"
        if prov == "user"
        else "Udemy / Odoo"
    )
    return (
        f"Réponse provenant de la banque de questions "
        f"(cette bonne réponse avait été enregistrée comme : {lab})."
    )


def bank_answer_reused_for_preview(item: dict) -> bool:
    """True si la bonne réponse affichée provient d'une fiche banque exploitable (même nb d'options + réponse cochée)."""
    return bool(item.get("bank_registered_answer"))


def bank_dup_title_only(item: dict) -> bool:
    """Titre déjà en banque, mais réponse non reprise depuis la fiche (Claude, Udemy, saisie, etc.)."""
    return bool(item.get("in_banque")) and not bank_answer_reused_for_preview(item)


def enrich_item_for_preview(
    item: dict,
    all_questions: list[dict],
    screenshot_path: str | None = None,
) -> dict:
    """Retourne une copie enrichie : Claude systématique pour la réponse ; doublon banque = score strict."""
    from quiz_llm import api_available

    base = dict(item)
    n_ans = len(base.get("answers") or [])
    vision_ci = _norm_correct_index(base.get("correct_index"), n_ans)
    if correct_index_points_to_idk(base, vision_ci):
        vision_ci = None
        base["correct_index"] = None
        base["correct_index_visible"] = False
    ui_confirmed = base.get("correct_index_visible") is True and vision_ci is not None

    identical, existing_id, dup_score, dup_reason = bank_identical_meta(
        base.get("title") or "",
        base.get("answers") or [],
        all_questions,
    )
    base["in_banque"] = identical
    base["existing_id"] = existing_id if identical else None
    base["bank_duplicate_score"] = dup_score
    base["bank_duplicate_reason"] = dup_reason
    base["bank_registered_answer"] = False

    bank = _find_bank_question(base["title"], all_questions) if identical else None
    if identical and existing_id is not None:
        bank = next((q for q in all_questions if isinstance(q, dict) and q.get("id") == existing_id), bank)

    overlay = _overlay_from_bank(base, bank) if bank else None
    same_len = bool(bank) and len(bank.get("answers") or []) == n_ans

    if same_len and overlay:
        if overlay.get("title_fr"):
            base["title_fr"] = overlay["title_fr"]
        if overlay.get("answers_fr"):
            base["answers_fr"] = overlay["answers_fr"]

    bank_prior_ci: int | None = None
    bank_prior_prov: str | None = None
    if bank and same_len and _bank_has_marked_correct(bank):
        for j, a in enumerate(bank.get("answers") or []):
            if isinstance(a, dict) and a.get("is_correct"):
                bank_prior_ci = j + 1
                break
        bank_prior_prov = bank.get("correct_answer_source")
        if bank_prior_prov not in ("udemy", "odoo", "claude", "user"):
            bank_prior_prov = "udemy"

    ci_before_api = _norm_correct_index(base.get("correct_index"), n_ans)
    if ci_before_api is not None and not ui_confirmed:
        base["_vision_hint_without_ui"] = True

    claude_called = False
    if api_available():
        base.update(_api_enrich(base, screenshot_path=screenshot_path, all_questions=all_questions))
        claude_called = True
    else:
        base["match_source"] = "sans_api"
        if ui_confirmed and vision_ci is not None:
            base["correct_index"] = vision_ci
        elif bank_prior_ci is not None and identical:
            base["correct_index"] = bank_prior_ci

    base.pop("_vision_hint_without_ui", None)

    ms = (base.get("match_source") or "").strip()
    if claude_called and not ui_confirmed and ms in CLAUDE_FAIL_SOURCES:
        base["correct_index"] = None

    final_ci = _norm_correct_index(base.get("correct_index"), n_ans)
    if correct_index_points_to_idk(base, final_ci):
        picked = _api_pick_correct_index(base, screenshot_path, all_questions)
        if picked is not None:
            final_ci = picked
            base["correct_index"] = picked
            if ms in CLAUDE_FAIL_SOURCES or not ms:
                base["match_source"] = "claude_api"
                ms = "claude_api"

    if bank_prior_ci is not None:
        base["bank_prior_correct_index"] = bank_prior_ci
        if bank_prior_prov:
            base["bank_answer_provenance"] = bank_prior_prov
        if final_ci is not None:
            base["bank_answer_agrees_claude"] = bank_prior_ci == final_ci
        elif identical:
            base["bank_answer_agrees_claude"] = False

    cap_src = (base.get("_capture_source") or "udemy").strip().lower()
    capture_prov = "odoo" if cap_src in ("odoo", "odoo_web", "website") else "udemy"
    if claude_called and ms in CLAUDE_OK_SOURCES and final_ci is not None:
        base["correct_suggestion_source"] = "claude"
        sug = "claude"
    elif ui_confirmed and vision_ci is not None and final_ci == vision_ci and not claude_called:
        base["correct_suggestion_source"] = capture_prov
        sug = capture_prov
    elif claude_called and (final_ci is None or ms in CLAUDE_FAIL_SOURCES):
        base["correct_suggestion_source"] = "aucune"
        sug = ""
    else:
        base["correct_suggestion_source"] = ""
        sug = ""

    base["suggested_correct_index"] = final_ci
    if sug in ("udemy", "odoo", "claude"):
        base["suggested_correct_source"] = sug
    elif final_ci is not None:
        base["suggested_correct_source"] = ""
    else:
        base["suggested_correct_source"] = ""

    base["bank_dup_title_only"] = False
    _attach_rag_similar(base, all_questions)
    return base


def merge_item_prefill_fields(item: dict) -> dict:
    """Champs optionnels sérialisés pour items.json (pending)."""
    return {
        k: item.get(k)
        for k in (
            "title",
            "answers",
            "correct_index",
            "correct_index_visible",
            "explication_udemy",
            "needs_question_image",
            "crop_rel",
            "_capture_source",
            "title_fr",
            "answers_fr",
            "explication_claude",
            "match_source",
            "in_banque",
            "existing_id",
            "suggested_correct_index",
            "suggested_correct_source",
            "bank_answer_line_fr",
            "bank_registered_answer",
            "bank_dup_title_only",
            "bank_answer_provenance",
            "correct_suggestion_source",
            "rag_similar",
            "rag_search_mode",
            "rag_prompt_min_score",
            "bank_duplicate_score",
            "bank_duplicate_reason",
            "bank_prior_correct_index",
            "bank_answer_agrees_claude",
        )
        if k in item
    }
