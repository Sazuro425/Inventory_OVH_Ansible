#!/usr/bin/env bash
# Collecte planifiee de l'inventaire (VM Linux).
#
# Lance par le timer systemd, ou a la main pour tester :
#     ./deploy/collecte.sh
#
# Les secrets viennent du fichier d'environnement, jamais de la ligne de
# commande : un "ps aux" les exposerait a tous les utilisateurs de la machine.

set -euo pipefail

RACINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${INVENTAIRE_ENV:-/etc/inventaire-infra.env}"
VERROU="${INVENTAIRE_LOCK:-/tmp/inventaire-infra.lock}"
PYTHON="${INVENTAIRE_PYTHON:-python3}"

# Deux sources possibles. systemd-creds depose les secrets dans un repertoire
# qu'il est seul a pouvoir lire, et les scripts Python le consultent avant
# l'environnement ; sinon on charge le fichier d'environnement.
SECRETS_PAR_SYSTEMD=0
if [[ -n "${CREDENTIALS_DIRECTORY:-}" && -r "${CREDENTIALS_DIRECTORY}/pve_token" ]]; then
  SECRETS_PAR_SYSTEMD=1
  echo "secrets fournis par systemd-creds"
elif [[ -r "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
else
  echo "aucun secret disponible : ni CREDENTIALS_DIRECTORY, ni $ENV_FILE" >&2
  echo "renseigne deploy/inventaire.env.example (chmod 640)," >&2
  echo "ou chiffre-les avec deploy/chiffrer-secrets.sh" >&2
  exit 78  # EX_CONFIG
fi

if [[ $SECRETS_PAR_SYSTEMD -eq 0 ]]; then
  for var in OVH_APPLICATION_KEY OVH_APPLICATION_SECRET OVH_CONSUMER_KEY \
             PVE_TOKEN INVENTORY_DB_PASSWORD; do
    if [[ -z "${!var:-}" ]]; then
      echo "variable manquante dans $ENV_FILE : $var" >&2
      exit 78
    fi
  done
fi

cd "$RACINE"

# Un seul passage a la fois : une collecte qui traine ne doit pas en croiser
# une autre, sinon deux snapshots concurrents partent en base.
exec 9>"$VERROU"
if ! flock -n 9; then
  echo "une collecte est deja en cours ($VERROU), passage ignore" >&2
  exit 0
fi

debut=$(date +%s)
echo "--- collecte $(date --iso-8601=seconds) ---"

if ! "$PYTHON" infra_inventory.py --refresh --store-db > /dev/null; then
  code=$?
  echo "echec de la collecte (code $code)" >&2
  exit "$code"
fi
echo "collecte terminee en $(( $(date +%s) - debut ))s"

# Publication de la page, si un emplacement est configure. L'ecriture est
# atomique cote Python : le serveur web ne sert jamais de page tronquee.
if [[ -n "${INVENTAIRE_WEB:-}" ]]; then
  if "$PYTHON" topologie.py --out "$INVENTAIRE_WEB" > /dev/null; then
    echo "page publiee : $INVENTAIRE_WEB"
  else
    # une page non regeneree n'invalide pas la collecte, qui est en base
    echo "page non regeneree (la collecte, elle, est enregistree)" >&2
  fi
fi
