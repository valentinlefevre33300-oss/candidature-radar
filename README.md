# Candidature Radar

Trouver **à qui écrire** pour une candidature spontanée, sans passer par un formulaire ATS.

On part d'un métier et d'un territoire ; l'outil remonte les entreprises du secteur,
retrouve leur site, en extrait les adresses de contact publiques, identifie les
personnes (RH, direction) et classe le tout par pertinence. Puis, en **campagne**, il
envoie depuis ton Gmail un mail rédigé pour chaque entreprise — une personne par
entreprise, au rythme des quotas — et suit ouvertures et réponses.

```
 secteur + zone          site web            pages contact /           adresses + rôles
 (annuaire Sirene)  ->   de l'entreprise ->  mentions légales /    ->  classés par
                                             équipe / recrutement      pertinence
```

## Ce que ça fait vraiment

L'annuaire officiel des entreprises publie les **dirigeants nominatifs** (nom, prénom,
qualité) mais pas l'adresse du site. Les sites d'entreprise, eux, publient des adresses
mais rarement celle du dirigeant. L'outil fait la jonction :

1. **Cibler** — l'API [Recherche d'entreprises](https://recherche-entreprises.api.gouv.fr)
   (base Sirene / RNE, gratuite, sans clé) filtre par activité, département et effectif.
2. **Résoudre** — le domaine est déduit de la raison sociale, puis *vérifié* : le nom de
   l'entreprise doit apparaître dans la page. Sinon, repli sur un moteur de recherche.
   En cas de doute, l'outil répond « introuvable » plutôt que de proposer un homonyme.
3. **Explorer** — seules les pages rentables sont visitées (contact, mentions légales,
   équipe, recrutement), pas le site entier.
4. **Extraire** — `mailto:`, texte brut, obfuscation `nom [at] domaine [dot] fr`,
   et protection Cloudflare `data-cfemail`.
5. **Déduire** — les pages Équipe donnent des noms et des fonctions sans adresse ;
   une seule adresse nominative observée suffit à identifier le motif maison
   (`prenom.nom@`, `pnom@`…) et à reconstituer celles des dirigeants et des
   responsables trouvés. Sans aucune adresse observée, `prenom.nom@` est tenté et
   marqué **motif supposé** — classé derrière, rebond possible.
6. **Classer** — d'abord **la personne du métier visé** (le responsable du service
   avant le pair), puis le dirigeant d'une petite structure, puis les RH, puis les
   boîtes génériques. Chaque note est justifiée en clair.
7. **Cibler une campagne** — l'assistant compte les entreprises par secteur pour la zone
   (« Conseil 796, Tech 630… »), lance la recherche, puis retient **une personne par
   entreprise**, la mieux placée, en écartant celles déjà contactées et les prestataires.
8. **Rédiger** — un squelette à variables (`{salutation}`, `{entreprise}`, `{poste}`…)
   dont deux paragraphes sont écrits pour chaque envoi. `{ouverture}` s'adapte à la
   **fonction du destinataire** : à un CPO, « c'est votre équipe que j'aimerais rejoindre
   en tant que product manager » ; à un dirigeant, « rejoindre l'entreprise, vous saurez
   orienter » ; aux RH, la candidature classique. `{accroche}` s'appuie sur une **fiche
   « enjeux »** que Claude rédige une fois par entreprise à partir du texte de son site
   (accueil, à propos) — affichée dans l'assistant pour que tu vérifies qu'on a compris
   la boîte avant d'écrire. Sans clé API, des phrases par règles prennent le relais.
9. **Envoyer et suivre** — depuis ton Gmail (OAuth), CV joint, un mail toutes les
   quelques minutes aux heures de bureau, plafonds par jour et par mois. Un pixel compte
   les ouvertures (une fois déployé), la lecture des en-têtes Gmail détecte les réponses.

