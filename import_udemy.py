#!/usr/bin/env python3
"""Fusion d'items type Udemy dans questions.json (sans doublon sur titre normalisé)."""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).parent

try:
    import generate_explanations as ge
except ImportError:
    ge = None

from question_images import (
    _normalize_crop_rel,
    has_valid_question_image,
    normalize_question_media_rel,
    save_question_image_from_screenshot,
)


def _coerce_question_id_for_image(val: Any) -> int | None:
    """Accepte int ou chaîne numérique (JSON) pour l’écriture du fichier image."""
    if val is None or isinstance(val, bool):
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def norm_title_key(title: str) -> str:
    s = unicodedata.normalize("NFC", (title or "").strip().lower())
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^\w\sàâäéèêëïîôùûüçœæ]", "", s, flags=re.IGNORECASE)
    return s.strip()


def max_answer_id(questions: list) -> int:
    m = 0
    for q in questions:
        for a in q.get("answers", []):
            try:
                m = max(m, int(a.get("id", 0)))
            except (TypeError, ValueError):
                pass
    return m


def next_udemy_question_id(questions: list) -> int:
    negs = [q["id"] for q in questions if isinstance(q.get("id"), int) and q["id"] < 0]
    if not negs:
        return -1
    return min(negs) - 1


def build_seen_keys(questions: list) -> set[str]:
    return {norm_title_key(q.get("title", "")) for q in questions if q.get("title")}


def title_suggests_balance_context(title: str) -> bool:
    """Heuristique : question qui s'appuie probablement sur un tableau / balance à l'écran."""
    t = (title or "").strip().lower()
    if not t:
        return False
    if "trial balance" in t:
        return True
    if "balance sheet" in t:
        return True
    if "chart of accounts" in t or "chart of account" in t:
        return True
    if "general ledger" in t:
        return True
    if "balance" in t and "account" in t and (
        "debit" in t or "credit" in t or "table" in t or "which" in t
    ):
        return True
    return False


def _norm_compare_line(s: str) -> str:
    """Comparaison souple (typographie) pour détecter une réponse = copie du titre."""
    from import_preview_enrich import normalize_text_for_merge_compare

    return normalize_text_for_merge_compare(s or "")


def answer_duplicates_question_text(
    title_en: str,
    answer_en: str,
    *,
    title_fr: str = "",
    answer_fr: str = "",
) -> bool:
    """True si le texte de réponse est (quasi) identique au titre question."""
    te = _norm_compare_line(title_en)
    ae = _norm_compare_line(answer_en)
    if te and ae == te:
        return True
    tf = _norm_compare_line(title_fr)
    af = _norm_compare_line(answer_fr)
    if tf and af == tf:
        return True
    if te and af == te:
        return True
    if tf and ae == tf:
        return True
    return False


def strip_answer_options_duplicating_title(
    options: list[str],
    title_en: str,
    *,
    title_fr: str = "",
) -> list[str]:
    """Retire les options dont le texte EN recopie le titre (erreur fréquente d'extraction)."""
    out: list[str] = []
    for opt in options:
        o = str(opt).strip()
        if not o:
            continue
        if answer_duplicates_question_text(title_en, o, title_fr=title_fr, answer_fr=""):
            continue
        out.append(o)
    return out


def strip_capture_item_duplicate_answers(item: dict) -> dict:
    """Retire les options = titre ; réaligne answers_fr et correct_index (1-based)."""
    title = (item.get("title") or "").strip()
    title_fr = (item.get("title_fr") or "").strip()
    answers = [str(a).strip() for a in (item.get("answers") or [])]
    afr_in = item.get("answers_fr")
    afr = [str(x).strip() for x in afr_in] if isinstance(afr_in, list) else []
    kept_a: list[str] = []
    kept_fr: list[str] = []
    removed_before_correct = 0
    ci = item.get("correct_index")
    ci_i = ci if isinstance(ci, int) else None
    for i, a in enumerate(answers):
        fr = afr[i] if i < len(afr) else ""
        if answer_duplicates_question_text(title, a, title_fr=title_fr, answer_fr=fr):
            if ci_i is not None and i < ci_i - 1:
                removed_before_correct += 1
            continue
        kept_a.append(a)
        kept_fr.append(fr)
    out = dict(item)
    out["answers"] = kept_a
    if isinstance(afr_in, list):
        out["answers_fr"] = kept_fr
    if ci_i is not None:
        new_ci = ci_i - removed_before_correct
        if new_ci < 1 or new_ci > len(kept_a):
            out["correct_index"] = None
        else:
            out["correct_index"] = new_ci
    return out


