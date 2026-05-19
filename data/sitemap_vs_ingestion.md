# Sitemap vs ingestion

*Généré le 2026-05-19 11:46 UTC*

Sources URLs : v18 `searchindex.js` · v19 `searchindex.js`

**Lecture des ratios** : 4–10 chunks/URL = normal (~600 tokens/chunk). < 3.0 = pages manquantes ou mauvais découpage. > 12.0 = possible sur-découpage.

## Comparaison sitemap / chunks

| Module | URLs sitemap v18 | Chunks v18 | Ratio | URLs sitemap v19 | Chunks v19 | Ratio | Diagnostic |
|---|---:|---:|---:|---:|---:|---:|---|
| `crm` | 27 | 72 | 2.7x | 28 | 68 | 2.4x | v18: ❌ incomplet (ratio 2.7x) · v19: ❌ incomplet (ratio 2.4x) |
| `sales` | 57 | 152 | 2.7x | 63 | 157 | 2.5x | v18: ❌ incomplet (ratio 2.7x) · v19: ❌ incomplet (ratio 2.5x) |
| `purchase` | 17 | 42 | 2.5x | 19 | 44 | 2.3x | v18: ❌ incomplet (ratio 2.5x) · v19: ❌ incomplet (ratio 2.3x) |
| `accounting` | 92 | 206 | 2.2x | 93 | 221 | 2.4x | v18: ❌ incomplet (ratio 2.2x) · v19: ❌ incomplet (ratio 2.4x) |
| `inventory_and_mrp/inventory` | 84 | 233 | 2.8x | 84 | 93 | 1.1x | v18: ❌ incomplet (ratio 2.8x) · v19: ❌ incomplet (ratio 1.1x) |
| `inventory_and_mrp/manufacturing` | 36 | 103 | 2.9x | 37 | 105 | 2.8x | v18: ❌ incomplet (ratio 2.9x) · v19: ❌ incomplet (ratio 2.8x) |
| `services/project` | 10 | 17 | 1.7x | 12 | 21 | 1.8x | v18: ❌ incomplet (ratio 1.7x) · v19: ❌ incomplet (ratio 1.8x) |
| `services/timesheets` | 3 | 4 | 1.3x | 3 | 6 | 2.0x | v18: OK plafond naturel (ratio 1.3x, peu de pages) · v19: OK plafond naturel (ratio 2.0x, peu de pages) |
| `hr` | 83 | 332 | 4.0x | 96 | 415 | 4.3x | v18: OK (ratio ~4.0x) · v19: OK (ratio ~4.3x) |
| `websites/website` | 23 | 55 | 2.4x | 22 | 55 | 2.5x | v18: ❌ incomplet (ratio 2.4x) · v19: ❌ incomplet (ratio 2.5x) |
| `websites/ecommerce` | 12 | 31 | 2.6x | 16 | 41 | 2.6x | v18: ❌ incomplet (ratio 2.6x) · v19: ❌ incomplet (ratio 2.6x) |
| `marketing/email_marketing` | 5 | 28 | 5.6x | 5 | 28 | 5.6x | v18: OK (ratio ~5.6x) · v19: OK (ratio ~5.6x) |
| `marketing/marketing_automation` | 7 | 18 | 2.6x | 7 | 18 | 2.6x | v18: OK plafond naturel (ratio 2.6x, peu de pages) · v19: OK plafond naturel (ratio 2.6x, peu de pages) |
| `marketing/sms_marketing` | 7 | 14 | 2.0x | 8 | 16 | 2.0x | v18: OK plafond naturel (ratio 2.0x, peu de pages) · v19: OK plafond naturel (ratio 2.0x, peu de pages) |
| `marketing/surveys` | 6 | 26 | 4.3x | 6 | 26 | 4.3x | v18: OK (ratio ~4.3x) · v19: OK (ratio ~4.3x) |
| `sales/point_of_sale` | 42 | 71 | 1.7x | 43 | 87 | 2.0x | v18: ❌ incomplet (ratio 1.7x) · v19: ❌ incomplet (ratio 2.0x) |
| `productivity/spreadsheet` | 10 | 45 | 4.5x | 11 | 52 | 4.7x | v18: OK (ratio ~4.5x) · v19: OK (ratio ~4.7x) |
| `studio` | 8 | 43 | 5.4x | 8 | 48 | 6.0x | v18: OK (ratio ~5.4x) · v19: OK (ratio ~6.0x) |
| `productivity/ai` | 0 | 0 | — | 12 | 22 | 1.8x | v19: ❌ incomplet (ratio 1.8x) |
| `productivity/knowledge` | 1 | 5 | 5.0x | 1 | 6 | 6.0x | v18: OK (ratio ~5.0x) · v19: OK (ratio ~6.0x) |

