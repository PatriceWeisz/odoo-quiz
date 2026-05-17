#!/usr/bin/env python3
"""Prompts vision pour extraire une question quiz depuis une capture (Udemy ou site Odoo)."""

from __future__ import annotations

VISION_PROMPT_UDEMY = """Tu analyses une capture d'écran d'un quiz Udemy / formation en ligne (question à choix multiples).

Extrais UNIQUEMENT un objet JSON valide (sans texte avant ou après), avec exactement ces clés :
{
  "no_quiz_content": false ou true,
  "no_quiz_reason": "phrase courte en français si no_quiz_content est true, sinon chaîne vide",
  "title": "texte exact de la question en anglais (comme sur l'écran)",
  "answers": ["option1", "option2", ...],
  "correct_index": null ou entier 1-based (1 = première option) — voir règles ci-dessous avec correct_index_visible,
  "correct_index_visible": true ou false,
  "explication_udemy": "texte d'explication visible sous la question s'il y en a une, sinon chaîne vide",
  "needs_question_image": false ou true,
  "crop_rel": null ou {"left": 0.0, "top": 0.0, "width": 0.0, "height": 0.0}
}

Si la capture ne montre **pas** une question à choix multiples en cours (catalogue de cours, page d'accueil Udemy, lecteur vidéo sans quiz, écran de chargement, paramètres, certificat sans question, etc.) :
- mets **"no_quiz_content": true**
- mets **"no_quiz_reason"** : ce que vous voyez à la place (ex. « liste des leçons », « écran de connexion »)
- mets **"title": ""** et **"answers": []**
- ne invente pas de question ni d'options.

Sinon mets **"no_quiz_content": false** et **"no_quiz_reason": ""**, puis extrais la question normalement.

Règles :
- "answers" : toutes les options visibles, dans l'ordre affiché, texte exact.
- "correct_index_visible" : mets **true** uniquement si la capture montre **clairement** quelle option est la réponse (cases à cocher / radio **visuellement actives**, surlignage « votre réponse », pastille de résultat, correction affichée après soumission, etc.).
- "correct_index" :
  - Si correct_index_visible est **true** : indique le numéro (1-based) de l’option **ainsi montrée** sur l’écran.
  - Si correct_index_visible est **false** : mets **obligatoirement null** pour correct_index — **ne devine pas** la bonne réponse par raisonnement, même si tu la connais (sur Udemy avant notation, aucune option n’est en général « la bonne » côté interface).
- Si plusieurs choix sont cochés ou c’est ambigu : correct_index_visible false et correct_index null.
- Dans le titre, si l'écran montre des termes entre guillemets (ex. "Control Policy"), n'utilise **pas** le caractère `"` à l'intérieur de la chaîne JSON : utilise des **apostrophes '** ou **échappe** chaque guillemet avec \\".
- "needs_question_image" : mets **true** si répondre exige de voir un élément **non entièrement décrit** dans le titre, par exemple :
  - tableau comptable (trial balance, balance sheet, grand livre, etc.) ;
  - **fenêtre / pop-up / message d'avertissement Odoo** affiché sur la capture alors que la question dit « the following warning/message/dialog », « this button », « shown below », etc. ;
  - capture d'écran, icône ou bouton montré visuellement.
  Mets **false** seulement si le titre + les options suffisent sans regarder l'image.
- "crop_rel" : uniquement si needs_question_image est **true**. Rectangle serré autour de la zone utile (tableau, boîte de dialogue d'avertissement, zone UI citée) — **pas** les boutons radio ni le bandeau Udemy. Coordonnées **normalisées** (0 à 1). Si la zone est petite ou incertaine, mets **null** (capture entière enregistrée). Si needs_question_image est false, mets **null** pour crop_rel.
- Les valeurs numériques de crop_rel doivent être des nombres JSON (pas de chaînes).
- Pas de markdown autour du JSON, pas de commentaires JSON, pas de virgule finale avant ] ou }.
"""