def strip_question_duplicate_answers(q: dict) -> tuple[dict, list[int]]:
    """Retire les entrées answers[] qui recopient le titre. Retourne (question, indices supprimés)."""
    title = (q.get("title") or "").strip()
    title_fr = (q.get("title_fr") or "").strip()
    removed: list[int] = []
    kept: list[dict] = []
    for i, a in enumerate(q.get("answers") or []):
        if not isinstance(a, dict):
            continue
        if answer_duplicates_question_text(
            title,
            (a.get("value") or "").strip(),
            title_fr=title_fr,
            answer_fr=(a.get("value_fr") or "").strip(),
        ):
            removed.append(i)
            continue
        kept.append(a)
    if not removed:
        return q, removed
    out = dict(q)
    out["answers"] = kept
    return out, removed


def _title_suggests_image_question(title: str) -> bool:
    t = (title or "").strip().lower()
    if not t:
        return False
    hints = (
        "what is this button",
        "what is this?",
        "what is this ",
        "what does this button",
        "what does this?",
        "what does this ",
        "what does this warning",
        "what does this message",
        "what does this dialog",
        "what does this pop-up",
        "what does this popup",
        "when does odoo show the following",
        "when does odoo display the following",
        "when is the following",
        "which button",
        "which icon",
        "this button",
        "this icon",
        "this field",
        "this warning",
        "this message",
        "this dialog",
        "this pop-up",
        "this popup",
        "the following warning",
        "the following message",
        "the following dialog",
        "the following pop-up",
        "the following popup",
        "the following screenshot",
        "the following image",
        "the following screen",
        "shown in the",
        "shown below",
        "displayed below",
        "in the screenshot",
        "in the image",
        "on the screen",
        "warning message below",
        "à quoi sert ce bouton",
        "quel est ce bouton",
        "message suivant",
        "avertissement suivant",
        "fenêtre suivante",
        "capture suivante",
        "image suivante",
        "écran suivant",
        "cette fenêtre",
        "cette boîte de dialogue",
        "ce message d'avertissement",
        "ce message d’avertissement",
        "affiche-t-il le message",
        "affiche le message suivant",
    )
    return any(h in t for h in hints)


def title_requires_capture_image(title: str) -> bool:
    """Question qui exige de conserver la capture (tableau, UI, message d'avertissement, etc.)."""
    return _title_suggests_image_question(title) or title_suggests_balance_context(title)


def validate_udemy_item(it: dict, index: int | None = None) -> dict:
    lab = f"items[{index}]" if index is not None else "entrée"
    if not isinstance(it, dict):
        raise ValueError(f"{lab} : doit être un objet JSON.")
    title = (it.get("title") or "").strip()
    answers = it.get("answers")
    if not title or not isinstance(answers, list) or len(answers) < 2:
        raise ValueError(f"{lab} : 'title' non vide et 'answers' (liste d'au moins 2 éléments) requis.")
    it = strip_capture_item_duplicate_answers(it)
    opts = [str(a).strip() for a in it.get("answers") or []]
    if len(opts) < 2:
        raise ValueError(
            f"{lab} : après retrait des options recopiant le titre, il reste moins de 2 réponses."
        )
    ci = it.get("correct_index")
    if any(not x for x in opts):
        raise ValueError(f"{lab} : chaque réponse doit être un texte non vide.")
    if ci is not None:
        if not isinstance(ci, int) or ci < 1 or ci > len(opts):
            raise ValueError(f"{lab} : correct_index doit être entre 1 et {len(opts)} (ou absent / null).")
    expl = it.get("explication_udemy")
    if expl is None:
        expl = ""
    if not isinstance(expl, str):
        expl = str(expl)
    expl = expl.strip()
    raw_need = it.get("needs_question_image")
    heuristic_img = title_requires_capture_image(title)
    if raw_need is None:
        needs_img = heuristic_img
    else:
        # L’heuristique prime si le titre renvoie à un visuel non recopié dans le texte.
        needs_img = bool(raw_need) or heuristic_img
    crop = it.get("crop_rel")
    if crop is not None and not isinstance(crop, dict):
        raise ValueError(f"{lab} : 'crop_rel' doit être un objet ou absent.")
    crop_norm = _normalize_crop_rel(crop) if isinstance(crop, dict) else None
    if not needs_img:
        crop_norm = None
    raw_vis = it.get("correct_index_visible")
    if raw_vis is None:
        correct_index_visible: bool | None = None
    elif isinstance(raw_vis, bool):
        correct_index_visible = raw_vis
    else:
        correct_index_visible = None
    if correct_index_visible is False and ci is not None:
        ci = None
    return {
        "title": title,
        "title_fr": (it.get("title_fr") or "").strip(),
        "answers": opts,
        "answers_fr": it.get("answers_fr") if isinstance(it.get("answers_fr"), list) else [],
        "correct_index": ci,
        "correct_index_visible": correct_index_visible,
        "explication_udemy": expl,
        "needs_question_image": needs_img,
        "crop_rel": crop_norm,
        "image_url": ((it.get("image_url") or "").strip() or None) if needs_img else None,
    }