## Taille moyenne des chunks (échantillon)

| Module | Ver. | n échant. | avg chars | avg tokens | min | max | Anomalie découpage |
|---|---:|---:|---:|---:|---:|---:|---|
| `crm` | 18.0 | 72 | 2083 | 482 | 19 | 600 | ✅ |
| `crm` | 19.0 | 68 | 2073 | 473 | 19 | 601 | ✅ |
| `sales` | 18.0 | 100 | 2166 | 492 | 21 | 600 | ✅ |
| `sales` | 19.0 | 100 | 2190 | 496 | 17 | 600 | ✅ |
| `purchase` | 18.0 | 42 | 1862 | 448 | 23 | 601 | ✅ |
| `purchase` | 19.0 | 44 | 1918 | 458 | 23 | 601 | ✅ |
| `accounting` | 18.0 | 100 | 2177 | 512 | 92 | 602 | ✅ |
| `accounting` | 19.0 | 100 | 2189 | 493 | 85 | 601 | ✅ |
| `inventory_and_mrp/inventory` | 18.0 | 100 | 2194 | 507 | 28 | 600 | ✅ |
| `inventory_and_mrp/inventory` | 19.0 | 93 | 2169 | 502 | 30 | 601 | ✅ |
| `inventory_and_mrp/manufacturing` | 18.0 | 100 | 2134 | 496 | 20 | 601 | ✅ |
| `inventory_and_mrp/manufacturing` | 19.0 | 100 | 2125 | 493 | 20 | 601 | ✅ |
| `services/project` | 18.0 | 17 | 1769 | 412 | 27 | 601 | ✅ |
| `services/project` | 19.0 | 21 | 1860 | 430 | 27 | 601 | ✅ |
| `services/timesheets` | 18.0 | 4 | 1451 | 325 | 32 | 600 | ✅ |
| `services/timesheets` | 19.0 | 6 | 1947 | 453 | 148 | 600 | ✅ |
| `hr` | 18.0 | 100 | 2197 | 505 | 83 | 601 | ✅ |
| `hr` | 19.0 | 100 | 2185 | 504 | 93 | 601 | ✅ |
| `websites/website` | 18.0 | 55 | 2093 | 495 | 39 | 600 | ✅ |
| `websites/website` | 19.0 | 55 | 2105 | 499 | 36 | 600 | ✅ |
| `websites/ecommerce` | 18.0 | 31 | 2164 | 514 | 86 | 602 | ✅ |
| `websites/ecommerce` | 19.0 | 41 | 2175 | 510 | 106 | 600 | ✅ |
| `marketing/email_marketing` | 18.0 | 28 | 2378 | 549 | 85 | 601 | ✅ |
| `marketing/email_marketing` | 19.0 | 28 | 2378 | 549 | 85 | 601 | ✅ |
| `marketing/marketing_automation` | 18.0 | 18 | 2304 | 525 | 154 | 601 | ✅ |
| `marketing/marketing_automation` | 19.0 | 18 | 2304 | 525 | 154 | 601 | ✅ |
| `marketing/sms_marketing` | 18.0 | 14 | 1956 | 459 | 128 | 600 | ✅ |
| `marketing/sms_marketing` | 19.0 | 16 | 2016 | 474 | 128 | 600 | ✅ |
| `marketing/surveys` | 18.0 | 26 | 2302 | 529 | 117 | 601 | ✅ |
| `marketing/surveys` | 19.0 | 26 | 2301 | 529 | 117 | 601 | ✅ |
| `sales/point_of_sale` | 18.0 | 71 | 1734 | 417 | 22 | 600 | ✅ |
| `sales/point_of_sale` | 19.0 | 87 | 2001 | 476 | 83 | 601 | ✅ |
| `productivity/spreadsheet` | 18.0 | 45 | 2274 | 538 | 125 | 600 | ✅ |
| `productivity/spreadsheet` | 19.0 | 52 | 2304 | 547 | 94 | 601 | ✅ |
| `studio` | 18.0 | 43 | 2298 | 557 | 173 | 600 | ✅ |
| `studio` | 19.0 | 48 | 2277 | 548 | 124 | 600 | ✅ |
| `productivity/ai` | 19.0 | 22 | 2194 | 485 | 153 | 600 | ✅ |
| `productivity/knowledge` | 18.0 | 5 | 2559 | 596 | 585 | 600 | ✅ |
| `productivity/knowledge` | 19.0 | 6 | 2378 | 556 | 339 | 600 | ✅ |

