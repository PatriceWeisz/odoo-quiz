#!/usr/bin/env python3
"""Thématiques de la banque — basées sur le VRAI champ `module` des questions.

Remplace l'ancienne inférence par mots-clés (`_infer_odoo_topic`) qui ignorait
le champ `module` (présent sur ~2581 questions générées). Fournit :

  - un catalogue catégorie → libellé FR et module → libellé FR (2 niveaux) ;
  - `resolve_module(q, inferred)` : module réel, sinon module inféré ;
  - `infer_modules(questions)` : inférence du module pour les questions sans
    `module` (Udemy / anciennes), via kNN sur l'index d'embeddings déjà calculé
    (fallback mots-clés si l'index n'est pas disponible). Résultat caché.
  - `build_topic_tree(...)` : arbre {catégorie → [modules]} avec compteurs, pour
    peupler le menu déroulant groupé du front.

Les valeurs de filtre exposées au front :
  - "__all__"            : toutes les questions
  - "mod:<module>"       : un module précis (ex. "mod:sales/point_of_sale")
  - "cat:<category_key>" : toute une catégorie (ex. "cat:sales")
"""

from __future__ import annotations

import hashlib
import re
import threading
from typing import Any

# ---------------------------------------------------------------------------
# Catalogue de libellés (FR)
# ---------------------------------------------------------------------------

# Préfixe de module (avant "/") → libellé FR de la grande catégorie.
# Les modules sans "/" forment leur propre catégorie (gérés ci-dessous).
CATEGORY_LABELS: dict[str, str] = {
    "crm": "CRM",
    "sales": "Ventes",
    "purchase": "Achats",
    "accounting": "Comptabilité",
    "inventory_and_mrp": "Inventaire & Fabrication",
    "hr": "Ressources humaines",
    "services": "Services",
    "websites": "Sites web",
    "marketing": "Marketing",
    "productivity": "Productivité",
    "studio": "Studio",
}

# Module complet → libellé FR du sous-module (niveau 2).
MODULE_LABELS: dict[str, str] = {
    # racines (catégorie = elles-mêmes)
    "crm": "CRM",
    "sales": "Ventes (général)",
    "purchase": "Achats",
    "accounting": "Comptabilité",
    "hr": "RH (général)",
    "studio": "Studio",
    # inventory_and_mrp/*
    "inventory_and_mrp/inventory": "Inventaire",
    "inventory_and_mrp/manufacturing": "Fabrication (MRP)",
    "inventory_and_mrp/barcode": "Code-barres",
    "inventory_and_mrp/quality": "Qualité",
    "inventory_and_mrp/maintenance": "Maintenance",
    # services/*
    "services/helpdesk": "Assistance (Helpdesk)",
    "services/project": "Projet",
    "services/timesheets": "Feuilles de temps",
    "services/field_service": "Services sur site",
    "services/planning": "Planification",
    # hr/*
    "hr/payroll": "Paie",
    "hr/recruitment": "Recrutement",
    "hr/appraisals": "Évaluations",
    "hr/time_off": "Congés",
    # sales/*
    "sales/point_of_sale": "Point de vente",
    "sales/subscriptions": "Abonnements",
    "sales/rental": "Location",
    # websites/*
    "websites/website": "Site web",
    "websites/ecommerce": "eCommerce",
    # productivity/*
    "productivity/sign": "Signature",
    "productivity/spreadsheet": "Tableur",
    "productivity/discuss": "Discussion",
    "productivity/whatsapp": "WhatsApp",
    "productivity/ai": "IA",
    "productivity/calendar": "Calendrier",
    "productivity/knowledge": "Connaissances",
    "productivity/documents": "Documents",
    # marketing/*
    "marketing/surveys": "Sondages",
    "marketing/email_marketing": "Email Marketing",
    "marketing/social_marketing": "Marketing social",
    "marketing/events": "Événements",
    "marketing/marketing_automation": "Automatisation marketing",
    "marketing/sms_marketing": "SMS Marketing",
}

# Ordre d'affichage des catégories dans le menu.
CATEGORY_ORDER: tuple[str, ...] = (
    "crm", "sales", "purchase", "accounting",
    "inventory_and_mrp", "hr", "services",
    "websites", "marketing", "productivity", "studio",
)