## Installation

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
cp .env.example .env    # optionnel
```

## Utilisation

### Interface web

```bash
.venv/Scripts/python.exe -m uvicorn app.web.server:app --port 8010
```

Puis <http://localhost:8010>. Quatre espaces :

- **Campagnes** — le tableau de bord : envois du mois sur le plafond, envoyés, ouverts,
  liste des campagnes (active / en pause / terminée) et journal d'activité. « Créer une
  campagne » ouvre l'assistant en six étapes : poste et zone → secteurs avec compteurs →
  personnes retenues (décochables) → CV → mail (variables surlignées, aperçu rédigé
  pour la première entreprise) → récap et lancement. Chaque campagne a sa page :
  complétion, journal, « à relancer en priorité » (les personnes qui ouvrent sans
  répondre), table des candidatures paginée avec statut, « voir le mail », LinkedIn.
- **Recherche** — le poste visé, une zone (`33` ou `33000`), une taille d'entreprise
  (défaut 10–249, là où ça rend), des secteurs en pilules. Résultats triés par score,
  un clic explique la note. Point de départ possible d'une campagne.
- **Suivi** — le suivi manuel : personnes marquées à la main (à contacter → contacté →
  relancé → a répondu), note, export CSV.
- **Réglages** — connexion Gmail, nom et signature, résumé de profil pour la rédaction,
  CV, plafonds, et l'interrupteur **mode simulation** : toute la chaîne tourne, aucun
  mail ne part. Commence par là.

L'interface reprend le système de design de [Le Brief](https://veille-tech-543e9e.fly.dev/) :
blanc, cartes grises sans bordure, titres en Fraunces, corps en Poppins, un seul accent indigo.

### Ligne de commande

```bash
python cli.py secteurs
python cli.py chercher --poste "développeur python" --secteur tech --dept 33 --effectif 10:250
python cli.py historique
```

## Lire les résultats

| Colonne | Sens |
|---|---|
| **Score** | Pertinence pour une candidature. Au-dessus de 80, l'interlocuteur est le bon. |
| **Catégorie** | `metier` (la personne du métier visé — responsable de service d'abord) > `direction` > `rh` > `nominatif` > `generique` > le reste. |
| `responsable` | Dirige le service qui recrute pour ce poste : l'interlocuteur idéal. |
| `déduite` | **L'adresse n'a jamais été vue en ligne.** Elle est reconstituée depuis le motif maison. À vérifier avant d'écrire. |
| `sans MX` | Le domaine ne déclare aucun serveur de messagerie : l'adresse ne peut pas recevoir. |
| **Raisons** | Pourquoi ce score. Utile pour repérer un classement discutable. |

Deux garde-fous notables, appris en construisant l'outil :

- les RH ne sont **pas** prioritaires : elles reçoivent des centaines de candidatures,
  alors que le responsable du service est le premier à vouloir un nouveau membre dans
  son équipe — l'outil cherche à parler directement aux gens concernés ;
- une adresse **déduite** est pénalisée par rapport à une adresse **observée**, et une
  adresse au **motif supposé** l'est encore plus ;
- une boîte fonctionnelle (`contact@`, `info@`) n'est **pas** promue en RH sous prétexte
  que le mot « recrutement » figure dans le menu du site.

Les adresses d'un domaine tiers (agence web, hébergeur cités dans les mentions légales)
sont conservées mais fortement dépriorisées, et la raison est affichée.

## Ce que l'outil ne fait pas

- **Pas de LinkedIn.** Anti-bot agressif et conditions d'utilisation contraires ; le
  compte du scrapeur y passe avant les données. L'annuaire légal donne les dirigeants
  sans ce risque. Les boutons « in » ouvrent une recherche LinkedIn, pas un profil.
- **Pas d'envoi en rafale.** Une personne par entreprise, jamais deux fois la même,
  un mail toutes les ~4 minutes entre 8 h et 19 h, 25 par jour et 200 par mois par
  défaut. Ce sont des candidatures, pas une newsletter — et Gmail coupe les comptes
  qui se comportent autrement.
- **Pas de contournement.** `robots.txt` est respecté, le débit est limité à une requête
  par domaine et par 1,5 s, et l'agent s'annonce avec une adresse de contact.
- **Pas de comptage d'ouvertures fiable.** Le pixel ne fonctionne que si l'application
  est joignable depuis Internet (`CR_PUBLIC_URL`), et Gmail comme Apple Mail préchargent
  les images : on compte des ouvertures probables, pas certaines. CandiBoost et les
  autres ont exactement la même limite.

Il faut s'attendre à ce que **les grandes entreprises ne donnent rien** : leurs adresses
sont derrière un formulaire. Le rendement est nettement meilleur sur les structures de
10 à 250 personnes, qui sont aussi celles où une candidature spontanée a le plus de
chances d'être lue.

## Cadre d'usage

Les données viennent de sources publiques : annuaire légal des entreprises et sites web
publics. Écrire à une personne sur son adresse professionnelle au sujet de sa fonction
(recruter, diriger une équipe) relève de la prospection B2B, ce que le RGPD admet au
titre de l'intérêt légitime — à condition de rester pertinent et de respecter toute
demande d'arrêt.

Concrètement : un message ciblé et personnalisé à une personne dont c'est le métier,
oui. Un envoi de masse à toutes les adresses d'un export, non — c'est du spam, c'est
inefficace, et ça brûle le domaine de l'expéditeur.

Le **Suivi** garde la trace de chaque personne démarchée. Une adresse déjà suivie reste
visible dans les résultats des recherches suivantes, annotée de son statut : personne ne
reçoit deux fois la même candidature.

## Configuration

Tout est réglable par variables d'environnement — voir [.env.example](.env.example).
Les plus utiles : `CR_CONTACT_EMAIL` (permet à un webmaster de vous joindre plutôt que
de vous bloquer), `CR_DOMAIN_DELAY`, `CR_CONCURRENCY`, `CR_MAX_PAGES`.

Pour les campagnes :

| Variable | Rôle |
|---|---|
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Client OAuth pour « Continuer avec Google ». À créer dans Google Cloud → Identifiants (ID client OAuth, application Web) avec l'URI de redirection `http://localhost:8010/api/gmail/callback` ; activer l'API Gmail ; s'ajouter en utilisateur test de l'écran de consentement. La page Réglages détaille la procédure. |
| `ANTHROPIC_API_KEY` | Rédaction du paragraphe par entreprise. Absente : repli par règles. |
| `CR_CLAUDE_MODEL` | Modèle utilisé (défaut `claude-opus-5`). |
| `CR_PUBLIC_URL` | URL publique de l'application, pour le pixel d'ouverture. Vide en local. |
| `CR_MONTHLY_CAP` / `CR_DAILY_CAP` / `CR_SEND_INTERVAL` | Plafonds et espacement des envois. |
| `CR_DRY_RUN` | `1` force la simulation quoi qu'il arrive (l'interrupteur des réglages fait la même chose sans redémarrer). |