## Échantillonnage — URLs manquantes (top 5 ratios bas)

### `inventory_and_mrp/inventory` · v19 (ratio 1.1x)
- URLs sitemap : **84** · URLs distinctes en base : **33** · **Manquantes : 51**
- Premières URLs absentes de la base :
  - `https://www.odoo.com/documentation/19.0/applications/inventory_and_mrp/inventory/shipping_receiving/picking_methods.html`
  - `https://www.odoo.com/documentation/19.0/applications/inventory_and_mrp/inventory/shipping_receiving/removal_strategies.html`
  - `https://www.odoo.com/documentation/19.0/applications/inventory_and_mrp/inventory/shipping_receiving/removal_strategies/closest_location.html`
  - `https://www.odoo.com/documentation/19.0/applications/inventory_and_mrp/inventory/shipping_receiving/removal_strategies/fefo.html`
  - `https://www.odoo.com/documentation/19.0/applications/inventory_and_mrp/inventory/shipping_receiving/removal_strategies/fifo.html`
  - `https://www.odoo.com/documentation/19.0/applications/inventory_and_mrp/inventory/shipping_receiving/removal_strategies/least_packages.html`
  - `https://www.odoo.com/documentation/19.0/applications/inventory_and_mrp/inventory/shipping_receiving/removal_strategies/lifo.html`
  - `https://www.odoo.com/documentation/19.0/applications/inventory_and_mrp/inventory/shipping_receiving/reservation_methods.html`
  - `https://www.odoo.com/documentation/19.0/applications/inventory_and_mrp/inventory/shipping_receiving/reservation_methods/at_confirmation.html`
  - `https://www.odoo.com/documentation/19.0/applications/inventory_and_mrp/inventory/shipping_receiving/reservation_methods/before_scheduled_date.html`

### `services/timesheets` · v18 (ratio 1.3x)
- URLs sitemap : **3** · URLs distinctes en base : **3** · **Manquantes : 0**
- Aucune URL sitemap absente de la base (couverture URL complète).

### `sales/point_of_sale` · v18 (ratio 1.7x)
- URLs sitemap : **42** · URLs distinctes en base : **42** · **Manquantes : 0**
- Aucune URL sitemap absente de la base (couverture URL complète).

### `services/project` · v18 (ratio 1.7x)
- URLs sitemap : **10** · URLs distinctes en base : **10** · **Manquantes : 0**
- Aucune URL sitemap absente de la base (couverture URL complète).

### `productivity/ai` · v19 (ratio 1.8x)
- URLs sitemap : **12** · URLs distinctes en base : **12** · **Manquantes : 0**
- Aucune URL sitemap absente de la base (couverture URL complète).