def manual_vision_fallback_udemy_item() -> dict:
    """Carte par défaut si l’API vision échoue : l’utilisateur saisit question, options et bonne réponse."""
    return validate_udemy_item(
        {
            "title": "(Vision unavailable) Paste the question in English as on the capture.",
            "answers": [
                "(Replace) First answer (EN)",
                "(Replace) Second answer (EN)",
            ],
            "correct_index": None,
            "correct_index_visible": False,
            "explication_udemy": "",
            "needs_question_image": False,
            "crop_rel": None,
        },
        0,
    )


def _correct_1based(q: dict) -> int | None:
    for j, a in enumerate(q.get("answers") or []):
        if a.get("is_correct"):
            return j + 1
    return None


def resolve_capture_saved_answer_source(item: dict, final_ci: int | None) -> str | None:
    """Provenance à enregistrer si l'utilisateur garde la suggestion (Udemy / Claude / banque) ; sinon utilisateur."""
    if final_ci is None:
        return None
    raw = item.get("suggested_correct_index")
    try:
        sug_i = int(raw) if raw is not None and str(raw).strip().isdigit() else None
    except (TypeError, ValueError):
        sug_i = None
    src = (item.get("suggested_correct_source") or "").strip()
    if sug_i is not None and sug_i == final_ci and src in ("udemy", "odoo", "claude", "user"):
        return src
    return "user"


def to_app_question(
    item: dict,
    qid: int,
    answer_ids: list[int],
    correct_index: int | None,
    *,
    correct_answer_source: str | None = None,
) -> dict:
    answers = []
    for j, val in enumerate(item["answers"]):
        is_ok = correct_index is not None and (j + 1) == correct_index
        answers.append(
            {
                "id": answer_ids[j],
                "value": val,
                "value_fr": "",
                "is_correct": bool(is_ok),
                "score": 0.0,
            }
        )
    expl = item.get("explication_udemy") or ""
    out = {
        "id": qid,
        "title": item["title"],
        "title_fr": "",
        "type": "simple_choice",
        "is_scored": True,
        "source": (
            "odoo_web"
            if (item.get("_capture_source") or "").strip().lower() in ("odoo", "odoo_web", "website")
            else "udemy"
        ),
        "topic": "",
        "explication_senedoo": expl,
        "explication_claude": "",
        "answers": answers,
        "question_image": "",
    }
    if correct_answer_source in ("udemy", "odoo", "claude", "user"):
        out["correct_answer_source"] = correct_answer_source
    elif correct_index is not None:
        out["correct_answer_source"] = "user"
    return out


def enrich_with_claude(q: dict) -> None:
    if ge is None:
        sys.exit("❌ Impossible d'importer generate_explanations.py.")
    parsed = ge.process_one(q)
    ge.apply_result(q, parsed)


def _question_index_by_id(questions: list, qid: int) -> int | None:
    for i, q in enumerate(questions):
        if q.get("id") == qid:
            return i
    return None


def _first_question_index_by_title_key(questions: list, key: str) -> int | None:
    if not key:
        return None
    for i, q in enumerate(questions):
        if norm_title_key(q.get("title", "")) == key:
            return i
    return None


