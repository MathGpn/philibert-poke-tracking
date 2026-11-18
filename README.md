# philibert - pokemon - 30 ans

Surveillance simple de Philibert pour les articles **Pokémon 30 ans**.

## Surveillance Philibert — Pokémon 30 ans

`watch_philibert.py` interroge (toutes les **5 minutes** via GitHub Actions, ou toutes les **2 minutes** par défaut en mode boucle sur une machine), la recherche équivalente à la page :

[Pokémon 30 ans sur Philibert](https://www.philibertnet.com/fr/212-pokemon#e210/mobile/m=and&q=pok%C3%A9mon+30+ans)

Dès qu'un **nouvel article devient disponible à l'achat**, ou qu'un article déjà connu (indisponible / à venir) passe à l'achat, un **e-mail Gmail** est envoyé, soit directement en SMTP, soit via un workflow GitHub Actions (si la machine qui surveille ne peut pas joindre `smtp.gmail.com`).

Le script :

- passe par l'API de recherche publique Doofinder utilisée par le calque de la page (le fragment `#e210/mobile/m=and&q=…` n'est pas envoyé au serveur : c'est un filtre côté navigateur) ;
- mémorise les articles déjà vus dans `seen_products.json` (commité dans le dépôt pour le mode GitHub Actions) ;
- n'envoie **aucune alerte** lors de la première exécution (baseline), sauf si tu passes `--alert-on-first-run` ;
- traite une précommande **ouverte** (ajout au panier possible) comme achetable, en la labellisant clairement ; une fiche « à venir » encore indisponible est seulement mémorisée.

L'heure affichée est celle de la détection, fuseau **Europe/Paris**.

## Mode actuel : GitHub Actions toutes les 5 minutes

La surveillance tourne désormais **sur GitHub Actions**, sans aucune machine à laisser allumée. Le workflow [`.github/workflows/philibert-watch.yml`](.github/workflows/philibert-watch.yml) :

- se lance toutes les **5 minutes** (`cron: "*/5 * * * *"`) et peut aussi être lancé à la main depuis l'onglet Actions (`workflow_dispatch`) ;
- exécute `python3 watch_philibert.py --once` sur un runner `ubuntu-slim` (1 vCPU, le moins cher) ;
- envoie l'alerte **Telegram** puis l'**e-mail Gmail directement en SMTP** depuis le runner (`NOTIFY=smtp`) ;
- garde la mémoire des articles déjà vus dans `seen_products.json`, **commité dans le dépôt** : le fichier n'est recommité que si la liste des articles ou leur statut « achetable » change (les simples horodatages ne créent pas de commit) ;
- utilise un groupe `concurrency` pour que deux passages ne se chevauchent jamais.

Secrets du dépôt utilisés (Settings → Secrets and variables → Actions) :

- `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `MAIL_TO`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

Lancer un passage à la main :

```bash
gh workflow run philibert-watch.yml -R MathGpn/philibert-poke-tracking
gh run list --workflow philibert-watch.yml -R MathGpn/philibert-poke-tracking
```

À savoir :

- GitHub ne garantit pas l'horaire des tâches planifiées : un passage peut partir avec quelques minutes de retard, voire être sauté quand GitHub est très chargé.
- Dans un dépôt **public**, GitHub désactive les tâches planifiées après 60 jours sans activité sur le dépôt. Ce dépôt étant **privé**, cette règle ne s'applique pas.
- Dans un dépôt privé, chaque passage consomme au moins 1 minute facturée sur le quota gratuit Actions du compte (environ 8 600 passages par mois à 5 minutes, soit environ 17 $ au tarif `ubuntu-slim`, à comparer aux 12 $ inclus dans GitHub Free). Dans un dépôt public, les runners standards sont gratuits et illimités.
- Si un passage échoue (Philibert ou Gmail injoignable), GitHub l'indique dans l'onglet Actions ; l'état n'est pas enregistré et l'alerte sera retentée au passage suivant.

Le script peut toujours tourner en continu sur une machine (sections ci-dessous), mais il ne faut pas faire tourner les deux en même temps, sous peine d'alertes en double.

### Installation

Aucune dépendance tierce : Python 3.10+ suffit (bibliothèque standard uniquement).

```bash
# optionnel, pour isoler l'environnement
python3 -m venv .venv
source .venv/bin/activate
```

### Notifications : SMTP local ou GitHub Actions

Le script envoie l'alerte selon `NOTIFY` :

- `NOTIFY=smtp` : envoi direct via `smtp.gmail.com:587` (STARTTLS).
- `NOTIFY=github` : déclenche le workflow [`.github/workflows/philibert-alert.yml`](.github/workflows/philibert-alert.yml) (`repository_dispatch`, type `philibert-alert`), qui envoie l'e-mail depuis les runners GitHub. Utile quand la machine de surveillance n'atteint pas SMTP.
- Si `NOTIFY` n'est pas défini : mode `github` dès qu'un jeton `GH_TOKEN` ou `GITHUB_TOKEN` est présent, sinon mode `smtp` si les identifiants Gmail sont présents.

`--test-email` exerce le notificateur actif (sans interroger Philibert).

#### Secrets Gmail (mot de passe d'application)

Google bloque les mots de passe de compte habituels pour les applications. Il faut un **mot de passe d'application**.

1. Active la [validation en deux étapes](https://myaccount.google.com/signinoptions/two-step-verification) sur le compte Gmail qui enverra les alertes.
2. Ouvre [Mots de passe des applications](https://myaccount.google.com/apppasswords), crée un mot de passe (par exemple « Philibert »).

En mode SMTP local, copie `.env.example` vers `.env` et remplis les valeurs (le script charge `.env` automatiquement) :

```bash
cp .env.example .env
```

```
NOTIFY=smtp
GMAIL_ADDRESS=ton.adresse@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
# MAIL_TO=autre.adresse@gmail.com   # optionnel, par défaut = GMAIL_ADDRESS
```

Ne commite jamais le fichier `.env`. Le mot de passe d'application peut être collé avec ou sans espaces.

```bash
NOTIFY=smtp python3 watch_philibert.py --test-email
```

#### Mode GitHub Actions

Le workflow ne tourne que depuis la branche par défaut (`main`). Il lit les secrets du dépôt :

- `GMAIL_ADDRESS`
- `GMAIL_APP_PASSWORD`
- `MAIL_TO`

```bash
gh secret set GMAIL_ADDRESS -R MathGpn/philibert-poke-tracking
gh secret set GMAIL_APP_PASSWORD -R MathGpn/philibert-poke-tracking
gh secret set MAIL_TO -R MathGpn/philibert-poke-tracking
```

Sur la machine qui surveille, fournis un jeton avec le droit `repo` (par exemple celui de `gh`) :

```bash
export NOTIFY=github
export GH_TOKEN="$(gh auth token)"
# optionnel : export GITHUB_REPO=MathGpn/philibert-poke-tracking
python3 watch_philibert.py --test-email
```

Tu peux aussi lancer un essai manuel depuis l'onglet Actions (`workflow_dispatch`).

### Lancer une fois (test, sans e-mail de baseline)

```bash
python3 watch_philibert.py --once --dry-run
```

Le premier passage liste les articles trouvés, enregistre la baseline dans `seen_products.json`, et n'envoie pas d'e-mail. Les passages suivants n'envoient un mail que pour les **nouveaux** articles achetables, ou pour un article connu qui redevient achetable.

Options utiles :

```bash
# Un passage, et alerte aussi pour les articles déjà achetables
python3 watch_philibert.py --once --alert-on-first-run

# Intervalle personnalisé (secondes) et autre requête
python3 watch_philibert.py --interval 120 --query "pokémon 30 ans"

# Fichier d'état ailleurs
python3 watch_philibert.py --once --state-file /tmp/seen_products.json
```

### Tourner en continu

```bash
python3 watch_philibert.py
# ou :
python3 watch_philibert.py --interval 120
```

En arrière-plan, avec les logs dans un fichier (exemple en mode GitHub) :

```bash
export NOTIFY=github
export GH_TOKEN="$(gh auth token)"
nohup python3 -u watch_philibert.py --interval 120 >> watch.log 2>&1 &
```

Avec `tmux` ou `screen` :

```bash
tmux new -s philibert
python3 watch_philibert.py
# détacher : Ctrl+B puis D
```

### Cron (un passage à intervalle fixe)

Le script gère déjà la boucle interne, avec jitter et backoff. Un cron n'est utile que si tu préfères un processus court (`--once`) :

```cron
* * * * * cd /chemin/vers/philibert-poke-tracking && python3 watch_philibert.py --once >> watch.log 2>&1
```

Dans ce mode, laisse `seen_products.json` au même endroit (ou passe `--state-file`) pour ne pas re-signaler les anciens articles. Un cron toutes les 2 minutes correspond au défaut de 120 s : reste raisonnable, n'ajoute pas plusieurs crons en parallèle.

### Limites

- Les pages HTML de philibertnet.com sont protégées par Akamai Bot Manager (souvent un 403 depuis un serveur). Le script n'en a pas besoin : il parle à l'API de recherche Doofinder (`eu1-search.doofinder.com`) avec les en-têtes `Origin` / `Referer` du site. Si Philibert change de moteur ou d'identifiant (`hashid`), la recherche devra être mise à jour.
- L'index Doofinder peut avoir un léger décalage par rapport à la fiche produit.
- En cas d'erreur réseau ou HTTP, le script double l'attente (backoff, plafonné) puis réessaie. En mode boucle, il ne s'arrête pas pour autant.
- L'intervalle par défaut de 120 secondes, avec un jitter de ±20 %, vise un bon compromis entre réactivité et charge. Le minimum accepté est de 30 secondes.
- Le mode `github` exige que le workflow soit présent sur `main` (`repository_dispatch` ne s'exécute que sur la branche par défaut) et que les secrets Gmail du dépôt soient renseignés.

## Alerte Telegram (instantanée)

En plus de l'e-mail, le script peut envoyer un message Telegram, bien plus rapide. Il suffit de définir :

```bash
export TELEGRAM_BOT_TOKEN=123456:ABC...   # token donné par @BotFather
export TELEGRAM_CHAT_ID=123456789        # ton id de conversation avec le bot
```

Le message Telegram part en premier ; un échec Telegram n'empêche pas l'envoi de l'e-mail.