VISION_PROMPT_ODOO = """Tu analyses une capture d'écran d'un **quiz Odoo** (site odoo.com eLearning / slides, certification, ou module Sondages sur une instance Odoo).

L'interface peut différer d'Udemy : cartes d'options, boutons radio, listes, barre de progression, numéro « Question X / Y », bandeau vert/rouge après correction, texte « Correct » / « Incorrect », feedback sous la question.

**Format de sortie (sans markdown) :**
- **Une seule** question visible → un **objet JSON**.
- **Plusieurs** questions distinctes sur la **même** capture (cas fréquent : **2, 3 ou 4** énoncés empilés sur une page, ex. section Sales/CRM) → un **tableau JSON** d'objets, **un objet par question**, dans l'ordre **de haut en bas** à l'écran.
- Ne fusionne **jamais** deux questions en une seule : chaque objet n'a que **ses** options (pas celles de la question voisine).

Chaque objet a exactement ces clés :
{
  "no_quiz_content": false ou true,
  "no_quiz_reason": "phrase courte en français si no_quiz_content est true, sinon chaîne vide",
  "title": "texte exact de la question en anglais (comme sur l'écran)",
  "answers": ["option1", "option2", ...],
  "correct_index": null ou entier 1-based (1 = première option) — voir règles avec correct_index_visible,
  "correct_index_visible": true ou false,
  "explication_udemy": "texte d'explication ou de feedback visible (panneau après réponse, message de correction Odoo), sinon chaîne vide",
  "needs_question_image": false ou true,
  "crop_rel": null ou {"left": 0.0, "top": 0.0, "width": 0.0, "height": 0.0}
}

Si la capture ne montre **aucune** question de quiz (accueil odoo.com, liste des cours, page de connexion, menu latéral seul, écran de chargement, etc.) :
- renvoie **un seul objet** avec **"no_quiz_content": true**, **"no_quiz_reason"**, **"title": ""**, **"answers": []**.

Sinon extrais **chaque** question visible — parcourez **toute** la capture du haut jusqu’au bouton « Soumettre » / « Continuer » (souvent **4** questions sur une page ; parfois 2 ou 3). **Comptez** les blocs distincts (énoncé + options radio) : le tableau JSON doit avoir **autant d’objets que de questions visibles** (maximum **4**). Ne vous arrêtez pas après 2 questions si d’autres sont visibles plus bas.

**INTERDIT** : ne mets **jamais** no_quiz_content à true parce qu'il y a **plusieurs** questions sur la même page — c'est le cas normal : renvoie un **tableau** avec un objet par question.

Règles (pour chaque objet) :
- "title" : l'intitulé de **cette** question uniquement, pas le titre du cours ni le menu Odoo. Ignore navigation (logo, fil d'Ariane, « Rejoindre ce cours », bouton « Continuer »).
- "answers" : uniquement les options de **cette** question, dans l'ordre affiché, texte exact (pas « Submit », « Continue », « Retry », « Continuer »).
- "correct_index_visible" : **true** seulement si la capture montre clairement la bonne option **de cette question** (coche violette/verte, surlignage, icône ✓ sur une carte).
- "correct_index" : si correct_index_visible est **true**, numéro 1-based de l'option ainsi indiquée ; sinon **null**. Ne mets **jamais** correct_index sur « I don't know » / « Je ne sais pas » sauf si cette option est clairement cochée comme réponse de l'utilisateur (rare).
- "explication_udemy" : feedback visible **sous cette question** seulement.
- "needs_question_image" : **true** si répondre exige un visuel **de cette question** (capture UI Odoo sous l'énoncé, tableau, schéma) non entièrement décrit dans le titre.
- "crop_rel" : si needs_question_image, rectangle **normalisé (0–1) sur toute la capture** englobant le bloc utile **de cette question** (énoncé + capture UI + ses options si pertinent) ; sinon null. Si plusieurs questions sont empilées (2 à 4), chaque crop_rel doit couvrir **uniquement** le bloc de **cette** question (zones **disjointes**).
- Guillemets dans le titre : apostrophes ' ou échappement \\" dans le JSON.
- Pas de markdown, pas de commentaires JSON, pas de virgule finale avant ] ou }.
"""

VISION_PROMPT = VISION_PROMPT_UDEMY  # rétrocompatibilité

MAX_QUESTIONS_PER_CAPTURE = 4

