# Recommandation

*Généré le 2026-05-19 11:46 UTC*

## Modules à re-crawler en priorité

- `sales` v18 : 57 URLs au sitemap, 56 ingérées (1 manquantes)
- `sales` v19 : 63 URLs au sitemap, 59 ingérées (4 manquantes)
- `inventory_and_mrp/inventory` v18 : 84 URLs au sitemap, 82 ingérées (2 manquantes)
- `inventory_and_mrp/inventory` v19 : 84 URLs au sitemap, 33 ingérées (51 manquantes)
- `inventory_and_mrp/manufacturing` v18 : 36 URLs au sitemap, 35 ingérées (1 manquantes)
- `inventory_and_mrp/manufacturing` v19 : 37 URLs au sitemap, 36 ingérées (1 manquantes)
- `websites/website` v18 : 23 URLs au sitemap, 22 ingérées (1 manquantes)
- `websites/website` v19 : 22 URLs au sitemap, 21 ingérées (1 manquantes)
- `marketing/marketing_automation` v18 : 7 URLs au sitemap, 6 ingérées (1 manquantes)
- `marketing/marketing_automation` v19 : 7 URLs au sitemap, 6 ingérées (1 manquantes)

## Modules au plafond naturel (NE PAS re-crawler)

- `services/timesheets` : 3 URLs au sitemap, tout ingéré → **4** chunks (plafond naturel)
- `services/timesheets` : 3 URLs au sitemap, tout ingéré → **6** chunks (plafond naturel)
- `marketing/email_marketing` : 5 URLs au sitemap, tout ingéré → **28** chunks (plafond naturel)
- `marketing/email_marketing` : 5 URLs au sitemap, tout ingéré → **28** chunks (plafond naturel)
- `marketing/sms_marketing` : 7 URLs au sitemap, tout ingéré → **14** chunks (plafond naturel)
- `marketing/sms_marketing` : 8 URLs au sitemap, tout ingéré → **16** chunks (plafond naturel)
- `marketing/surveys` : 6 URLs au sitemap, tout ingéré → **26** chunks (plafond naturel)
- `marketing/surveys` : 6 URLs au sitemap, tout ingéré → **26** chunks (plafond naturel)
- `studio` : 8 URLs au sitemap, tout ingéré → **43** chunks (plafond naturel)
- `studio` : 8 URLs au sitemap, tout ingéré → **48** chunks (plafond naturel)
- `productivity/knowledge` : 1 URLs au sitemap, tout ingéré → **5** chunks (plafond naturel)
- `productivity/knowledge` : 1 URLs au sitemap, tout ingéré → **6** chunks (plafond naturel)

## Anomalies de découpage

- Aucune — moyennes entre ~400 et 700 tokens sur l’échantillon.

## Action recommandée

- [ ] Lancer une re-ingestion ciblée (`--modules`) sur les modules listés en priorité
  - v19 : `inventory_and_mrp/inventory,inventory_and_mrp/manufacturing,marketing/marketing_automation,sales,websites/website`
  - v18 : `inventory_and_mrp/inventory,inventory_and_mrp/manufacturing,marketing/marketing_automation,sales,websites/website`
- [ ] Ajuster les quotas du plan de génération pour les modules au plafond naturel
- [ ] Procéder à la génération avec la couverture actuelle

**Règle quotas** : `target_questions = min(planned_quota, n_chunks × 2)` (voir `scripts/plan_generation.py`).