def _apply_editor_translations_to_question(q: dict, item: dict) -> None:
    """Remplit FR / explications depuis l’éditeur (nouvelle question ou complément)."""
    q["title_fr"] = (item.get("title_fr") or "").strip()
    afr = item.get("answers_fr") or []
    for j, a in enumerate(q.get("answers", [])):
        a["value_fr"] = (str(afr[j]).strip() if j < len(afr) else "")
    q["explication_claude"] = (item.get("explication_claude") or "").strip()
    q["explication_senedoo"] = (item.get("explication_udemy") or "").strip()


def _apply_capture_update_existing_question(
    q: dict, item: dict, alloc_answer_ids: Callable[[int], list[int]],
) -> None:
    """Met à jour une entrée banque existante à partir des champs écran capture."""
    old_ci = _correct_1based(q)
    q["title"] = (item.get("title") or "").strip()
    q["title_fr"] = (item.get("title_fr") or "").strip()
    q["explication_senedoo"] = (item.get("explication_udemy") or "").strip()
    q["explication_claude"] = (item.get("explication_claude") or "").strip()
    opts = [str(x).strip() for x in (item.get("answers") or [])]
    opts = strip_answer_options_duplicating_title(
        opts, (item.get("title") or "").strip(), title_fr=(item.get("title_fr") or "").strip()
    )
    afr = list(item.get("answers_fr") or [])
    n = len(opts)
    ci = item.get("correct_index")
    old = q.get("answers") or []
    if len(old) == n and n > 0:
        for j in range(n):
            old[j]["value"] = opts[j]
            old[j]["value_fr"] = (str(afr[j]).strip() if j < len(afr) else "")
            old[j]["is_correct"] = bool(ci is not None and (j + 1) == ci)
        q["answers"] = old
    else:
        ids = alloc_answer_ids(n)
        q["answers"] = [
            {
                "id": ids[j],
                "value": opts[j],
                "value_fr": (str(afr[j]).strip() if j < len(afr) else ""),
                "is_correct": bool(ci is not None and (j + 1) == ci),
                "score": 0.0,
            }
            for j in range(n)
        ]

    new_ci = ci if isinstance(ci, int) else None
    prev_src = q.get("correct_answer_source")
    if not isinstance(prev_src, str) or prev_src not in ("udemy", "claude", "user"):
        prev_src = None
    hint_src = (item.get("suggested_correct_source") or "").strip()
    if new_ci is not None:
        if (
            not hint_src
            and old_ci is not None
            and new_ci == old_ci
            and prev_src in ("udemy", "claude", "user")
        ):
            q["correct_answer_source"] = prev_src
        else:
            q["correct_answer_source"] = resolve_capture_saved_answer_source(item, new_ci)
    else:
        q.pop("correct_answer_source", None)