UNCLASSIFIED_KEY = "_unclassified"
UNCLASSIFIED_LABEL = "Non classées (Udemy / anciennes)"

# Mots-clés de secours (si l'index d'embeddings n'est pas disponible).
# module → tuple de mots-clés (minuscule) recherchés dans titre + réponses.
MODULE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("crm", ("crm", "opportunity", "pipeline", "lead", "prospect")),
    ("sales/point_of_sale", ("point of sale", "pos ", "cash register", "caisse")),
    ("sales/subscriptions", ("subscription", "abonnement", "recurring")),
    ("sales/rental", ("rental", "location", "rent ")),
    ("sales", ("sale order", "sales order", "quotation", "devis", "commande client", "pricelist")),
    ("purchase", ("purchase order", "rfq", "request for quotation", "vendor", "fournisseur")),
    ("accounting", ("accounting", "invoice", "journal entry", "comptab", "facture", "tax", "reconcil")),
    ("inventory_and_mrp/manufacturing", ("manufacturing", "bom", "bill of material", "work order", "mrp", "fabrication")),
    ("inventory_and_mrp/barcode", ("barcode", "code-barres", "scanner")),
    ("inventory_and_mrp/quality", ("quality check", "quality control", "qualité")),
    ("inventory_and_mrp/maintenance", ("maintenance", "equipment", "preventive")),
    ("inventory_and_mrp/inventory", ("inventory", "stock", "warehouse", "inventaire", "entrepôt", "delivery", "putaway")),
    ("services/helpdesk", ("helpdesk", "ticket", "sla")),
    ("services/project", ("project ", "milestone", "task", "tâche", "projet")),
    ("services/timesheets", ("timesheet", "feuille de temps")),
    ("services/field_service", ("field service", "intervention sur site")),
    ("services/planning", ("planning", "shift", "planification")),
    ("hr/payroll", ("payroll", "payslip", "paie", "salaire")),
    ("hr/recruitment", ("recruitment", "candidate", "recrutement", "applicant")),
    ("hr/appraisals", ("appraisal", "évaluation", "feedback 360")),
    ("hr/time_off", ("time off", "leave", "congé", "absence")),
    ("hr", ("employee", "employé", "department", "contract", "ressources humaines")),
    ("websites/ecommerce", ("ecommerce", "e-commerce", "boutique", "online shop", "cart", "panier")),
    ("websites/website", ("website", "snippet", "theme", "visitor", "page builder", "site web")),
    ("productivity/sign", ("sign ", "signature", "signataire", "esign")),
    ("productivity/spreadsheet", ("spreadsheet", "tableur", "pivot", "formula")),
    ("productivity/discuss", ("discuss", "channel", "chatter", "mail gateway", "discussion")),
    ("productivity/whatsapp", ("whatsapp",)),
    ("productivity/ai", ("artificial intelligence", " ai ", "chatgpt", "ia ")),
    ("productivity/calendar", ("calendar", "calendrier", "meeting", "appointment")),
    ("productivity/knowledge", ("knowledge", "article", "workspace", "sous-article")),
    ("productivity/documents", ("documents", "dms", "workspace document")),
    ("marketing/surveys", ("survey", "sondage", "questionnaire", "respondent")),
    ("marketing/email_marketing", ("email marketing", "mailing list", "campagne email")),
    ("marketing/social_marketing", ("social marketing", "social media", "réseaux sociaux")),
    ("marketing/events", ("event", "événement", "attendee")),
    ("marketing/marketing_automation", ("marketing automation", "automation scenario", "automatisation")),
    ("marketing/sms_marketing", ("sms marketing", "sms ")),
    ("studio", ("studio", "custom field", "automated action", "custom view")),
)


def _norm_module(m: Any) -> str:
    return (str(m).strip() if m else "").strip("/")


def category_of(module: str) -> str:
    """Clé de catégorie (préfixe avant '/' ; le module lui-même si pas de '/')."""
    module = _norm_module(module)
    if not module:
        return UNCLASSIFIED_KEY
    return module.split("/", 1)[0]


def category_label(cat_key: str) -> str:
    if cat_key == UNCLASSIFIED_KEY:
        return UNCLASSIFIED_LABEL
    return CATEGORY_LABELS.get(cat_key, cat_key.replace("_", " ").title())


