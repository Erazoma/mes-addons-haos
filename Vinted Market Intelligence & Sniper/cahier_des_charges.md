# Cahier des Charges : Add-on Home Assistant "Vinted Market Intelligence & Sniper"

## 1. Contexte & Objectif
Développer un add-on complet pour **Home Assistant OS (HAOS)** destiné à un usage personnel de veille et d'achat sur Vinted.
L'application fonctionne en **deux phases séquentielles** pilotées depuis une interface web intégrée (Ingress Home Assistant) :
1. **Phase 1 — L'Observatoire (Benchmark) :** Collecte massive et haute fréquence d'annonces sur une période définie (ex: 7 jours) pour analyser le marché réel (prix de vente effectifs, vitesse de rotation, tendances sémantiques) via une IA multimodale.
2. **Phase 2 — Le Sniper (Veille Temps Réel) :** Surveillance continue et ultra-réactive basée sur les enseignements de la Phase 1, envoyant des alertes Telegram instantanées et "prêtes à acheter" uniquement pour les annonces correspondant à 100 % aux critères.

---

## 2. Architecture Technique & Stockage (Double Base SQLite)
L'add-on doit être léger, autonome et stocker ses données de manière persistante dans le dossier `/data` de Home Assistant via **deux bases de données SQLite distinctes** :

### Base 1 : `observatory.db` (Étude de marché exhaustive)
Doit stocker le **maximum de données brutes** pour chaque annonce captée durant la Phase 1 afin de permettre une analyse statistique et sémantique complète :
* `vinted_id` (Clé unique)
* `search_id` (Lien vers la recherche cible)
* `keyword_used` (Mot-clé ayant permis de trouver l'annonce)
* `title` (Titre complet)
* `description` (Description intégrale)
* `category`, `brand`, `condition` (Catégorie, marque, état déclaré)
* `initial_price` (Prix de mise en ligne)
* `price_history` (JSON listant les variations de prix observées avec horodatage)
* `final_price` (Prix au moment de la vente ou dernier prix affiché)
* `status` (`active`, `sold`, `deleted`) — Distinction stricte entre officiellement vendu et supprimé.
* `created_at_vinted`, `first_seen_at`, `ended_at` (Horodatages précis permettant de calculer la durée exacte avant vente)
* `url` (Lien direct)
* `image_url` / `local_image_path` (Photo principale pour analyse Vision)
* `ai_status` (`pending`, `validated`, `rejected`) et `ai_reason`

### Base 2 : `sniper_audit.db` (Suivi temps réel léger & Audit)
Doit stocker l'historique léger (**sans conserver les photos**) de toutes les annonces traitées en Phase 2 :
* `vinted_id` (Clé unique)
* `search_id`
* `keyword_used` (Permet de calculer le taux de pertinence de chaque mot-clé dans le temps)
* `title`
* `last_seen_price` (Permet de détecter si une annonce déjà analysée et validée subit une **baisse de prix**)
* `ai_decision` (`retained`, `rejected`)
* `ai_reason` (Justification courte de la décision)
* `notified_at` (Horodatage d'envoi sur Telegram, si applicable)

---

## 3. Architecture IA, Quotas & Règles Métiers
L'application s'appuie sur l'API **Google Gemini (Tiers Gratuit)** avec une architecture résiliente :
* **Système de Fallback en cascade :** L'utilisateur configure une liste ordonnée de 3 à 4 modèles Gemini (ex: `gemini-2.5-flash`, `gemini-2.5-flash-lite`, `gemini-2.0-flash`, etc.). Si un modèle renvoie une erreur de quota (`HTTP 429`) ou une indisponibilité, le code bascule automatiquement et de manière transparente sur le modèle suivant dans la liste.
* **Règle de "Tolérance Zéro sur le Doute" :** Lors de la validation d'une annonce, l'IA doit appliquer une rigueur absolue sur les spécifications techniques exigées par l'utilisateur (ex: si l'utilisateur demande 16 Go de RAM DDR4, rejeter impérativement 4 Go, 8 Go ou DDR3). **En cas d'ambiguïté persistante (même après analyse de l'image), l'annonce doit être rejetée (`rejected`).**

---

## 4. Sécurité Scraping & Anti-Ban
Vinted bloquant les comportements robotiques, le module de collecte doit intégrer :
* Initialisation et renouvellement propre des cookies/headers de session.
* **Comportement humain :** Intervalles de scan aléatoires (jitter) et pauses régulières programmées.
* **Disjoncteur automatique (Circuit-Breaker) :** Si le scraper rencontre 3 erreurs consécutives ou un blocage (`HTTP 403`, `429`, Captcha), il se met immédiatement en pause pendant plusieurs heures et envoie une alerte technique sur Telegram.

---

