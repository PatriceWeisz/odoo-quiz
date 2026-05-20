# Plan de génération — Phase 5

*Généré le 2026-05-19T23:59:27Z*

- **Cible totale** : 3000 questions
- Tier budget : {'cert': 0.7, 'tier1': 0.2, 'tier2': 0.1}
- Ratios versions : v18=0.4, v19=0.6
- Plafond : `target ≤ n_chunks × 2` (modules <100 chunks) ou `× 3` (≥100, denses)

**Total atteignable** : **2999** questions
**Overflow (non-allouable par plafonnement)** : 0

## Tier `cert` — budget 2100, atteignable **2101**, overflow 0

| Module | chunks v18 | chunks v19 | cap v18 | cap v19 | target v18 | target v19 | total |
|---|---:|---:|---:|---:|---:|---:|---:|
| `crm` | 72 | 68 | 144 | 136 | **34** | **51** | **85** |
| `sales` | 295 | 312 | 885 | 936 | **147** | **221** | **368** |
| `purchase` | 42 | 44 | 84 | 88 | **21** | **31** | **52** |
| `accounting` | 206 | 221 | 618 | 663 | **104** | **155** | **259** |
| `inventory_and_mrp/inventory` | 233 | 232 | 699 | 696 | **113** | **169** | **282** |
| `inventory_and_mrp/manufacturing` | 103 | 105 | 309 | 315 | **50** | **76** | **126** |
| `services/project` | 17 | 21 | 34 | 42 | **9** | **14** | **23** |
| `services/timesheets` | 4 | 6 | 8 | 12 | **2** | **4** | **6** |
| `hr` | 332 | 415 | 996 | 1245 | **181** | **272** | **453** |
| `websites/website` | 55 | 55 | 110 | 110 | **27** | **40** | **67** |
| `websites/ecommerce` | 31 | 41 | 62 | 82 | **18** | **26** | **44** |
| `marketing/email_marketing` | 28 | 28 | 56 | 56 | **14** | **20** | **34** |
| `marketing/marketing_automation` | 18 | 18 | 36 | 36 | **9** | **13** | **22** |
| `marketing/sms_marketing` | 14 | 16 | 28 | 32 | **7** | **11** | **18** |
| `marketing/surveys` | 26 | 26 | 52 | 52 | **13** | **19** | **32** |
| `sales/point_of_sale` | 71 | 87 | 142 | 174 | **38** | **58** | **96** |
| `productivity/spreadsheet` | 45 | 52 | 90 | 104 | **24** | **35** | **59** |
| `studio` | 43 | 48 | 86 | 96 | **22** | **33** | **55** |
| `productivity/ai` 🆕 | 0 | 22 | 0 | 44 | **0** | **13** | **13** |
| `productivity/knowledge` 🆕 | 5 | 6 | 10 | 12 | **0** | **7** | **7** |

## Tier `tier1` — budget 600, atteignable **599**, overflow 0

| Module | chunks v18 | chunks v19 | cap v18 | cap v19 | target v18 | target v19 | total |
|---|---:|---:|---:|---:|---:|---:|---:|
| `productivity/whatsapp` | 14 | 14 | 28 | 28 | **16** | **24** | **40** |
| `marketing/social_marketing` | 12 | 12 | 24 | 24 | **14** | **20** | **34** |
| `productivity/discuss` | 16 | 15 | 32 | 30 | **18** | **26** | **44** |
| `productivity/sign` | 100 | 119 | 300 | 357 | **124** | **187** | **311** |
| `services/helpdesk` | 38 | 39 | 76 | 78 | **44** | **65** | **109** |
| `sales/subscriptions` | 24 | 19 | 48 | 38 | **24** | **37** | **61** |

## Tier `tier2` — budget 300, atteignable **299**, overflow 0

| Module | chunks v18 | chunks v19 | cap v18 | cap v19 | target v18 | target v19 | total |
|---|---:|---:|---:|---:|---:|---:|---:|
| `services/field_service` | 6 | 6 | 12 | 12 | **2** | **2** | **4** |
| `services/planning` | 6 | 6 | 12 | 12 | **2** | **2** | **4** |
| `hr/recruitment` | 40 | 40 | 80 | 80 | **12** | **18** | **30** |
| `hr/time_off` | 24 | 24 | 48 | 48 | **7** | **11** | **18** |
| `hr/payroll` | 128 | 185 | 384 | 555 | **47** | **70** | **117** |
| `hr/appraisals` | 19 | 21 | 38 | 42 | **6** | **9** | **15** |
| `sales/rental` | 5 | 13 | 10 | 26 | **3** | **4** | **7** |
| `inventory_and_mrp/barcode` | 46 | 46 | 92 | 92 | **14** | **20** | **34** |
| `inventory_and_mrp/quality` | 26 | 23 | 52 | 46 | **7** | **11** | **18** |
| `inventory_and_mrp/maintenance` | 16 | 4 | 32 | 8 | **3** | **4** | **7** |
| `productivity/documents` | 6 | 7 | 12 | 14 | **2** | **3** | **5** |
| `productivity/calendar` | 13 | 13 | 26 | 26 | **4** | **6** | **10** |
| `marketing/events` | 41 | 38 | 82 | 76 | **12** | **18** | **30** |