def module_label(module: str) -> str:
    module = _norm_module(module)
    if not module:
        return UNCLASSIFIED_LABEL
    if module in MODULE_LABELS:
        return MODULE_LABELS[module]
    # Dérivation par défaut depuis le slug du sous-module.
    leaf = module.split("/")[-1]
    return leaf.replace("_", " ").title()


# ---------------------------------------------------------------------------
# Inférence de module (questions sans `module` : Udemy / anciennes)
# ---------------------------------------------------------------------------

_infer_lock = threading.Lock()
_infer_cache: dict[str, dict[int, str]] = {}


def _bank_fingerprint(questions: list[dict]) -> str:
    h = hashlib.sha256()
    for q in questions:
        qid = q.get("id")
        mod = _norm_module(q.get("module"))
        h.update(f"{qid}\t{mod}\n".encode("utf-8"))
    return h.hexdigest()[:16]


def _keyword_infer(q: dict) -> str | None:
    parts = [q.get("title") or "", q.get("title_fr") or ""]
    for a in q.get("answers") or []:
        parts.append(a.get("value") or "")
        parts.append(a.get("value_fr") or "")
    blob = " ".join(parts).lower()
    best, score = None, 0
    for module, keys in MODULE_KEYWORDS:
        s = sum(1 for k in keys if k in blob)
        if s > score:
            score, best = s, module
    return best if score > 0 else None


def _vector_infer(
    questions: list[dict], known_module: dict[int, str], top_k: int = 7
) -> dict[int, str]:
    """kNN sur l'index d'embeddings déjà calculé.

    Pour chaque question sans module, vote pondéré (par cosine) du module des
    voisins les plus proches parmi les questions classées. Tout en numpy.
    """
    try:
        import numpy as np
        from bank_embeddings import get_bank_vector_index, warmup_bank_embeddings
    except Exception:
        return {}

    index = get_bank_vector_index()
    if index is None:
        try:
            warmup_bank_embeddings(questions)
            index = get_bank_vector_index()
        except Exception:
            index = None
    if index is None or getattr(index, "matrix", None) is None or index.ids.size == 0:
        return {}

    ids = index.ids  # (n,)
    matrix = index.matrix  # (n, dim) L2-normalisé
    row_of = {int(qid): i for i, qid in enumerate(ids.tolist())}

    known_rows, known_mods = [], []
    unknown_rows, unknown_ids = [], []
    for q in questions:
        qid = q.get("id")
        if not isinstance(qid, int) and not (isinstance(qid, str) and qid.lstrip("-").isdigit()):
            continue
        qid = int(qid)
        r = row_of.get(qid)
        if r is None:
            continue
        if qid in known_module:
            known_rows.append(r)
            known_mods.append(known_module[qid])
        else:
            unknown_rows.append(r)
            unknown_ids.append(qid)

    if not known_rows or not unknown_rows:
        return {}

    ref = matrix[known_rows]  # (k, dim)
    qry = matrix[unknown_rows]  # (u, dim)
    sims = qry @ ref.T  # (u, k) cosine (vecteurs normalisés)

    k = min(top_k, ref.shape[0])
    out: dict[int, str] = {}
    for i, qid in enumerate(unknown_ids):
        row = sims[i]
        top_idx = np.argpartition(row, -k)[-k:]
        votes: dict[str, float] = {}
        for j in top_idx:
            sc = float(row[j])
            if sc <= 0:
                continue
            mod = known_mods[j]
            votes[mod] = votes.get(mod, 0.0) + sc
        if votes:
            out[qid] = max(votes, key=votes.get)
    return out


def infer_modules(questions: list[dict]) -> dict[int, str]:
    """Retourne {id: module_inféré} pour les questions sans `module`. Caché."""
    fp = _bank_fingerprint(questions)
    with _infer_lock:
        cached = _infer_cache.get(fp)
        if cached is not None:
            return cached

    known_module: dict[int, str] = {}
    unknown: list[dict] = []
    for q in questions:
        qid = q.get("id")
        try:
            qid = int(qid)
        except (TypeError, ValueError):
            continue
        mod = _norm_module(q.get("module"))
        if mod:
            known_module[qid] = mod
        else:
            unknown.append(q)

    inferred: dict[int, str] = {}
    # 1) tentative vectorielle (plus précise)
    try:
        inferred = _vector_infer(questions, known_module)
    except Exception:
        inferred = {}
    # 2) fallback mots-clés pour ce qui reste
    for q in unknown:
        try:
            qid = int(q.get("id"))
        except (TypeError, ValueError):
            continue
        if qid in inferred:
            continue
        kw = _keyword_infer(q)
        if kw:
            inferred[qid] = kw

    with _infer_lock:
        _infer_cache[fp] = inferred
    return inferred


