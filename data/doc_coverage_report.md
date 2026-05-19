# Audit de couverture documentaire Odoo

*Généré le 2026-05-19 11:40 UTC — base `/Users/patri/odoo-quiz/data/odoo_docs.sqlite`*

## Totaux

- Odoo 18.0 : **2487** chunks ❌ (cible ≥ 6000)
- Odoo 19.0 : **2591** chunks ❌ (cible ≥ 6000)

### Embeddings

- v18 sans embedding : **0**
- v19 sans embedding : **0**

## Détail par module

Seuil par module : **≥ 150** chunks (sauf modules v19-only : 0 attendu en v18).

| Module | v18 chunks | v19 chunks | Statut |
|---|---:|---:|---|
| `crm` | 72 | 68 | ⚠️ v18 sous-représenté (72); v19 sous-représenté (68) |
| `sales` | 295 | 312 | ✅ |
| `purchase` | 42 | 44 | ⚠️ v18 sous-représenté (42); v19 sous-représenté (44) |
| `accounting` | 206 | 221 | ✅ |
| `inventory_and_mrp/inventory` | 233 | 93 | ⚠️ v19 sous-représenté (93) |
| `inventory_and_mrp/manufacturing` | 103 | 105 | ⚠️ v18 sous-représenté (103); v19 sous-représenté (105) |
| `services/project` | 17 | 21 | ⚠️ v18 sous-représenté (17); v19 sous-représenté (21) |
| `services/timesheets` | 4 | 6 | ⚠️ v18 sous-représenté (4); v19 sous-représenté (6) |
| `hr` | 332 | 415 | ✅ |
| `websites/website` | 55 | 55 | ⚠️ v18 sous-représenté (55); v19 sous-représenté (55) |
| `websites/ecommerce` | 31 | 41 | ⚠️ v18 sous-représenté (31); v19 sous-représenté (41) |
| `marketing/email_marketing` | 28 | 28 | ⚠️ v18 sous-représenté (28); v19 sous-représenté (28) |
| `marketing/marketing_automation` | 18 | 18 | ⚠️ v18 sous-représenté (18); v19 sous-représenté (18) |
| `marketing/sms_marketing` | 14 | 16 | ⚠️ v18 sous-représenté (14); v19 sous-représenté (16) |
| `marketing/surveys` | 26 | 26 | ⚠️ v18 sous-représenté (26); v19 sous-représenté (26) |
| `sales/point_of_sale` | 71 | 87 | ⚠️ v18 sous-représenté (71); v19 sous-représenté (87) |
| `productivity/spreadsheet` | 45 | 52 | ⚠️ v18 sous-représenté (45); v19 sous-représenté (52) |
| `studio` | 43 | 48 | ⚠️ v18 sous-représenté (43); v19 sous-représenté (48) |
| `productivity/ai` | — | 22 | ❌ v19 sous-représenté (22 < 150) |
| `productivity/knowledge` | 5 | 6 | ❌ v18 inattendu (5); v19 sous-représenté (6 < 150) |

## Recommandation

- [ ] Couverture OK, passer à la génération
- [ ] **Re-ingestion v18 requise** (crm, purchase, inventory_and_mrp/manufacturing, services/project, services/timesheets, websites/website, websites/ecommerce, marketing/email_marketing, marketing/marketing_automation, marketing/sms_marketing, marketing/surveys, sales/point_of_sale, productivity/spreadsheet, studio)
- [ ] **Re-ingestion v19 requise** (crm, purchase, inventory_and_mrp/inventory, inventory_and_mrp/manufacturing, services/project, services/timesheets, websites/website, websites/ecommerce, marketing/email_marketing, marketing/marketing_automation, marketing/sms_marketing, marketing/surveys, sales/point_of_sale, productivity/spreadsheet, studio, productivity/ai, productivity/knowledge)

*Note : modules v19-only avec chunks v18 inattendus : productivity/knowledge*
