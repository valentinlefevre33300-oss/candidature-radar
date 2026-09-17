# Candidature Radar

Trouver **à qui écrire** pour une candidature spontanée, sans passer par un formulaire ATS.

On part d'un métier et d'un territoire ; l'outil remonte les entreprises du secteur,
retrouve leur site, en extrait les adresses de contact publiques, identifie les
personnes (RH, direction) et classe le tout par pertinence.

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
5. **Déduire** — une seule adresse nominative observée suffit à identifier le motif
   maison (`prenom.nom@`, `pnom@`…) et à reconstituer celle des dirigeants connus.
6. **Classer** — un contact RH identifié passe devant un dirigeant, qui passe devant
   une boîte générique. Chaque note est justifiée en clair.

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

Puis <http://localhost:8010>. Trois vues :

- **Recherche** — le poste visé, une zone (`33` ou `33000`), une taille d'entreprise
  (défaut 10–249, là où ça rend), des secteurs en pilules. La progression s'affiche
  entreprise par entreprise ; l'historique des recherches est en dessous.
- **Résultats** — lignes triées par score, filtres par catégorie, monogramme par
  entreprise. Un clic sur une ligne explique le score. **Suivre** ajoute la personne
  au suivi, **Écarter** la range hors de vue. Export CSV.
- **Suivi** — chaque personne avec son statut (à contacter → contacté → relancé →
  a répondu), une note, et la recherche d'où elle vient. Export CSV.

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
| **Catégorie** | `rh` > `direction` > `nominatif` > `generique` > le reste. |
| `déduite` | **L'adresse n'a jamais été vue en ligne.** Elle est reconstituée depuis le motif maison. À vérifier avant d'écrire. |
| `sans MX` | Le domaine ne déclare aucun serveur de messagerie : l'adresse ne peut pas recevoir. |
| **Raisons** | Pourquoi ce score. Utile pour repérer un classement discutable. |

Deux garde-fous notables, appris en construisant l'outil :

- une adresse **déduite** ne passe jamais devant une adresse **observée** — un contact RH
  confirmé vaut mieux qu'un PDG supposé ;
- une boîte fonctionnelle (`contact@`, `info@`) n'est **pas** promue en RH sous prétexte
  que le mot « recrutement » figure dans le menu du site.

Les adresses d'un domaine tiers (agence web, hébergeur cités dans les mentions légales)
sont conservées mais fortement dépriorisées, et la raison est affichée.

## Ce que l'outil ne fait pas

- **Pas de LinkedIn.** Anti-bot agressif et conditions d'utilisation contraires ; le
  compte du scrapeur y passe avant les données. L'annuaire légal donne les dirigeants
  sans ce risque.
- **Pas d'envoi de mail.** L'outil trouve des adresses, il n'écrit à personne.
- **Pas de contournement.** `robots.txt` est respecté, le débit est limité à une requête
  par domaine et par 1,5 s, et l'agent s'annonce avec une adresse de contact.

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
├── pipeline.py         orchestration
├── db.py               SQLite : historique, déduplication, export
└── web/                interface locale
```