def resolve_module(q: dict, inferred: dict[int, str]) -> tuple[str, bool]:
    """Retourne (module, is_inferred). module == '' si non classable."""
    mod = _norm_module(q.get("module"))
    if mod:
        return mod, False
    try:
        qid = int(q.get("id"))
    except (TypeError, ValueError):
        qid = None
    if qid is not None and qid in inferred:
        return inferred[qid], True
    return "", False


# ---------------------------------------------------------------------------
# Arbre thématique (catégorie → modules) avec compteurs
# ---------------------------------------------------------------------------

def build_topic_tree(questions: list[dict], inferred: dict[int, str]) -> list[dict]:
    """Construit l'arbre pour le menu groupé.

    Retour : liste ordonnée de catégories
      [{key, label, count, value:"cat:<key>",
        modules:[{key, label, count, value:"mod:<module>", inferred_count}]}]
    """
    cat_counts: dict[str, int] = {}
    mod_counts: dict[str, int] = {}
    mod_inferred: dict[str, int] = {}
    for q in questions:
        mod, is_inf = resolve_module(q, inferred)
        if not mod:
            cat_counts[UNCLASSIFIED_KEY] = cat_counts.get(UNCLASSIFIED_KEY, 0) + 1
            continue
        cat = category_of(mod)
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
        mod_counts[mod] = mod_counts.get(mod, 0) + 1
        if is_inf:
            mod_inferred[mod] = mod_inferred.get(mod, 0) + 1

    ordered_cats = [c for c in CATEGORY_ORDER if c in cat_counts]
    ordered_cats += sorted(c for c in cat_counts if c not in CATEGORY_ORDER and c != UNCLASSIFIED_KEY)
    if UNCLASSIFIED_KEY in cat_counts:
        ordered_cats.append(UNCLASSIFIED_KEY)

    tree: list[dict] = []
    for cat in ordered_cats:
        mods = sorted(
            (m for m in mod_counts if category_of(m) == cat),
            key=lambda m: (-mod_counts[m], module_label(m)),
        )
        node = {
            "key": cat,
            "label": category_label(cat),
            "count": cat_counts.get(cat, 0),
            "value": "cat:" + cat,
            "modules": [
                {
                    "key": m,
                    "label": module_label(m),
                    "count": mod_counts[m],
                    "inferred_count": mod_inferred.get(m, 0),
                    "value": "mod:" + m,
                }
                for m in mods
            ],
        }
        tree.append(node)
    return tree


def full_catalog() -> list[dict]:
    """Catalogue complet (statique) catégorie → modules, pour l'éditeur.

    Indépendant des données : liste TOUS les modules connus (MODULE_LABELS),
    afin de pouvoir (ré)assigner n'importe quel module à une question.
    """
    cats: dict[str, list[str]] = {}
    for module in MODULE_LABELS:
        cats.setdefault(category_of(module), []).append(module)
    ordered = [c for c in CATEGORY_ORDER if c in cats]
    ordered += sorted(c for c in cats if c not in CATEGORY_ORDER)
    out: list[dict] = []
    for cat in ordered:
        mods = sorted(cats[cat], key=module_label)
        out.append(
            {
                "key": cat,
                "label": category_label(cat),
                "modules": [{"value": m, "label": module_label(m)} for m in mods],
            }
        )
    return out


def matches_topic_filter(q: dict, inferred: dict[int, str], topic_filter: str) -> bool:
    """Teste si une question correspond au filtre (mod:/cat:/__all__/legacy)."""
    if not topic_filter or topic_filter == "__all__":
        return True
    mod, _ = resolve_module(q, inferred)
    if topic_filter.startswith("mod:"):
        return mod == topic_filter[4:]
    if topic_filter.startswith("cat:"):
        target = topic_filter[4:]
        if target == UNCLASSIFIED_KEY:
            return not mod
        return bool(mod) and category_of(mod) == target
    # rétro-compat : ancien filtre par libellé exact
    return module_label(mod) == topic_filter if mod else False