VISION_PROMPT_ODOO_MULTI_RETRY = """La capture est une page de quiz **Odoo** (odoo.com eLearning) avec **plusieurs questions empilées** sur le même écran (souvent **3 ou 4**, parfois 2).

Relis **toute** l'image du haut jusqu'au bouton « Soumettre » / « Continuer ». Tu as peut-être omis des questions en bas de page.

Si tu as indiqué no_quiz_content parce qu'il n'y a pas « une seule » question : c'était une erreur. Extrais **toutes** les questions visibles — **4** blocs distincts = **4** objets dans le tableau.

Renvoie UNIQUEMENT un **tableau JSON** (sans markdown), un objet par question (ordre haut → bas). Chaque objet :
{
  "no_quiz_content": false,
  "no_quiz_reason": "",
  "title": "énoncé exact en anglais",
  "answers": ["option1", "option2", ...],
  "correct_index": null ou entier 1-based,
  "correct_index_visible": true ou false,
  "explication_udemy": "",
  "needs_question_image": false ou true,
  "crop_rel": null ou {"left": 0.0, "top": 0.0, "width": 0.0, "height": 0.0}
}

Règles : uniquement les options de **cette** question ; pas de bouton Continuer ; maximum 4 questions ; no_quiz_content true **uniquement** si aucune question de quiz n'est visible.
"""


def get_vision_prompt(capture_source: str = "udemy") -> str:
    """capture_source : 'udemy' | 'odoo' (site web / eLearning)."""
    if (capture_source or "").strip().lower() in ("odoo", "odoo_web", "website"):
        return VISION_PROMPT_ODOO
    return VISION_PROMPT_UDEMY


class CaptureMultiQuestionMisreadError(Exception):
    """Le modèle a traité une capture multi-questions comme « sans quiz »."""

    def __init__(self, reason: str = "", capture_source: str = "udemy"):
        self.reason = (reason or "").strip()
        self.capture_source = (capture_source or "udemy").strip().lower()

    def user_message(self) -> str:
        return (
            "La capture contient plusieurs questions Odoo sur la même page. "
            "Sélectionnez « Odoo (site web) » si besoin, puis relancez « Analyser la capture » — "
            "une fiche sera créée par question (2 ou 3)."
        )


class CaptureNoQuizContentError(Exception):
    """La capture ne contient pas de question/réponses exploitables."""

    def __init__(self, reason: str = "", capture_source: str = "udemy"):
        self.reason = (reason or "").strip()
        self.capture_source = (capture_source or "udemy").strip().lower()
        super().__init__(self.user_message())

    def user_message(self) -> str:
        is_odoo = self.capture_source in ("odoo", "odoo_web", "website", "elearning", "slides")
        where = "le quiz Odoo (odoo.com / eLearning)" if is_odoo else "le quiz Udemy"
        base = (
            f"Cette capture ne contient pas de question à choix multiples visible pour {where}. "
            f"Affichez l’écran avec l’énoncé et au moins deux options de réponse, puis recapturez ou recollez l’image."
        )
        if self.reason:
            return f"{base}\n\nCe que montre la capture : {self.reason}"
        return base


def _vision_item_declares_no_quiz(it: dict) -> bool:
    if not isinstance(it, dict):
        return True
    flag = it.get("no_quiz_content")
    if flag is True:
        return True
    if flag is False:
        return False
    title = (it.get("title") or "").strip()
    answers = it.get("answers")
    if not title and (not isinstance(answers, list) or len(answers) < 2):
        return True
    return False


def _vision_item_no_quiz_reason(it: dict) -> str:
    if not isinstance(it, dict):
        return ""
    for key in ("no_quiz_reason", "reason"):
        v = it.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def no_quiz_reason_indicates_multi_question_misread(reason: str) -> bool:
    """Le modèle a refusé l'extraction alors que plusieurs questions sont visibles."""
    low = (reason or "").lower()
    if not low:
        return False
    needles = (
        "plusieurs questions",
        "multiple questions",
        "plusieurs énoncés",
        "2 questions",
        "3 questions",
        "deux questions",
        "trois questions",
        "même page",
        "same page",
        "simultan",
        "empil",
        "pas une seule",
        "not a single",
        "not one single",
        "isolée",
        "isolated",
        "odoo",
        "elearning",
        "e-learning",
        "functional certification",
    )
    return any(n in low for n in needles)