def apply_capture_items_to_bank(
    items: list[dict],
    *,
    screenshot_path: str | None = None,
    use_claude_for_incomplete_new: bool = False,
    verbose: bool = False,
) -> dict:
    """Enregistre ou met à jour la banque depuis l’écran d’édition post-capture (formulaire)."""
    if ge is None:
        raise RuntimeError("generate_explanations.py introuvable.")
    data = ge.load()
    questions = data.get("questions", [])
    if not isinstance(questions, list):
        raise RuntimeError("questions.json : clé 'questions' invalide.")

    seen = build_seen_keys(questions)
    aid = max_answer_id(questions)
    qid_next = next_udemy_question_id(questions)

    to_add: list[dict] = []
    skipped: list[str] = []
    images_updated: list[int] = []
    updated_ids: list[int] = []
    image_warnings: list[str] = []

    def alloc_answer_ids(count: int) -> list[int]:
        nonlocal aid
        out = list(range(aid + 1, aid + 1 + count))
        aid += count
        return out

    for raw_item in items:
        item = strip_capture_item_duplicate_answers(raw_item)
        title = (item.get("title") or "").strip()
        answers = item.get("answers") or []
        if not title or len(answers) < 2:
            skipped.append("(titre vide ou moins de 2 réponses)")
            continue
        opts = [str(a).strip() for a in answers]
        if any(not x for x in opts):
            skipped.append("(réponse vide)")
            continue
        ci = item.get("correct_index")
        if ci is not None:
            if not isinstance(ci, int) or ci < 1 or ci > len(opts):
                skipped.append(f"(correct_index invalide : {ci!r})")
                continue

        key = norm_title_key(title)
        if not key:
            skipped.append("(titre vide après normalisation)")
            continue

        ex_raw = item.get("existing_id")
        ex_id: int | None = None
        if isinstance(ex_raw, int) and ex_raw > 0:
            ex_id = ex_raw
        elif isinstance(ex_raw, str) and ex_raw.strip().isdigit():
            ex_id = int(ex_raw.strip())

        force_new = bool(item.get("force_new_despite_bank_dup"))

        dup_idx: int | None = None
        if not force_new:
            if ex_id is not None:
                dup_idx = _question_index_by_id(questions, ex_id)
            if dup_idx is None and key in seen:
                dup_idx = _first_question_index_by_title_key(questions, key)

        if item.get("skip_new_question"):
            skipped.append(
                f"«{title[:100]}{'…' if len(title) > 100 else ''}» : non ajoutée à la banque (ignorée)."
            )
            continue

        if dup_idx is not None:
            if item.get("skip_bank_update"):
                skipped.append(
                    f"«{title[:100]}{'…' if len(title) > 100 else ''}» : banque inchangée (fusion ignorée)."
                )
                continue
            q = questions[dup_idx]
            _apply_capture_update_existing_question(q, item, alloc_answer_ids)
            qid_save = _coerce_question_id_for_image(q.get("id"))
            if screenshot_path and item.get("needs_question_image") and qid_save is not None:
                rel = ""
                err_note: str | None = None
                try:
                    rel = save_question_image_from_screenshot(
                        screenshot_path, qid_save, item.get("crop_rel"), True,
                        image_url=item.get("image_url"),
                    ) or ""
                except Exception as exc:
                    err_note = str(exc)
                if rel:
                    q["question_image"] = normalize_question_media_rel(rel) or rel
                    images_updated.append(qid_save)
                elif item.get("needs_question_image"):
                    if err_note:
                        image_warnings.append(
                            f"«{title[:80]}{'…' if len(title) > 80 else ''}» : image non enregistrée ({err_note})."
                        )
                    else:
                        image_warnings.append(
                            f"«{title[:80]}{'…' if len(title) > 80 else ''}» : image demandée mais fichier WebP non créé "
                            f"(vérifiez la capture ou le recadrage)."
                        )
            elif item.get("needs_question_image"):
                if not screenshot_path:
                    image_warnings.append(
                        f"«{title[:80]}{'…' if len(title) > 80 else ''}» : image demandée mais fichier capture introuvable (session expirée ?)."
                    )
                elif qid_save is None:
                    image_warnings.append(
                        f"«{title[:80]}{'…' if len(title) > 80 else ''}» : image demandée mais id question banque invalide."
                    )
            updated_ids.append(int(q["id"]))
            continue

        n = len(opts)
        new_aids = alloc_answer_ids(n)
        src = resolve_capture_saved_answer_source(item, ci)
        q = to_app_question(
            {**item, "title": title, "answers": opts},
            qid_next,
            new_aids,
            ci,
            correct_answer_source=src,
        )
        qid_next -= 1
        _apply_editor_translations_to_question(q, item)
        if screenshot_path and item.get("needs_question_image"):
            rel = ""
            err_note: str | None = None
            try:
                rel = save_question_image_from_screenshot(
                    screenshot_path, q["id"], item.get("crop_rel"), True,
                    image_url=item.get("image_url"),
                ) or ""
            except Exception as exc:
                err_note = str(exc)
            if rel:
                q["question_image"] = normalize_question_media_rel(rel) or rel
                images_updated.append(int(q["id"]))
            else:
                if err_note:
                    image_warnings.append(
                        f"«{title[:80]}{'…' if len(title) > 80 else ''}» : image non enregistrée ({err_note})."
                    )
                else:
                    image_warnings.append(
                        f"«{title[:80]}{'…' if len(title) > 80 else ''}» : image demandée mais fichier WebP non créé "
                        f"(vérifiez la capture ou le recadrage)."
                    )
        elif item.get("needs_question_image") and not screenshot_path:
            image_warnings.append(
                f"«{title[:80]}{'…' if len(title) > 80 else ''}» : image demandée mais fichier capture introuvable (session expirée ?)."
            )
        if use_claude_for_incomplete_new:
            from quiz_llm import llm_available

            if llm_available() and (
                not (q.get("title_fr") or "").strip()
                or not ge.has_correct(q)
                or not (q.get("explication_claude") or "").strip()
            ):
                if verbose:
                    print(f"🤖 Claude (complément) : {q['title'][:65]}…", flush=True)
                enrich_with_claude(q)
        to_add.append(q)
        seen.add(key)

    saved_any = False
    if to_add:
        questions.extend(to_add)
    if to_add or updated_ids or images_updated:
        data["questions"] = questions
        ge.save(data)
        saved_any = True

    return {
        "added_count": len(to_add),
        "updated_count": len(updated_ids),
        "skipped": skipped,
        "images_updated": images_updated,
        "image_warnings": image_warnings,
        "updated_ids": updated_ids,
        "saved": saved_any,
        "to_add": to_add,
    }


