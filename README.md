# vinted-nba

Outils pour surveiller Vinted à la recherche de cartes NBA.

## Surveillance Kyrie Irving rookie Prizm

`watch_irving_prizm.py` interroge le catalogue Vinted (par défaut [vinted.fr](https://www.vinted.fr)) toutes les **10 minutes**, cherche une carte **Kyrie Irving rookie Panini Prizm**, et n'affiche que les **nouvelles** annonces.

Le script :

- ouvre une session anonyme en visitant la page d'accueil (comme un navigateur) ;
- interroge l'API catalogue actuelle, `https://api.vinted.fr/svc-catalogue/items` ;
- lance plusieurs recherches (`kyrie irving prizm rookie`, `irving prizm 2012`, etc.) ;
- ne retient que les titres qui mentionnent Irving/Kyrie **et** Prizm, plus un indice rookie (`rookie`, `RC`, `2012`, `2012-13`) ;
- ignore notamment les cartes de Bucky Irving (NFL) ;
- mémorise les ids déjà vus dans `seen_items.json` ;
- peut envoyer une notification (ntfy ou Telegram) si tu configures des variables d'environnement — rien n'est envoyé sinon.

La date affichée est l'heure de **première détection** (fuseau Europe/Paris). L'API catalogue ne renvoie pas la date de mise en ligne exacte.

### Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Lancer une fois (test)

```bash
python watch_irving_prizm.py --once
```

Le premier passage affiche les annonces déjà en ligne qui passent le filtre, puis les enregistre. Les passages suivants ne signalent que les **nouvelles** mises en ligne.

Options utiles :

```bash
# Prix maximum en euros, domaine et intervalle
python watch_irving_prizm.py --once --max-price 250 --domain vinted.fr

# Requêtes et mots-clés personnalisés
python watch_irving_prizm.py --once \
  --query "kyrie irving prizm" --query "irving prizm 2012" \
  --name-keywords kyrie,irving --set-keywords prizm,prism

# Afficher aussi les annonces déjà vues
python watch_irving_prizm.py --once --show-all
```

### Tourner en continu (toutes les 10 minutes)

```bash
python watch_irving_prizm.py
# ou :
python watch_irving_prizm.py --interval 10
```

En arrière-plan avec les logs dans un fichier :

```bash
nohup python watch_irving_prizm.py --interval 10 >> watch.log 2>&1 &
```

Avec `tmux` ou `screen` :

```bash
tmux new -s vinted-irving
python watch_irving_prizm.py
# détacher : Ctrl+B puis D
```

### Cron (un passage toutes les 10 minutes)

Le script gère déjà la boucle interne ; un cron n'est utile que si tu préfères un processus court (`--once`) :

```cron
*/10 * * * * cd /chemin/vers/vinted-nba && .venv/bin/python watch_irving_prizm.py --once >> watch.log 2>&1
```

Dans ce mode, laisse `seen_items.json` au même endroit (ou passe `--seen-file`) pour ne pas re-signaler les anciennes annonces à chaque exécution.

### Notifications (optionnelles)

Aucune clé n'est stockée dans le code. Exporte simplement les variables, ou copie `.env.example` vers `.env` et source-le.

**ntfy.sh**

```bash
export NTFY_TOPIC="mon-topic-secret"
# ou une URL complète (serveur auto-hébergé) :
# export NTFY_URL="https://ntfy.exemple.org/mon-topic"
python watch_irving_prizm.py
```

**Telegram**

```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
python watch_irving_prizm.py
```

### Limites

- L'endpoint utilisé est l'API interne du site web Vinted, non documentée : un changement côté Vinted peut casser le script.
- En cas d'erreur réseau, de HTTP 429 ou de session expirée, le script attend (backoff) puis recommence. En mode boucle, il ne s'arrête pas pour autant.
- Un rythme trop agressif peut entraîner un blocage (Cloudflare / DataDome). L'intervalle de 10 minutes et quelques requêtes par passage restent raisonnables ; n'abrège pas trop l'intervalle.
- La recherche Vinted est large : le filtre sur le titre est indispensable. Une annonce mal titrée (sans « Irving », « Prizm » ou indice rookie) peut passer inaperçue. Tu peux assouplir avec `--no-require-rookie`.
