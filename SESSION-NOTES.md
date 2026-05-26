# Notes de passation — état après session du 20 mai 2026

Ce fichier complète `BRIEFING_COWORK_quiz_odoo.md.docx`. Il ne le remplace pas :
le briefing reste la source d'origine pour les décisions architecturales. Ce
document liste les **écarts**, **décisions prises pendant l'exécution**, et la
**dette technique** identifiée.

---

## 🆕 SESSION 26 mai 2026 — Upgrade VPS Hetzner **CX23 → CX33** + hub d'évals + correctifs OOM

### Contexte
Après bascule au modèle d'embedding **mxbai-large-v1** (dim 1024) puis nettoyage mojibake
de 22 titres, le warmup gunicorn re-embedde toute la banque au prochain restart
(fingerprint changé). Avec **fastembed batch par défaut 256** + mxbai-large, ce warmup
allouait **>3 Go** d'arena onnxruntime → **OOM** sur l'ancien VPS CX23 (3,7 Go RAM).
Le hub d'évals /eval (lancement subprocess depuis Flask) chargeait une 2ᵉ copie du
modèle → aggravait le problème.

### VPS désormais
- Hetzner Cloud, projet **« Odoo-quiz »**, serveur `odoo-quiz` **id `132093560`**,
  IPv4 **178.104.211.37**, zone **eu-central / Falkenstein**.
- Type : **cx33** (4 vCPU, 8 Go RAM, 80 Go disque NVMe) — **6,49 €/mois HT**.
  ⚠️ La ligne actuelle Hetzner est **cx2X/cx3X gen 3** (cx23 / cx33 / cx43 / cx53),
  pas cx22 / cx32. Ne pas confondre avec les comparatifs web qui mentionnent CX22/CX32.

### Procédure rescale via API Hetzner (à reproduire si besoin)
1. Console Hetzner → projet **Odoo-quiz** → **Security → API Tokens** → *Generate*
   (Read & Write), description ex. `rescale-claude`.
2. Sauver le token côté Mac (jamais en clair dans le chat) :
   ```bash
   umask 077; printf '%s' 'LE_TOKEN' > ~/odoo-quiz/.hetzner-token
   ```
   (Le fichier `.hetzner-token` est dans le `.gitignore`.)
3. Appels API (via le pont) — bien parser le JSON avec un **heredoc Python séparé**,
   ne pas mélanger `curl | python3 <<'PY'` (le heredoc remplace stdin du pipe) :
   ```bash
   TOKEN=$(cat ~/odoo-quiz/.hetzner-token); API='https://api.hetzner.cloud/v1'; SID=132093560
   curl -X POST -H "Authorization: Bearer $TOKEN" "$API/servers/$SID/actions/poweroff"
   # attendre status=off
   curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"server_type":"cx33","upgrade_disk":true}' "$API/servers/$SID/actions/change_type"
   # poller /v1/actions/<id> jusqu'à status=success
   curl -X POST -H "Authorization: Bearer $TOKEN" "$API/servers/$SID/actions/poweron"
   # attendre status=running, puis SSH verify
   ```
4. Cleanup : `rm ~/odoo-quiz/.hetzner-token` + révoquer le token dans la console.

⚠️ `upgrade_disk: true` est **obligatoire** pour passer à un type à disque plus
gros (cx23 40 Go → cx33 80 Go) ; le disque est ensuite **irréversible**. Le
CPU/RAM, eux, sont réversibles.

### Correctifs livrés (commits associés)
- `bank_embeddings._embed_texts` : `batch_size=32` (au lieu du défaut fastembed 256).
  Borne le pic mémoire du warmup même si le fingerprint change à nouveau.
- Gunicorn : **`--workers 1 --threads 4`** (au lieu de 2/2) dans
  `/etc/systemd/system/odoo-quiz.service` — une seule copie du modèle en RAM. Sur
  CX33 (8 Go) on peut repasser à 2/2 sans risque si on veut.
- Hub d'évals `/eval` : garde anti-concurrence **synchrone** (écrit le fichier
  `running` avant `Popen`) + **pré-check `MemAvailable ≥ 900 Mo`** avant lancement.
  Stockage par run dans `data/evals/<run_id>.json`.

### État final v2.7.1
- VPS : cx33 (4 vCPU / 7,6 Gi RAM / 75 Go libres / Falkenstein).
- Service `odoo-quiz` actif, `bank_rag.embeddings = true`, modèle mxbai-large-v1.
- Hub d'évals opérationnel à `/eval` (lancer / historique / détail).
- Banque : 3 561 questions (mojibake nettoyé), index mxbai dim 1024.
- Doc Odoo : 5 230 chunks dim 1024.

---

## 🆕 SESSION 24 mai 2026 (soir) — Mode « test des prédictions de Claude » au quiz (v2.6.0)

**But (demande Patrice)** : passer un quiz aléatoire en choisissant **Source = Udemy**,
pouvoir prendre **tous les modules**, fixer **temps imparti** + **nombre de questions**,
et afficher **la prédiction de Claude avec un % de confiance issu des questions embedded
proches** — Claude prédisant **en aveugle d'Udemy** pour mesurer honnêtement la qualité.

### Fait
1. **`app/quiz_predict.py`** (nouveau) : `predict_quiz_answer()` reproduit le pipeline
   d'éval à l'unité — banque RAG **leave-one-out + sans AUCUNE question Udemy**
   (`build_loo_bank`), appel `suggest_answer` (escalade Opus optionnelle), puis **%
   de confiance par accord des voisins embedded** (`_neighbor_confidence` : chaque
   voisin vote pour l'option la plus proche de sa bonne réponse, pondéré par la
   similarité ; repli bande catégorielle si trop peu de voisins). Vérifié : banque RAG
   3561 → **2587** (974 Udemy + question retirées, zéro fuite).
2. **`/api/questions`** : nouveau paramètre **`source`** (ex. `udemy`) + `module=__all__`
   (= tous les modules).
