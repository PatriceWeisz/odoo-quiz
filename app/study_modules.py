#!/usr/bin/env python3
"""Source de vérité : périmètre de modules Odoo à étudier pour la certification.

Trois tiers de priorité (cf. briefing) :
  - **cert**  : 70 % du budget de génération (~2100 questions)
                — modules obligatoires de la cert officielle.
  - **tier1** : 20 % (~600 questions)
                — modules fréquents en pratique consultant, hors cert stricte.
  - **tier2** : 10 % (~300 questions)
                — modules occasionnels, complément de couverture.

Les **identifiants de modules** sont des "libellés cert" (utilisés dans le code
et les rapports). Quand le chemin URL réel sous odoo.com/documentation/<v>/
applications/ diffère, le mapping est dans `MODULE_URL_PATHS`.

V19_ONLY_MODULES : modules présents uniquement à partir d'Odoo 19.0.
"""

from __future__ import annotations

from typing import Final

STUDY_MODULES: Final[dict[str, list[str]]] = {
    "cert": [
        "crm",
        "sales",
        "purchase",
        "accounting",
        "inventory_and_mrp/inventory",
        "inventory_and_mrp/manufacturing",
        "services/project",
        "services/timesheets",
        "hr",
        "websites/website",
        "websites/ecommerce",
        "marketing/email_marketing",
        "marketing/marketing_automation",
        "marketing/sms_marketing",
        "marketing/surveys",
        "sales/point_of_sale",
        "productivity/spreadsheet",
        "studio",
        # v19 only :
        "productivity/ai",
        "productivity/knowledge",
    ],
    "tier1": [
        "productivity/whatsapp",
        "marketing/social_marketing",
        "productivity/discuss",
        "productivity/sign",
        "services/helpdesk",
        "sales/subscriptions",
    ],
    "tier2": [
        "services/field_service",
        "services/planning",
        "hr/recruitment",
        "hr/time_off",
        "hr/payroll",
        "hr/appraisals",
        "sales/rental",
        "inventory_and_mrp/barcode",
        "inventory_and_mrp/quality",
        "inventory_and_mrp/maintenance",
        "productivity/documents",
        "productivity/calendar",
        "marketing/events",
    ],
}

# Budget de génération par tier (fraction du total). Doit sommer à 1.0.
TIER_BUDGET: Final[dict[str, float]] = {
    "cert": 0.70,
    "tier1": 0.20,
    "tier2": 0.10,
}

# Modules introduits à partir d'Odoo 19.0 (0 chunk v18 attendu).
V19_ONLY_MODULES: Final[frozenset[str]] = frozenset({
    "productivity/ai",
    "productivity/knowledge",
})

# Mapping libellé cert → chemin(s) URL réel(s) sous /applications/, quand ils
# diffèrent du libellé. Les chemins sont relatifs au préfixe
# /documentation/<version>/applications/. Plusieurs entrées si le module a été
# déplacé entre v18 et v19 ou s'il existe plusieurs URLs valides.
#
# Note : ce dict est augmenté au fil des validations sitemap (Phase 3.2).
MODULE_URL_PATHS: Final[dict[str, list[str]]] = {
    "crm": ["sales/crm"],
    "accounting": ["finance/accounting"],
    "purchase": ["inventory_and_mrp/purchase"],
    # Tier 1 & 2 — chemins à valider via scripts/validate_module_paths.py.
    # Si vide / absent, le validateur essaiera le libellé tel quel.
}

ALL_TIERS: Final[tuple[str, ...]] = tuple(STUDY_MODULES.keys())


def all_modules() -> list[str]:
    """Liste plate de tous les modules, tous tiers confondus, ordre déterministe."""
    out: list[str] = []
    for tier in ALL_TIERS:
        out.extend(STUDY_MODULES[tier])
    return out


def modules_in_tier(tier: str) -> list[str]:
    if tier not in STUDY_MODULES:
        raise ValueError(f"Tier inconnu : {tier!r} (attendu : {', '.join(ALL_TIERS)})")
    return list(STUDY_MODULES[tier])


def tier_of(module: str) -> str | None:
    """Retourne le tier auquel appartient `module`, ou None si inconnu."""
    for tier, mods in STUDY_MODULES.items():
        if module in mods:
            return tier
    return None


def is_v19_only(module: str) -> bool:
    return module in V19_ONLY_MODULES


def url_paths_for(module: str) -> list[str]:
    """Chemins URL réels (sous /applications/) pour un module donné.

    Retourne `MODULE_URL_PATHS[module]` si configuré, sinon `[module]`.
    """
    return list(MODULE_URL_PATHS.get(module, [module]))


__all__ = [
    "STUDY_MODULES",
    "TIER_BUDGET",
    "V19_ONLY_MODULES",
    "MODULE_URL_PATHS",
    "ALL_TIERS",
    "all_modules",
    "modules_in_tier",
    "tier_of",
    "is_v19_only",
    "url_paths_for",
]
