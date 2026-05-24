#!/usr/bin/env python3
"""Prédiction « test » de la réponse par Claude pour le quiz, en aveugle des
questions Udemy.

Objectif : mesurer en direct la qualité des prédictions de Claude sur les
questions Udemy, **sans** lui laisser voir les questions/réponses Udemy (sinon un
quasi-doublon de la banque suffirait à lui souffler la réponse). On reproduit la
logique du harnais d'éval `scripts/eval_suggestions.py` (leave-one-out +
`--rag-exclude-source udemy`) mais à l'unité, pour l'UI du quiz.

Le pourcentage de confiance affiché est **issu des questions embedded proches** :
on récupère les voisins vectoriels (banque privée d'Udemy + de la question
courante), chaque voisin « vote » pour l'option de la question courante la plus
proche de SA bonne réponse, pondéré par sa similarité. La confiance = masse de
vote portée par l'option choisie par Claude / masse totale.
"""

from __future__ import annotations

import difflib
from typing import Any

# Voisins considérés pour le calcul de confiance.
_NEIGHBOR_TOP_N = 8
_NEIGHBOR_MIN_SCORE = 0.12
# Un voisin ne « vote » que si sa bonne réponse ressemble assez à une option.
_ANSWER_MATCH_FLOOR = 0.34
# Repli si aucun voisin exploitable : confiance catégorielle -> bande %.
_CONF_BAND = {"haute": 88, "moyenne": 68, "basse": 48}
_CONF_RANK = {"basse": 0, "moyenne": 1, "haute": 2}


def effective_source(q: dict) -> str:
    """Source effective d'une question (correct_answer_source prioritaire)."""
    return str((q.get("correct_answer_source") or q.get("source") or "")).strip().lower()


def is_udemy(q: dict) -> bool:
    return effective_source(q) == "udemy"


def _options(q: dict) -> list[str]:
    return [str((a.get("value") or "")).strip() for a in (q.get("answers") or [])]


def _ground_truth_index(q: dict) -> int | None:
    cis = [i + 1 for i, a in enumerate(q.get("answers") or []) if a.get("is_correct")]
    return cis[0] if len(cis) == 1 else None


def _target_version(q: dict) -> str:
    tv = q.get("target_version")
    tv = str(tv).strip() if tv is not None and str(tv).strip() else ""
    return tv or "19.0"