def merge_udemy_items(
    items: list[dict],
    *,
    dry_run: bool = False,
    use_claude: bool = False,
    verbose: bool = True,
    screenshot_path: str | None = None,
) -> dict:
    if ge is None:
        raise RuntimeError("generate_explanations.py introuvable.")
    data = ge.load()
    questions = data.get("questions", [])
    if not isinstance(questions, list):
        raise RuntimeError("questions.json : clé 'questions' invalide.")

    seen = build_seen_keys(questions)
    aid = max_answer_id(questions)
    qid = next_udemy_question_id(questions)

    to_add: list[dict] = []
    skipped: list[str] = []
    images_updated: list[int] = []

    for item in items:
        key = norm_title_key(item["title"])
        if not key:
            skipped.append("(titre vide après normalisation)")
            continue

        if key in seen:
            dup_idx = None
            for i, q in enumerate(questions):
                if norm_title_key(q.get("title", "")) == key:
                    dup_idx = i
                    break
            if (
                not dry_run
                and dup_idx is not None
                and screenshot_path
                and item.get("needs_question_image")
                and isinstance(questions[dup_idx].get("id"), int)
            ):
                ex = questions[dup_idx]
                if not has_valid_question_image(ex):
                    rel = save_question_image_from_screenshot(
                        screenshot_path,
                        ex["id"],
                        item.get("crop_rel"),
                        True,
                        image_url=item.get("image_url"),
                    )
                    if rel:
                        ex["question_image"] = normalize_question_media_rel(rel) or rel
                        images_updated.append(int(ex["id"]))
                        ge.save(data)
            skipped.append(item["title"][:120] + ("…" if len(item["title"]) > 120 else ""))
            continue

        seen.add(key)
        n = len(item["answers"])
        answer_ids = list(range(aid + 1, aid + 1 + n))
        aid += n
        q = to_app_question(
            item,
            qid,
            answer_ids,
            item.get("correct_index"),
            correct_answer_source=(
                "udemy" if item.get("correct_index") is not None else None
            ),
        )
        qid -= 1
        if not dry_run and screenshot_path and item.get("needs_question_image"):
            rel = save_question_image_from_screenshot(
                screenshot_path, q["id"], item.get("crop_rel"), True,
                image_url=item.get("image_url"),
            )
            if rel:
                q["question_image"] = normalize_question_media_rel(rel) or rel
        to_add.append(q)

    result: dict = {
        "added_count": len(to_add),
        "skipped": skipped,
        "images_updated": images_updated,
        "dry_run": dry_run,
        "saved": False,
        "to_add": to_add,
    }

    if dry_run:
        return result

    saved_any = bool(images_updated)

    if to_add:
        if use_claude:
            from quiz_llm import llm_available

            if not llm_available():
                raise RuntimeError(
                    "Aucun LLM disponible : installez le CLI `claude` ou configurez une clé API Anthropic."
                )

        for q in to_add:
            if use_claude:
                if verbose:
                    print(f"🤖 Claude : {q['title'][:65]}…", flush=True)
                enrich_with_claude(q)
            elif not ge.has_correct(q):
                pass

        questions.extend(to_add)
        data["questions"] = questions
        ge.save(data)
        saved_any = True

    result["saved"] = saved_any
    return result