Le jeton Gmail est stocké dans `data/gmail_token.json` (ignoré par git). Portées
demandées : `gmail.send` et `gmail.metadata` — envoyer, et lire les **en-têtes** des fils
pour détecter les réponses. Jamais le contenu des mails.

## Tests

```bash
.venv/Scripts/python.exe tests/test_emails.py     # extraction, hors réseau
.venv/Scripts/python.exe tests/live_crawl.py      # crawl réel
.venv/Scripts/python.exe tests/live_domain.py     # résolution de domaine réelle
```

## Structure

```
app/
├── sources/sirene.py   annuaire officiel des entreprises
├── resolve/domain.py   raison sociale -> site web, avec vérification
├── crawl/fetcher.py    robots.txt, débit limité, garde-fous
├── crawl/spider.py     parcours ciblé des pages utiles
├── extract/emails.py   extraction et désobfuscation
├── extract/people.py   noms, fonctions, motif d'adressage
├── score.py            classement et justifications
├── verify.py           contrôle MX (et sonde SMTP optionnelle)
├── pipeline.py         orchestration d'une recherche
├── compose.py          gabarit à variables + paragraphe par entreprise (Claude / règles)
├── gmail.py            OAuth, envoi, détection des réponses (API REST, sans SDK)
├── campaigns.py        destinataires, programmation, boucle d'envoi, quotas, analyse de marché
├── db.py               SQLite : recherches, contacts, suivi, campagnes, candidatures, journal
└── web/
    ├── server.py       API FastAPI, pixel d'ouverture, fichiers statiques
    └── static/         interface : core.js, views-*.js, app.css (système « Le Brief »)
```