3. **`POST /api/suggest-quiz`** : `{id, escalate}` → prédiction + % confiance + justif.
4. **UI quiz** (`HTML` dans `app.py`) : sélecteur **Source** (Udemy par défaut), option
   **Tous les modules**, champ **temps imparti** (min, 0=illimité, **compte à rebours**
   + fin auto). Encadré **« Réglages suggestions Claude »** : bascules **« prédictions »**,
   **« exclure les questions Udemy du contexte »** (coché = test en aveugle ; décoché =
   Claude voit la banque Udemy, paramètre `exclude_udemy` de `/api/suggest-quiz`) et
   **escalade Opus**. Encart **🤖 prédiction** (avant la réponse = indice) avec badge de
   mode dynamique + barre de confiance ; écran final **« Toi vs Claude »** (précision
   globale + ventilation par bande de confiance, libellés adaptés au mode).

### À faire pour mettre en service
- **Redémarrer Flask** (local : `scripts/restart_flask.sh` ou `run.sh` ; VPS : scp `app.py`
  + `app/quiz_predict.py` puis `systemctl restart odoo-quiz`).
- Coût : chaque question prédite = 1 appel API (≥1 si escalade). Le toggle permet de couper.

### Dette / notes
- Le `%` affiché « Tous les modules » est le total cert (la source est filtrée au lancement,
  l'échantillon est plafonné côté serveur) — volontairement approximatif (`≈`).
- `_text_sim` (option↔réponse voisin) reste lexical ; la sélection des voisins, elle, est
  bien vectorielle. Piste : matcher les réponses par embedding aussi.

### Réencodage embeddings vers un modèle robuste (`scripts/reencode_embeddings.py`)

Script **standalone**, à lancer **sur le serveur, hors session Claude** (idempotent,
backups, dry-run). Réencode **banque + index doc Odoo** avec un nouveau modèle
fastembed et met à jour `config.json`. ⚠️ le modèle est partagé banque/doc et
`search_doc_chunks` rejette toute requête de dimension différente → on réencode
les deux. Le `query_timeout_s` (nouveau, `config.json → bank_rag`) est relâché à
3 s car un gros modèle est plus lent que MiniLM (sinon la requête banque expire et
retombe en lexical ; `bank_embeddings._query_timeout_s` lit cette clé).

Modèle retenu : **mixedbread-ai/mxbai-embed-large-v1** (dim 1024, 0,64 Go — tient
dans les 3,7 Go du VPS ; dry-run OK, ~121 ms/texte).

```bash
cd /opt/odoo-quiz
# 1) validation (rien écrit) :
./.venv/bin/python -m scripts.reencode_embeddings --dry-run
# 2) réencodage complet, détaché (banque ~7-10 min, doc ~30-45 min sur 2 cœurs) :
nohup ./.venv/bin/python -m scripts.reencode_embeddings \
    --model mixedbread-ai/mxbai-embed-large-v1 --yes \
    > logs/reencode_$(date +%Y%m%dT%H%M%S).log 2>&1 &
# 3) une fois terminé :
sudo systemctl restart odoo-quiz && curl -s localhost:5001/health
```

Backups auto : `config.json.bak.*`, `bank_embeddings.npz.bak.*`,
`odoo_docs.sqlite.bak.*`. Rollback = restaurer ces fichiers + remettre l'ancien
`bank_rag.model` dans `config.json` + restart. Options : `--target bank|docs|both`,
`--batch-size`, `--no-config`, `--restart` (root).

---

## 🚀 SESSION 24 mai 2026 — COMMENCER ICI (v2.5.1)

**État** : tout en prod sur https://quiz-odoo.picvert-senedoo.org — **v2.5.1**,
**3 561 questions** (0 doublon de titre, 100 % avec une bonne réponse, 100 %
encodées dans l'index vectoriel). VPS dédié `odoo-quiz` (178.104.211.37). Repo
à jour et poussé (`PatriceWeisz/odoo-quiz`, `main`).

### Accès
- **Login appli (Caddy basic_auth, tout le site sauf `/health`)** : `patrice@senedoo.com`
  / mot de passe stocké en hash bcrypt dans `/etc/caddy/Caddyfile` (le changer :
  `caddy hash-password --plaintext '...'` puis remplacer le hash + `systemctl reload caddy`).
- **Page admin** : `…/admin/review` (s'appuie sur le login global ; `config.json` n'a plus
  de section `admin`). Lien « 🔧 Admin » dans l'en-tête.

### Fait cette session (chronologique)
1. **Phase 7.2 / 7.3** : bouton « Signaler » (→ `flagged`) + page de relecture admin
   (Valider / Modifier / Supprimer). (v2.2.0)
2. **Login global** Caddy basic_auth sur tout le site, `/health` ouvert ; jeton admin retiré. (v2.3.0)
3. **Décommission de l'ancien VPS** (service quiz + bloc Caddy) — **ResourceSpace intact**.
4. **Bugfixes** : picker module (`total` const→let, v2.2.1) ; bouton « Ignorer » de la capture
   (validait la réponse avant le choix, v2.3.1).
5. **Capture** : filet anti-dépassement vision (≤7800 px), image **recadrée** envoyée à Claude,
   puis **hybride lien `<img>` (téléchargé, anti-SSRF) sinon recadrage** (v2.4.0→2.4.2) ;
   **plafond 4→6 questions/page** (v2.4.3) ; **régression login→favori corrigée** (URLs de
   capture exemptées du login) ; **DOM-first** : on garde le texte du DOM, on n'envoie que
   l'image à Claude.
6. **Suggestions** : **escalade Opus 4.6** si confiance non-haute + **accord voisin** vectoriel ;
   forum/aide Odoo ajoutés aux sources web ; `escalation_model` dans `config.json`. (v2.5.0→2.5.1)
7. **Harnais d'éval** `scripts/eval_suggestions.py` (leave-one-out, parallèle, ablation, A/B modèle).
   Mesure : **~85 % sur questions Udemy jamais vues** (92 % en confiance haute) ; **la taille de
   banque n'influe pas** → générer +3 000 questions ou changer d'embedding n'aiderait sans doute pas.
8. **Import nouveau cours Udemy (vérifié humain)** via l'API Udemy (navigateur) :
   - +359 nouvelles questions (820 récupérées, 461 doublons écartés), `target_version=19.0`, module
     mappé depuis la section (`scripts/import_udemy_course.py`).
   - 443 anciennes Udemy re-taguées **`both`** → toutes visibles au quiz.
   - **13 réponses corrigées + 42 remplacées** par la version vérifiée (`scripts/fix_udemy_answers.py`).
   - **30 doublons de titre préexistants supprimés** (`scripts/dedup_bank_titles.py`).
   - **19 questions sans bonne réponse supprimées** (non exploitables, hors index vectoriel).
   - Bilan banque : 3251 → 3610 → **3561**.

### En réserve (NON faits — décidés « plus tard »)
- **A/B modèle d'embedding** `mixedbread-ai/mxbai-embed-large-v1` vs MiniLM (actuel). Le
  ré-encodage est gratuit ($0, fastembed local) mais touche **banque + index doc** (même modèle
  requête/index) ; gain probablement marginal (cf. ablation). Idéalement ré-encoder sur le Mac M5.
- **Éval complète** des ~641 Udemy (relancer `eval_suggestions.py --concurrency 6`, ~15-20 min).
- **Autres cours Udemy** : fournir l'URL d'un cours possédé → même pipeline (API → dédup → import).
- **68 divergences** anciennes : traitées (13 corrigées + 42 remplacées) ; le reste était
  incomparable/hors banque.

### Comment récupérer un cours Udemy (rappel méthode)
Via Claude in Chrome (session Udemy de l'utilisateur), API : `/api-2.0/courses/<id>/subscriber-curriculum-items/`
(liste des quiz) puis `/api-2.0/quizzes/<id>/assessments/?fields[assessment]=@all` (énoncé +
`answers` + `correct_response` lettre). `course_id` visible dans les requêtes réseau
`/users/me/subscribed-courses/<courseId>/...`. Export JSON → `scripts/import_udemy_course.py`.

### Clôture
- **Arrêter le pont** `cmdbridge.sh` (Ctrl+C dans le Terminal) en fin de session.

---

## ✅ SESSION 23 mai 2026 — finalisation Phase 7 + nettoyage infra (v2.2.0)

**Tout est en prod et vérifié.** App : https://quiz-odoo.picvert-senedoo.org — **v2.3.0**,
3251 questions, HTTPS OK, **accès protégé par login**. VPS dédié `odoo-quiz` (178.104.211.37).

### Fait cette session
1. **Phase 7.2 — Bouton « Signaler »** : sur chaque question du quiz (en-tête de carte,
   à côté du bouton FR). Appelle `POST /api/bank/<id>/flag` → `status=flagged` (donc exclue
   du quiz). L'ancien status est conservé dans `prev_status`. Motif facultatif.
2. **Phase 7.3 — Page de relecture admin** : `/admin/review`, protégée par jeton. Onglets
   Tout / À revoir (unverified) / Signalées (flagged) + compteurs. Pour chaque question :
   **Valider** (→ `verified_by_admin`, redevient visible), **Modifier** (édition inline
   énoncé / réponses / explications via `PUT /api/bank/<id>`), **Supprimer** (DELETE).
   Endpoints : `GET /api/admin/review`, `POST /api/admin/questions/<id>/validate`,
   `DELETE /api/admin/questions/<id>` — gardés par le jeton (`?token=`, header
   `X-Admin-Token`, ou cookie `admin_token`).
3. **Jeton admin** : `config.json` → `admin.token` (gitignoré ; documenté dans
   `config.example.json`). Accès : `…/admin/review?token=<jeton>`. Lien « 🔧 Admin » ajouté
   dans l'en-tête du quiz.
4. **Décommissionnement ancien VPS (niokolo-rs)** : service `odoo-quiz` arrêté + désactivé
   (port 5001 fermé) ; bloc `quiz-odoo` retiré du Caddyfile (backup
   `/etc/caddy/Caddyfile.bak.20260523T185712Z`), `caddy validate` OK + reload. **ResourceSpace
   100 % intact** (port 8080, apache2, mariadb, bloc Caddy conservés ; HTTPS 200). Réversible :
   restaurer le `.bak` + `sudo systemctl enable --now odoo-quiz`. ⚠️ toujours `niokolo` (jamais root).
5. **Déploiement** : backups VPS (`app.py.bak.20260523T185402Z`, `config.json.bak.*`), scp
   `app.py`, ajout jeton admin dans `config.json` in place (secrets préservés), contrôle de
   syntaxe, restart `odoo-quiz.service`.
6. **Correctif v2.2.1 — picker module du quiz** : sélectionner un module (ex. CRM) sur l'écran
   de démarrage ne faisait rien et « Commencer » restait grisé. Cause : `total` déclaré `const`
   puis réassigné dans `updateQuizState()` → `TypeError: Assignment to constant variable` (bug
   latent depuis v2.1.0, jamais testé côté quiz). Corrigé en `let total`. CRM = 38 q (v19) / 31 (v18).
7. **v2.3.0 — Login global de l'appli** (demande Patrice : un seul mot de passe pour toute
   l'appli, équipe Senedoo). Protection **HTTP Basic Auth au niveau de Caddy** sur tout le site
   `quiz-odoo.picvert-senedoo.org`, **sauf `/health`** (laissé ouvert pour la supervision).
   Identifiant `patrice@senedoo.com` ; mot de passe stocké **uniquement en hash bcrypt** dans
   `/etc/caddy/Caddyfile` (jamais en clair). Backup Caddyfile : `/etc/caddy/Caddyfile.bak.*`.
   Conséquence : le **jeton admin a été retiré** (`config.json` n'a plus de section `admin`) — la
   page `/admin/review` s'appuie désormais sur ce login global (code adapté : sans `admin.token`,
   l'admin est accessible aux utilisateurs déjà authentifiés). Changer le mot de passe :
   `caddy hash-password --plaintext '...'` puis remplacer le hash dans le Caddyfile + `systemctl reload caddy`.
8. **v2.3.1 — capture : bouton « Ignorer » corrigé.** Le POST `/import-capture` validait la
   bonne réponse AVANT de lire le choix add/update/ignore → « choisissez la bonne réponse 1..N »
   bloquait l'ignore. On lit le choix d'abord ; `correct_index` non requis si la carte est ignorée.
9. **Régression login → favori de capture, corrigée (Caddy).** Le login global bloquait le favori
   « pleine page » : il charge `/static/odoo_fullpage_capture.js` + `/static/quiz_dom_extract.js`
   et POST `/import-capture/fullpage` **en cross-origin depuis odoo.com/Udemy, sans le login**
   (impossible à transmettre) → 401. Sans le favori, on retombait sur un collage manuel (capture
   brute > 8000 px, sans DOM) → la **vision Anthropic échoue** (limite 8000 px) → « Vision
   unavailable ». **Fix** : ces 3 URL exemptées du login dans le Caddyfile (matcher `@needauth`
   + `not path …`) ; tout le reste (hub `/import-capture`, images `/static/doc_media`) reste
   protégé. Le favori re-télécharge ≤ 4096 px + envoie le DOM → plus d'erreur vision sur la
   taille, le texte vient du DOM (vision = secours). Diag : modèle/clé OK, seul le dépassement
   8000 px faisait échouer la vision. Amélioration possible : pour une question qui dépend d'une
   image (« In the image… »), n'envoyer à Claude que le **recadrage** (via `crop_rel` du DOM).
10. **v2.4.0 — capture : image recadrée + filet anti-dépassement.**
    - `question_images.crop_region_to_temp_png()` : découpe la région `crop_rel` (marge 4 %) en
      PNG temporaire. `import_preview_enrich._answer_image_paths()` envoie désormais à Claude **le
      recadrage de l'image concernée** (caché dans `item['_answer_crop_path']`) au lieu de toute la
      page → réponses plus fiables sur les questions « à image », et insensible à la taille.
    - `quiz_llm._vision_safe_image()` : **filet central** dans `_messages_api_vision` — toute image
      dont un côté dépasse 7800 px est réduite avant l'appel → plus jamais d'erreur « 8000 px »
      (couvre aussi les collages manuels de captures brutes).
    - Vérifié en prod : image 1000×9000 → vision OK (réduite), crop d'une région → PNG 816×960.
    - Rappel : « toute la page » reste le rôle du **favori** (DOM + scroll complet) ; le partage
      d'écran « Capturer et analyser » ne voit par nature que la partie visible.
11. **v2.4.1 — garder le texte du DOM, n'envoyer que l'image à Claude.** Avant, dès qu'UNE
    question s'appuyait sur une image, `dom_items_need_vision_fallback()` jetait toute
    l'extraction DOM et refaisait TOUT (texte compris) par vision. Désormais on ne rebascule en
    vision que si le **texte** (énoncé/options) manque réellement ; une question « à image »
    garde son texte du DOM, et seule l'**image** part vers Claude (recadrage à l'étape réponse).
    Vérifié : `needs_image`+texte complet → pas de fallback ; texte manquant → fallback.
    Reste à faire (amélioration, nécessite de **re-glisser le favori**) : faire calculer par
    l'extracteur DOM (`quiz_dom_extract.js`) la **boîte `crop_rel`** de l'image dans chaque
    question, pour n'envoyer/stocker QUE la petite image (aujourd'hui `crop_rel` du DOM = null →
    on retombe sur la page entière réduite).
12. **v2.4.2 — image de question hybride : lien `<img>` sinon recadrage.** (a) Front (favori) :
    `quiz_dom_extract.js` capte `image_url` (URL absolue d'une vraie `<img>`, hors `data:`) + la
    réf. de l'élément ; `odoo_fullpage_capture.js` calcule `crop_rel` (boîte de l'élément dans la
    capture) puis retire la réf. avant l'envoi JSON, et charge l'extracteur avec `?v=Date.now()`
    (anti-cache). (b) Back : `question_images.download_image_to_temp()` télécharge l'URL avec
    **garde-fous anti-SSRF** (http(s) seulement, IP privée/loopback/link-local/réservée refusées,
    taille ≤ 12 Mo, `Content-Type: image/*`, validation PIL) ; `save_question_image_from_screenshot`
    accepte `image_url` (priorité au lien) ; `_answer_image_paths` priorise URL → recadrage →
    page entière ; `image_url` propagé par `_normalize_dom_item` + `validate_udemy_item` + 4 call
    sites d'`import_udemy`. Vérifié prod : SSRF bloqué (loopback/link-local/privée/scheme → None),
    image publique téléchargée (80×80), statics servis.
    ⚠️ **Action requise** : **re-glisser le favori** « 📷 Quiz — pleine page » depuis
    `/import-capture` (la version est figée dans le favori : l'ancien charge les anciens scripts).
13. **v2.4.3 — plafond capture 4 → 6 questions/page.** `MAX_QUESTIONS_PER_CAPTURE = 6`, prompts
    vision et texte d'interface alignés. Une page à 5 (ou 6) questions est désormais entièrement
    capturée (DOM comme vision).
14. **v2.5.0 — escalade Opus 4.6 + accord voisin (qualité des suggestions).**
    - `app.llm.escalation_model()` (config `anthropic.escalation_model`, défaut `claude-opus-4-6`,
      `""` pour désactiver) ; `suggest_answer(..., model=...)` permet l'override de modèle.
    - `_api_enrich` : si la suggestion Sonnet est de confiance non-haute ou sans réponse, **relance
      avec Opus** (même contexte + image) et adopte le résultat s'il est au moins aussi confiant.
    - `_apply_neighbor_agreement` : compare la réponse choisie au plus proche voisin banque ;
      similarité ≥ 0,97 et même réponse → confiance **haute** (accord) ; ≥ 0,985 et réponse
      différente → confiance **basse** + « à revoir » (désaccord).
    - Vérifié prod : override Opus OK (`model=claude-opus-4-6`), accord→haute, désaccord→basse.
    - Coût indicatif : escalade ≈ 0,04–0,06 $/question (Opus 4.6 = 5 $/25 $ le M ; Batch −50 %).

### Tests réalisés (prod, HTTPS)
- Auth : `/admin/review` → 403 sans/mauvais jeton, 200 avec. `/api/admin/review` → 403 sans
  jeton ; counts `{unverified:678, flagged:0}`.
- Wiring : flag / validate / delete sur id inexistant → 404 ; delete sans jeton → 403.
- Round-trip réel q911 : unverified → flag (`flagged`) → validate (`verified_by_admin`) →
  **restauré en `unverified`** (données prod inchangées).
- Login global (v2.3.0) : `/` et `/banque` → 401 sans identifiants, 200 avec ; mauvais mot de
  passe → 401 ; `/health` → 200 (ouvert) ; `/admin/review` → 401 sans identifiants, 200 avec
  (et plus besoin de jeton).

### Nouveau status
- `verified_by_admin` : question validée manuellement depuis la page admin (visible au quiz,
  comme `verified_by_judge`). Non listée dans la file de relecture.

---

## 🚀 PROCHAINE SESSION — COMMENCER ICI

**État au 20 mai 2026 (soir) — tout est en prod et fonctionne.**

- **App en ligne** : https://quiz-odoo.picvert-senedoo.org — **v2.1.1**, 3251 questions, HTTPS OK.
- **Infra** : VPS Hetzner **dédié** `odoo-quiz` (projet Hetzner « Odoo-quiz », CX23, **178.104.211.37**),
  séparé de la médiathèque (qui reste sur `niokolo-rs` / 46.224.219.81).
- **Accès** : `ssh -i ~/.ssh/niokolo_claude root@178.104.211.37` (ou `senedoo@…` pour l'app).
- **Git** : à jour et poussé (`PatriceWeisz/odoo-quiz`, branche `main`).

### Fait cette session
1. **Migration** du quiz vers un VPS dédié (projet + serveur Hetzner créés, app + données + secrets
   migrés, Caddy + systemd, DNS OVH basculé A+AAAA, cert Let's Encrypt).
2. **Refonte du filtre thématique** de la banque (`bank_topics.py`) : filtre par **vrai module**
   + inférence (667/670 classées), menu 2 niveaux + compteurs, filtres combinés, sélecteur de
   module dans l'éditeur, suppression du panneau « Importer depuis Odoo ». (v2.0.0 → v2.1.0)
3. **Ajustements UI** éditeur : champs d'énoncé agrandis, section image épurée. (v2.1.1)

### À FAIRE en priorité la prochaine fois
1. ⏳ **Désactiver le quiz résiduel sur l'ancien VPS** (`niokolo-rs`) — procédure + incident
   fail2ban détaillés plus bas (⚠️ **ne jamais se connecter en root** sur l'ancien serveur).
2. **Réflexe versioning** : bumper `APP_VERSION` (app.py) à chaque changement fonctionnel + déployer.
3. Reliquats de la roadmap initiale : Phase 7.2 (bouton Signaler), 7.3 (page admin review).
4. (Option) éditeur du champ `module` : fait ; envisager de re-juger les `unverified`.

### Rappels de clôture
- Le **pont `cmdbridge.sh`** tournait sur le Mac (Terminal) — pensez à l'arrêter (Ctrl+C) en fin de session.
- Un script **`cleanup.sh`** est fourni à la racine pour le ménage du dossier (à relire puis lancer).

---

## ⚡ MIGRATION INFRA — session 20 mai 2026 (soir) : VPS dédié

**Le quiz tourne désormais sur un serveur Hetzner DÉDIÉ, séparé de la médiathèque.**

| Élément | Avant | Après |
|---|---|---|
| Projet Hetzner | `Mediathèque Picvert UICN` (mutualisé) | **`Odoo-quiz`** (nouveau projet dédié) |
| Serveur | `niokolo-rs` (CPX32, partagé avec ResourceSpace) | **`odoo-quiz`** (CX23, Falkenstein, dédié) |
| IPv4 | 46.224.219.81 (partagée) | **178.104.211.37** |
| IPv6 | 2a01:4f8:c0c:e09e::1 | **2a01:4f8:c015:be2b::1** |
| OS | Ubuntu 24.04 | Ubuntu 24.04.4 LTS (Python 3.12.3) |
| Coût | — | ~4,49 €/mois (CX23 + IPv4) |
| Caddy | mutualisé (ResourceSpace + quiz) | dédié quiz uniquement |
| Cert HTTPS | — | Let's Encrypt (renouv. auto), valable jusqu'au 18 août 2026 |

**DNS (OVH)** : `quiz-odoo.picvert-senedoo.org` A+AAAA → nouveau VPS (basculé via le manager OVH).
Les enregistrements `picvert-senedoo.org` / `www` restent sur 46.224.219.81 (ResourceSpace).

**Validé le 20/05** : `/health` = `{"questions":3251,"status":"ok","version":"2.0.0"}`,
images doc_media + question_media servies en HTTPS, redirection HTTP→HTTPS OK.

**Migration réalisée** : tar streamé (188 MB, hors .venv) ancien→nouveau via le Mac,
venv recréé (`pip install -r requirements.txt`), config.json (secrets) transféré,
service systemd `odoo-quiz.service` identique, Caddy quiz-only.

### Nouveaux accès SSH (même clé `~/.ssh/niokolo_claude`)
```bash
ssh -i ~/.ssh/niokolo_claude root@178.104.211.37      # nouveau VPS quiz (root)
ssh -i ~/.ssh/niokolo_claude senedoo@178.104.211.37   # nouveau VPS quiz (app)
# Anciens (désormais ResourceSpace uniquement) :
ssh -i ~/.ssh/niokolo_claude senedoo@picvert-senedoo.org
ssh -i ~/.ssh/niokolo_claude niokolo@picvert-senedoo.org
```

### ⏳ À FAIRE — désactiver le quiz résiduel sur l'ancien VPS (niokolo-rs)
Décidé le 20/05 : **on le fera plus tard** (le quiz tourne encore en fallback sur l'ancien
VPS, mais le DNS pointe déjà vers le nouveau, donc aucun impact utilisateur).

⚠️ **Incident à connaître** : une tentative de connexion `ssh root@picvert-senedoo.org` a
déclenché un **ban fail2ban** sur l'IP du Mac (symptôme : `No route to host` / `Connection
refused` sur le port 22). **Ne jamais se connecter en root** sur l'ancien VPS → toujours
`niokolo` (sudo NOPASSWD). Le ban se lève seul (~10 min) ou via la console Hetzner (KVM)
si besoin. ResourceSpace n'est pas affecté (répond en HTTPS pendant le ban).

Procédure exacte à exécuter quand on veut désactiver (réversible, **ne supprime aucun fichier**) :
```bash
SSH="ssh -i ~/.ssh/niokolo_claude niokolo@picvert-senedoo.org"
# 1) arrêter + désactiver le service quiz
$SSH "sudo systemctl stop odoo-quiz.service && sudo systemctl disable odoo-quiz.service"
# 2) backup + retirer le bloc quiz-odoo du Caddyfile (garder le bloc ResourceSpace !)
$SSH "sudo cp -a /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak.\$(date +%Y%m%dT%H%M%SZ)"
#    puis éditer /etc/caddy/Caddyfile pour SUPPRIMER uniquement :
#      quiz-odoo.picvert-senedoo.org { encode gzip zstd; reverse_proxy 127.0.0.1:5001 }
# 3) valider AVANT reload, puis recharger
$SSH "sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy"
# 4) vérifier : ResourceSpace toujours up, port 5001 fermé
$SSH "curl -s -o /dev/null -w 'RS:%{http_code}\n' http://127.0.0.1:8080/"
```
Pour réactiver (rollback) : `sudo systemctl enable --now odoo-quiz.service` + restaurer le
`.bak` du Caddyfile + `sudo systemctl reload caddy` + rebasculer le DNS si nécessaire.

### Autres points d'attention post-migration
- **`.ovh-creds.env` = placeholders** (`...`) → bascule DNS faite manuellement via le manager OVH.
  Pour automatiser plus tard : générer un token API OVH (droits GET/PUT/POST sur
  `/domain/zone/picvert-senedoo.org/*`) et le coller dans `.ovh-creds.env`.
- **fastembed** : modèle re-téléchargé à chaque restart (PrivateTmp). Comportement identique
  à l'ancien VPS. Optimisation possible : cache persistant.
- **gunicorn** : warning `Read-only file system: /home/senedoo/.gunicorn` (control socket) —
  non bloquant, déjà présent avant.

---

## ⚡ AMÉLIORATION FILTRE BANQUE — session 20 mai 2026 (soir) — v2.1.0

Refonte du **filtre thématique** de la page `/banque` (avant : inférence par mots-clés
qui ignorait le champ `module`, tout retombait sur « Général / Odoo »).

- **Nouveau module `bank_topics.py`** : catalogue catégorie→module (libellés FR),
  inférence de module pour les 670 questions sans `module` (Udemy/anciennes) via **kNN
  sur les embeddings déjà calculés** (fallback mots-clés), arbre thématique + compteurs.
  → **667/670** désormais classées, 11 catégories à 2 niveaux.
- **`/api/bank`** : filtre par `mod:<module>` / `cat:<catégorie>` + filtres combinés
  `source` / `version` / `status` / `tier` ; renvoie `topic_tree` (avec compteurs).
- **`/api/bank/modules`** : catalogue complet pour le sélecteur de l'éditeur.
- **`/api/bank/<id>`** GET/PUT : exposent + enregistrent le champ `module`.
- **`banque.html`** : menu déroulant à 2 niveaux (optgroups + compteurs) qui se
  rafraîchit au changement de cert ; 4 filtres combinés ; affichage « Catégorie › Module » ;
  **sélecteur de module dans l'éditeur** (remplace l'ancien champ texte « topic »).
- **Panneau « Importer depuis Odoo » supprimé** (remplaçait questions.json — devenu
  inutile/risqué avec 3251 questions). Le backend `/api/odoo/*` reste mais n'est plus exposé.
- **APP_VERSION → 2.1.0**. Déployé sur le VPS dédié, commits poussés.

À considérer plus tard : l'inférence de module est cachée en mémoire (recalcul si
`questions.json` change) ; on pourrait la persister. Le champ legacy `topic` n'est plus
utilisé par le filtre.

---

## État courant des phases — fin session 20 mai 2026

| Phase | État | Détail |
|---|---|---|
| 1 — Déploiement VPS | ✅ | https://quiz-odoo.picvert-senedoo.org/ — HTTPS, gunicorn `odoo-quiz.service`, Caddy mutualisé avec ResourceSpace |
| 2 — Couverture v19 inventory | ✅ | 233/232 chunks v18/v19 (parité) |
| 3 — Scope 3 tiers | ✅ | `app/study_modules.py`, 39 modules, validateur de chemins |
| 4 — Pipeline images | ✅ | 1614 pages traitées, **3110 doc_images**, **30667 chunk_images**, 4603 stockées / 1259 skipped |
| 5.1 schéma question étendu | ✅ | `app/question_schema.py` |
| 5.2 calibrage Udemy | ✅ | `data/calibration_report.md` |
| 5.3 plan génération | ✅ | `data/generation_plan.json` — 2999 q atteignables |
| 5.4 mini-run 50 q | ✅ | 96 q générées sur inventory v19 |
| 5.5.a Orchestrateur multi-modules | ✅ | `scripts/run_full_generation.py` — Batch API mono-batch |
| 5.5.b Pipeline judge | ✅ | `scripts/judge_questions.py` — 5 critères, MIN, groupage 4q/chunk |
| 5.5.c Dédup vectorisée | ✅ | `scripts/dedupe_pending.py` — numpy pur, threshold 0.92 |
| 5.5.d Few-shot rotatif par module | ✅ | `scripts/build_udemy_module_map.py` + patch `pick_few_shot` |
| **5.5 Full run** | ✅ | **2708 q générées → 2007 verified + 678 unverified + 86 reject + 137 dedup**. Coût $13.25 + $3.77 judge = **$17.02** |
| 5.6 Insertion atomique | ✅ | `scripts/insert_pending_questions.py` — 670 → **3251 q** insérées |
| 5.7 Embeddings nouvelles q | ✅ | Warmup auto via `bank_embeddings.warmup_bank_embeddings()` — 12.7 sec sur 3251 q |
| 5.8 Invalidate cache | ✅ | Intégré dans `save_bank_atomic()` |
| 6 traduction FR Udemy | ✅ | `scripts/translate_udemy_batch.py` — 643/643 traduites, coût $1.12 |
| 7.1 Filtre module obligatoire | ✅ | Picker module dans header, `/api/modules`, exclude unverified par défaut |
| 7.2 Bouton Signaler | ✅ | Bouton sur chaque question → `POST /api/bank/<id>/flag` → status=flagged (v2.2.0, 23 mai) |
| 7.3 Page admin review | ✅ | `/admin/review` (jeton) : Valider / Modifier / Supprimer unverified+flagged (v2.2.0, 23 mai) |

**App en ligne : v2.0.0 sur https://quiz-odoo.picvert-senedoo.org**

---

## Bilan session 20 mai 2026 — chiffré

| Métrique | Valeur |
|---|---|
| Questions ajoutées | **+2 581** (670 → 3 251, ×4.85) |
| Questions Udemy traduites | 643/643 (100 %) |
| Coût LLM total session | **$18.29** (vs briefing $16-27) |
| Tokens IN cached (judge) | 1 289 034 (économie ~$1.55) |
| Durée pipeline (gen+judge+trad+insert) | ~20 min vs ETA 4-5h |
| Commits poussés cette session | 16 (de a0aa30f → 802d489) |

---

## Stats banque finale

```
Total questions       : 3 251
  - udemy             : 643   (bilingues EN/FR depuis Phase 6)
  - claude (générées) : 2 586 (Phase 5.5)
  - user              : 1
  - sans source       : 21    (anciennes système)

Par status (générées uniquement) :
  - verified_by_judge : 1 903 (judge score ≥ 4 — affichées par défaut)
  - unverified        : 678   (judge score = 3 — cachées par défaut)
  - flagged           : 223   (judge ≤ 2 ou dedup duplicate — exclues)

Par target_version :
  - 19.0  : 1 547
  - 18.0  : 1 035
  - both  :   201
  - null  :   468   (anciennes, target_version non renseigné)

Par tier (générées) :
  - cert  : 1 844
  - tier1 :   476
  - tier2 :   261
```

---

## Architecture du pipeline 5.5 (résumé)

```
                ┌──────────────────┐
                │ generation_plan  │ (5.3)
                │ .json — 76 paires│
                │ (module,version) │
                └────────┬─────────┘
                         │
              ┌──────────▼────────────────┐
              │ run_full_generation.py     │  5.5.a
              │ Batch Anthropic mono-batch │
              │ (777 appels, $13.25)        │
              └──────────┬────────────────┘
                         │ pendings JSONL
              ┌──────────▼────────────────┐
              │ judge_questions.py         │  5.5.b
              │ 5 critères / 4 q/chunk    │
              │ ($3.77 dont $1.55 caching)│
              └──────────┬────────────────┘
                         │ status updates
              ┌──────────▼────────────────┐
              │ dedupe_pending.py          │  5.5.c
              │ cosine 0.92 vs bank +     │
              │ intra-pending (numpy)     │
              └──────────┬────────────────┘
                         │ flagged duplicates
              ┌──────────▼────────────────┐
              │ insert_pending_questions   │  5.6
              │ ré-attribution qid/aid    │
              │ backup + atomic save      │
              └──────────┬────────────────┘
                         │ 2581 q insérées
              ┌──────────▼────────────────┐
              │ warmup_bank_embeddings()  │  5.7
              │ 12.7s pour 3251 q         │
              └────────────────────────────┘
```

---

## Optimisations actives (toutes mesurées effectives)

1. **Batch API Anthropic mono-batch** — −50 % coût vs sync (1 batch_id par étape)
2. **Prompt caching `ephemeral`** — SYSTEM_PROMPT 7259 chars / 2074 tokens (au-dessus du seuil 1024) → 1.3M tokens cached sur le judge full, économie ~$1.55
3. **per_call = 4 questions** — amortit le contexte chunk + few-shot
4. **Module-inference Udemy 100 % numpy** — 3 sec sur 643 titres × 5217 chunks
5. **Dédup vectorisée 100 % numpy** — matmul, pas de boucle Python
6. **Embeddings nouvelles questions en batch=128** (fastembed local)
7. **State files atomiques** — `run_state.json`, `judge_state.json`, `translate_state.json` permettent la reprise via `--poll <batch_id>` sur n'importe quelle session
8. **Pipeline indépendant du bridge** — `nohup` côté VPS, peut continuer si bridge stoppé
9. **JSONL streamé** + `.tmp + replace` partout (atomique)
10. **Idempotence** — `judge_questions` skip les questions avec `judge_score is not None`, `insert_pending_questions` skip les questions avec `inserted_at`

---

## Écarts notables (mises à jour 20 mai)

1. **Caddy au lieu de nginx** — inchangé (session précédente)
2. **Sous-domaine `quiz-odoo.picvert-senedoo.org`** — inchangé
3. **`app/study_modules.py` au lieu de `app/config.py`** — inchangé
4. **WSGI shim `wsgi.py`** — inchangé
5. **`/api/ask` patché SDK Anthropic** — inchangé
6. **Anomalies SQL `audit_doc_coverage`** — inchangé (cosmétique)
7. **Bridge timeout 1800 s** — inchangé
8. **APP_VERSION → 2.0.0** (était 1.15.1) — bump majeur suite enrichissement banque

---

## Dette technique / TODOs reportés

### Court terme (à faire dans une session)
- **7.2 Bouton "Signaler"** sur chaque question → passe à `status=flagged` (UX + endpoint POST)
- **7.3 Page admin review** des `unverified` / `flagged` avec [Valider / Modifier / Supprimer]
- **Stats inserted vs flagged par tier** dans `/api/modules` ou un endpoint dédié (utile pour le pilotage)
- **Persistance du module choisi** côté serveur (actuellement non persistant, perdu au refresh — plug sur `app_settings.sqlite`)

### Moyen terme
- 28 questions Udemy hors normes (option count = 2 ou 5) — à nettoyer
- 2 questions Udemy avec ≠1 bonne réponse — à nettoyer
- `productivity/knowledge` v19-only mais 1 chunk en v18 — cosmétique
- 28 q v19 anciennes n'ont pas de `tier` — peuvent perturber le picker, à ré-classer
- 678 unverified : ré-juger avec un prompt judge ajusté pour distinguer "review borderline" vs "à revoir réellement" (actuellement tous au même status)

### Long terme
- Sphinx alt-text images Odoo : compenser via titre + section du chunk
- gunicorn warning `ProtectHome=read-only` — relocaliser control socket
- Sitemap.xml Odoo 18/19 → fallback `searchindex.js` (info)

---

## Commits Phase 5.5 + 6 + 7 (chronologique)

```
d02d592 feat(deploy): use Anthropic SDK in /api/ask + WSGI shim
cebeced feat(modules): periodic study modules in 3 tiers + path validator
4f597fb feat(images): doc-image pipeline (scénario A)
d6b2518 feat(gen): question schema + Udemy calibration + generation plan
0169d3c perf(images): throttle 0s pour images CDN + --skip-done
278adc8 feat(gen): generator + pending inspector (Phase 5.4 mini-run)
26c03c2 perf(gen): mode --async (AsyncAnthropic + semaphore) + prompt caching
6c6a12c docs(session): notes de passation session 19 mai
fb13e8e feat(5.5): pipeline génération + judge + dedup + few-shot rotatif
dc77fe3 fix(5.5.b): custom_id Batch judge < 64 chars
d6444e8 fix(5.5): custom_id Batch API conforme au pattern
5d1e6ef fix(5.5.a): prompt génération — distracteurs subtils + 4 options défaut
fd1e6b9 fix(5.5.b): idempotence judge — skip questions déjà jugées
3161d26 feat(6): traduction FR Udemy via Batch API
ee72641 feat(5.6): insertion atomique pendings dans questions.json
6f2e44d release: v2.0.0 — banque enrichie 670 → 3251 questions
802d489 feat(7): filtre module obligatoire + exclude unverified par défaut
```

---

## Quick commands pour la nouvelle session

```bash
# 1. État service VPS
curl -s https://quiz-odoo.picvert-senedoo.org/health | python3 -m json.tool

# 2. Stats DB courantes (chunks + images)
ssh -i ~/.ssh/niokolo_claude senedoo@picvert-senedoo.org \
  "cd /opt/odoo-quiz && ./.venv/bin/python -c '
import sqlite3
c = sqlite3.connect(\"data/odoo_docs.sqlite\")
print(\"chunks:\", c.execute(\"SELECT COUNT(*) FROM chunks\").fetchone()[0])
print(\"doc_images:\", c.execute(\"SELECT COUNT(*) FROM doc_images\").fetchone()[0])
print(\"chunk_images:\", c.execute(\"SELECT COUNT(*) FROM chunk_images\").fetchone()[0])'"

# 3. Stats banque locale
python3 -c "
import json
from collections import Counter
qs = json.load(open('questions.json'))['questions']
print('total:', len(qs))
print('sources:', dict(Counter(q.get('correct_answer_source') for q in qs)))
print('statuses:', dict(Counter(q.get('status') for q in qs)))"

# 4. Test /api/modules
curl -s 'https://quiz-odoo.picvert-senedoo.org/api/modules?cert=19.0' \
  | python3 -m json.tool | head -30
```

---

## Bridge `cmdbridge.sh` — état au 20 mai 23h (après migration)

- Prochain `.req` à utiliser : **`102`** (le dernier traité était `101` — bascule HTTPS)
- Le bridge tourne avec timeout par défaut 1800 s (override par `# TIMEOUT=N`)
- Les `.req` 084→101 = audit infra + création VPS + migration + bascule DNS/HTTPS

---

## Accès SSH (rappel)

```bash
ssh -i ~/.ssh/niokolo_claude senedoo@picvert-senedoo.org   # app quiz
ssh -i ~/.ssh/niokolo_claude niokolo@picvert-senedoo.org   # admin (sudo)
```

---

*Document mis à jour le 20 mai 2026 au soir, après migration vers VPS dédié (projet Hetzner Odoo-quiz, 178.104.211.37).*
*Prochaine session : nettoyage ancien VPS (désactiver service+Caddy quiz sur niokolo-rs), puis Phase 7.2 (Signaler) + 7.3 (Page admin review) + Tests utilisateur du picker module.*
