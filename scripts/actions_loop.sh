#!/usr/bin/env bash
# Boucle de surveillance exécutée dans GitHub Actions.
# Toutes les INTERVAL secondes : vérifie Philibert (watch_philibert.py --once),
# puis commite seen_products.json si la liste des articles ou leur statut change.
# Une itération en échec (Philibert, Gmail…) est journalisée sans arrêter la boucle ;
# l'état n'est alors pas enregistré, donc l'alerte sera retentée au tour suivant.
# La boucle s'arrête d'elle-même après LOOP_MINUTES, pour laisser le temps
# à l'étape suivante du workflow de relancer un nouveau run.
set -uo pipefail

INTERVAL="${INTERVAL:-300}"
LOOP_MINUTES="${LOOP_MINUTES:-335}"
CHECK_TIMEOUT="${CHECK_TIMEOUT:-240}"

start=$(date +%s)
deadline=$((start + LOOP_MINUTES * 60))

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

state_changed() {
  python3 - << 'PY'
import json, subprocess, sys
def key(text):
    data = json.loads(text) if text.strip() else {}
    return sorted(
        (pid, bool(p.get("purchasable")))
        for pid, p in (data.get("products") or {}).items()
    )
try:
    old = subprocess.run(["git", "show", "HEAD:seen_products.json"],
                         capture_output=True, text=True, check=True).stdout
except subprocess.CalledProcessError:
    old = ""
new = open("seen_products.json", encoding="utf-8").read()
sys.exit(0 if key(old) != key(new) else 1)
PY
}

save_state() {
  if ! state_changed; then
    echo "État inchangé"
    return 0
  fi
  echo "État modifié : enregistrement"
  cp seen_products.json /tmp/seen_products.json
  for attempt in 1 2 3; do
    git fetch -q origin main && git reset -q --hard origin/main || { sleep $((attempt * 5)); continue; }
    cp /tmp/seen_products.json seen_products.json
    git add seen_products.json
    git diff --cached --quiet && return 0
    git commit -q -m "Mise à jour de l'état Philibert [skip ci]"
    if git push -q origin HEAD:main; then
      return 0
    fi
    sleep $((attempt * 5))
  done
  echo "::warning::Impossible de pousser l'état (nouvel essai au prochain tour)"
  return 1
}

i=0
while :; do
  i=$((i + 1))
  it_start=$(date +%s)
  echo "::group::Itération $i — $(TZ=Europe/Paris date '+%H:%M:%S') (Paris)"
  if timeout "$CHECK_TIMEOUT" python3 watch_philibert.py --once; then
    save_state || true
    echo "Itération $i OK"
  else
    echo "::warning::Itération $i en échec (code $?), on continue"
    # On n'enregistre pas l'état : l'alerte sera retentée au prochain tour.
  fi
  echo "::endgroup::"

  now=$(date +%s)
  sleep_for=$((INTERVAL - (now - it_start)))
  [ "$sleep_for" -lt 0 ] && sleep_for=0
  if [ $((now + sleep_for + CHECK_TIMEOUT)) -gt "$deadline" ]; then
    echo "Fin de la boucle après $i itérations ; le workflow va se relancer."
    break
  fi
  sleep "$sleep_for"
done