def _text_sim(a: str, b: str) -> float:
    """Similarité texte 0–1 entre deux réponses (normalisation + Jaccard + ratio)."""
    from import_udemy import norm_title_key

    na, nb = norm_title_key(a), norm_title_key(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta = {w for w in na.split() if len(w) > 2}
    tb = {w for w in nb.split() if len(w) > 2}
    jacc = (len(ta & tb) / len(ta | tb)) if (ta and tb) else 0.0
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    return max(jacc, ratio)


def build_rag_bank(
    all_questions: list[dict],
    question_id: Any,
    *,
    exclude_udemy: bool = True,
) -> list[dict]:
    """Banque RAG pour la prédiction.

    Toujours en *leave-one-out* (la question testée est retirée, sinon elle serait
    son propre plus proche voisin). Selon `exclude_udemy` :
      - True  : retire AUSSI toutes les questions Udemy (test « en aveugle ») ;
      - False : conserve les autres Udemy (Claude peut s'appuyer sur la banque Udemy).
    """
    out: list[dict] = []
    for x in all_questions:
        if not isinstance(x, dict):
            continue
        if x.get("id") == question_id:
            continue
        if exclude_udemy and is_udemy(x):
            continue
        out.append(x)
    return out


def build_loo_bank(all_questions: list[dict], question_id: Any) -> list[dict]:
    """Compat : banque RAG sans aucune Udemy ni la question testée."""
    return build_rag_bank(all_questions, question_id, exclude_udemy=True)


def _neighbor_confidence(
    title: str,
    options: list[str],
    loo_bank: list[dict],
    predicted_index: int | None,
) -> dict[str, Any]:
    """Vote pondéré des voisins embedded sur les options de la question courante."""
    from bank_rag import find_similar_bank_questions

    query = title.strip()
    opt_join = " ".join(o for o in options if o)
    if opt_join:
        query = f"{query}\n{opt_join}"

    neighbors = find_similar_bank_questions(
        query, loo_bank, top_n=_NEIGHBOR_TOP_N, min_score=_NEIGHBOR_MIN_SCORE
    )

    votes = [0.0] * len(options)
    used: list[dict[str, Any]] = []
    for nb in neighbors:
        sim = float(nb.get("score") or 0.0)
        correct_text = (nb.get("correct_text") or "").strip()
        if not correct_text or sim <= 0:
            continue
        best_j, best_sim = -1, 0.0
        for j, opt in enumerate(options):
            s = _text_sim(correct_text, opt)
            if s > best_sim:
                best_sim, best_j = s, j
        if best_j < 0 or best_sim < _ANSWER_MATCH_FLOOR:
            continue
        votes[best_j] += sim
        used.append(
            {
                "id": nb.get("id"),
                "title": nb.get("title"),
                "similarity": round(sim, 3),
                "correct_text": correct_text,
                "maps_to_option": best_j + 1,
                "match": round(best_sim, 3),
            }
        )

    total = sum(votes)
    distribution = []
    if total > 0:
        for j, opt in enumerate(options):
            distribution.append(
                {"option_index": j + 1, "pct": round(votes[j] / total * 100, 1)}
            )

    pct = None
    if total > 0 and predicted_index and 1 <= predicted_index <= len(options):
        pct = round(votes[predicted_index - 1] / total * 100, 1)

    return {
        "pct": pct,
        "total_weight": round(total, 3),
        "n_neighbors_voting": len(used),
        "distribution": distribution,
        "neighbors": used,
    }


def predict_quiz_answer(
    question: dict,
    all_questions: list[dict],
    *,
    escalate: bool = False,
    exclude_udemy: bool = True,
) -> dict[str, Any]:
    """Prédit la réponse de Claude (en aveugle d'Udemy) + confiance voisins.

    Retourne un dict prêt pour JSON :
      predicted_index, predicted_letter, confiance (catégoriel), confidence_pct,
      confidence_source ('voisins' | 'modèle'), correct_index (vérité terrain),
      is_correct, justification, model, escalated, neighbor_distribution, neighbors.
    """
    from app.llm import (
        escalation_model,
        reponse_to_correct_index,
        suggest_answer,
    )
    from app.rag import build_context

    title = (question.get("title") or "").strip()
    options = _options(question)
    qid = question.get("id")
    tv = _target_version(question)
    n_opts = len([o for o in options if o])
    if not title or n_opts < 2:
        raise ValueError("Question non exploitable (titre vide ou < 2 options).")

    rag_bank = build_rag_bank(all_questions, qid, exclude_udemy=exclude_udemy)

    ctx = build_context(title, options, question_bank=rag_bank, target_version=tv)
    sugg, meta = suggest_answer(
        title, options, ctx, question_id=qid, target_version=tv
    )
    predicted_index = reponse_to_correct_index(sugg.reponse, len(options))
    confiance = sugg.confiance
    used_model = meta.get("model") or "?"
    justification = (sugg.justification or "").strip()
    escalated = False

    esc_model = escalation_model() if escalate else ""
    if esc_model and (confiance != "haute" or predicted_index is None):
        try:
            sugg2, meta2 = suggest_answer(
                title, options, ctx, question_id=qid, target_version=tv, model=esc_model
            )
            ci2 = reponse_to_correct_index(sugg2.reponse, len(options))
            if ci2 is not None and _CONF_RANK.get(sugg2.confiance, 0) >= _CONF_RANK.get(
                confiance, 0
            ):
                predicted_index = ci2
                confiance = sugg2.confiance
                justification = (sugg2.justification or "").strip()
                used_model = meta2.get("model") or esc_model
                escalated = True
        except Exception:
            pass  # garde la prédiction de base si l'escalade échoue

    neigh = _neighbor_confidence(title, options, rag_bank, predicted_index)
    if neigh["pct"] is not None:
        confidence_pct = neigh["pct"]
        confidence_source = "voisins"
    else:
        confidence_pct = _CONF_BAND.get(confiance, 60)
        confidence_source = "modèle"

    truth = _ground_truth_index(question)
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    predicted_letter = (
        letters[predicted_index - 1]
        if predicted_index and 1 <= predicted_index <= len(letters)
        else None
    )

    return {
        "id": qid,
        "predicted_index": predicted_index,
        "predicted_letter": predicted_letter,
        "confiance": confiance,
        "confidence_pct": confidence_pct,
        "confidence_source": confidence_source,
        "correct_index": truth,
        "is_correct": (predicted_index == truth) if (predicted_index and truth) else None,
        "justification": justification,
        "model": used_model,
        "escalated": escalated,
        "neighbor_distribution": neigh["distribution"],
        "n_neighbors_voting": neigh["n_neighbors_voting"],
        "neighbors": neigh["neighbors"],
        "target_version": tv,
        "exclude_udemy": exclude_udemy,
        "rag_bank_size": len(rag_bank),
    }