def _validation_error_suggests_no_quiz(msg: str) -> bool:
    low = (msg or "").lower()
    return (
        "'title' non vide" in low
        or "moins de 2" in low
        or "au moins 2 éléments" in low
        or "chaque réponse doit être un texte non vide" in low
    )


def coerce_vision_payload_to_item_dicts(parsed) -> list[dict]:
    """Normalise objet unique, tableau, ou enveloppe {questions: [...]}."""
    if isinstance(parsed, list):
        return [x for x in parsed if isinstance(x, dict)]
    if isinstance(parsed, dict):
        for key in ("questions", "items", "quiz_items"):
            inner = parsed.get(key)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
        return [parsed]
    return []


def items_from_vision_payload(parsed, capture_source: str = "udemy") -> list[dict]:
    """
    Transforme la réponse vision en items validés.
    Lève CaptureNoQuizContentError si la capture n’a pas de question/réponses.
    """
    from import_udemy import validate_udemy_item

    cap_src = (capture_source or "udemy").strip().lower()
    cap_src = "odoo" if cap_src in ("odoo", "odoo_web", "website", "elearning", "slides") else "udemy"

    def one(it: dict, index: int | None = None) -> dict:
        if _vision_item_declares_no_quiz(it):
            reason = _vision_item_no_quiz_reason(it)
            if no_quiz_reason_indicates_multi_question_misread(reason):
                raise CaptureMultiQuestionMisreadError(reason, cap_src)
            raise CaptureNoQuizContentError(reason, cap_src)
        try:
            return {**validate_udemy_item(it, index), "_capture_source": cap_src}
        except ValueError as e:
            if _validation_error_suggests_no_quiz(str(e)):
                raise CaptureNoQuizContentError("", cap_src) from e
            raise

    raw_items = coerce_vision_payload_to_item_dicts(parsed)
    if not raw_items:
        raise CaptureNoQuizContentError("", cap_src)

    out: list[dict] = []
    skipped_no_quiz = 0
    for i, it in enumerate(raw_items):
        if _vision_item_declares_no_quiz(it):
            skipped_no_quiz += 1
            continue
        out.append(one(it, i))

    if not out:
        reason = ""
        if skipped_no_quiz == 1 and raw_items:
            reason = _vision_item_no_quiz_reason(raw_items[0])
        if no_quiz_reason_indicates_multi_question_misread(reason):
            raise CaptureMultiQuestionMisreadError(reason, cap_src)
        raise CaptureNoQuizContentError(reason, cap_src)

    if len(out) > MAX_QUESTIONS_PER_CAPTURE:
        out = out[:MAX_QUESTIONS_PER_CAPTURE]
    return out


def vision_extract_items_from_capture(
    image_path: str, capture_source: str = "udemy"
) -> tuple[list[dict], str, bool]:
    """
    Extrait les questions via vision. Retourne (items, source_effective, auto_switched).
    Réessaie en mode Odoo si le modèle refuse une page multi-questions.
    """
    from quiz_llm import parse_json_value, run_prompt_with_images

    cap_src = (capture_source or "udemy").strip().lower()
    cap_src = "odoo" if cap_src in ("odoo", "odoo_web", "website", "elearning", "slides") else "udemy"
    auto_switched = False

    def _run(prompt: str, src: str) -> list[dict]:
        raw = run_prompt_with_images(prompt, [image_path])
        return items_from_vision_payload(parse_json_value(raw), src)

    try:
        items = _run(get_vision_prompt(cap_src), cap_src)
        if cap_src == "odoo" and len(items) < MAX_QUESTIONS_PER_CAPTURE:
            try:
                more = _run(VISION_PROMPT_ODOO_MULTI_RETRY, "odoo")
                if len(more) > len(items):
                    items = more
            except (CaptureNoQuizContentError, CaptureMultiQuestionMisreadError, ValueError, RuntimeError, OSError):
                pass
        return items, cap_src, False
    except CaptureMultiQuestionMisreadError:
        pass
    except CaptureNoQuizContentError as e:
        if not no_quiz_reason_indicates_multi_question_misread(e.reason):
            raise

    if cap_src != "odoo":
        items = _run(get_vision_prompt("odoo"), "odoo")
        return items, "odoo", True
    items = _run(VISION_PROMPT_ODOO_MULTI_RETRY, "odoo")
    return items, "odoo", False