## 5. Interface Web (Ingress Home Assistant)
Une interface web moderne, claire et responsive (accessible directement depuis le panneau latéral de Home Assistant via Ingress) regroupant :
1. **Tableau de bord (Dashboard) :** Liste des produits suivis, état actuel (`Phase 1 : Observation`, `En attente de validation`, `Phase 2 : Sniper actif`, `En pause`), compteurs d'annonces en direct et bouton d'activation manuelle de la Phase 2.
2. **Création / Édition d'une recherche :**
   * Saisie du produit cible et de ses exigences strictes.
   * **Assistant IA de mots-clés :** Bouton permettant d'envoyer le produit cible à Gemini pour qu'il génère et propose simultanément :
     1. Une **liste de mots-clés de recherche élargis** (synonymes, fautes fréquentes, termes génériques).
     2. Une **liste initiale de mots-clés d'exclusion (Blacklist)** (ex: `HS`, `pour pièces`, `boîte vide`, `câble seul`).
   * L'utilisateur peut éditer librement ces deux listes avant de lancer la Phase 1.
   * Réglage de la durée d'observation (par défaut : 7 jours) et du prix plafond (pour la Phase 2).
3. **Page Statistiques & Compte-rendu :**
   * Sélecteur de produit.
   * Affichage du **compte-rendu complet généré par l'IA** à l'issue de la Phase 1 (prix moyen de vente, prix plancher/bonne affaire, temps moyen de vente, et analyse sémantique des descriptions avec suggestions de mots-clés positifs ou négatifs à ajuster).
   * **Métriques clés (calculées strictement sur les annonces `validated` + `sold`) :** Volume de ventes, prix moyen, prix médian, délai moyen de vente.
   * **Indicateur des annonces supprimées (`deleted`) :** Volume et pourcentage par rapport au total pour détecter les ventes hors-plateforme ou annonces retirées.
   * **Audit Phase 2 :** Ratio annonces analysées vs retenues par mot-clé (pour identifier les mots-clés trop bruités à supprimer manuellement).
4. **Page Configuration Générale :**
   * Paramètres **Telegram** : `Bot Token` et `Chat ID` (notifications sortantes uniquement).
   * Clé API **Google Gemini** et **gestionnaire de la liste ordonnée des modèles** (ajout, suppression, réorganisation de l'ordre de priorité).

---

## 6. Workflows Détaillés

### Workflow Phase 1 : L'Observatoire
1. **Collecte continue :** Le bot scanne les mots-clés validés. Tout titre contenant un mot de la `Blacklist` est ignoré avant enregistrement. Les annonces valides sont insérées/mises à jour dans `observatory.db` (suivi des changements de prix et détection du passage en `sold` ou `deleted`).
2. **Nettoyage par lots réguliers (Filtrage en entonnoir) :** Pour lisser la consommation des quotas gratuits Gemini, un worker tourne périodiquement (ex: toutes les 12h ou 24h) sur les annonces au statut `pending` :
   * *Étape A (Filtre Textuel) :* L'IA analyse le titre + la description complète. Si le produit correspond clairement -> `validated`. Si hors-sujet -> `rejected`.
   * *Étape B (Levée de doute par Vision) :* Uniquement si le texte est trop vague pour conclure, le système télécharge la photo principale et l'envoie à Gemini Vision pour inspection (ex: lecture d'étiquette) -> `validated` ou `rejected`.
3. **Clôture de la Phase 1 :** À la fin de la durée configurée, l'IA analyse l'ensemble des annonces `validated` et `sold` de `observatory.db` :
   * Enregistre le compte-rendu complet dans la page Statistiques de l'interface web.
   * Envoie un **compte-rendu résumé sur Telegram**.
   * Passe la recherche au statut `En attente` (l'utilisateur décide manuellement quand activer la Phase 2 et fixe son prix max).

### Workflow Phase 2 : Le Sniper (Temps Réel)
1. **Détection :** Le scraper surveille les nouvelles annonces à haute fréquence (avec jitter anti-ban).
2. **Pipeline de filtrage séquentiel (pour chaque annonce trouvée) :**
   * *Filtre 1 (Règles dures) :* Rejet immédiat si présence d'un mot de la `Blacklist` ou si `prix > prix_max_utilisateur`.
   * *Filtre 2 (Vérification `sniper_audit.db`) :*
     * Si `vinted_id` existe avec `ai_decision == 'rejected'` -> Ignorer.
     * Si `vinted_id` existe avec `ai_decision == 'retained'` -> Vérifier si le prix actuel est inférieur à `last_seen_price`. Si oui, mettre à jour le prix et déclencher une alerte "Baisse de prix" ; sinon, ignorer.
   * *Filtre 3 (Validation IA à la volée - Tolérance zéro) :* Pour toute nouvelle annonce, appel immédiat à Gemini (Texte, puis Vision si nécessaire). Enregistrement du résultat (`retained` ou `rejected` + motif) dans `sniper_audit.db`.
3. **Notification Telegram "Zéro Tri" :** Si `retained` (ou baisse de prix valide), envoi immédiat d'un message Telegram formaté contenant :
   * La **photo** de l'annonce.
   * Le **prix** et l'**écart en %** par rapport au prix moyen calculé en Phase 1.
   * La **justification technique courte de l'IA** confirmant le respect des critères.
   * Le **lien direct** vers l'annonce Vinted.