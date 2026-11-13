#!/usr/bin/env bash
# Lance inventory_cli.py sous le compte de collecte, avec ses secrets.
#
#   sudo /opt/inventaire-infra/deploy/inventory.sh --list
#   sudo /opt/inventaire-infra/deploy/inventory.sh --compte-local admin
#   sudo /opt/inventaire-infra/deploy/inventory.sh --tester-ovh
#
# Les secrets sont chiffres par systemd-creds : un shell ordinaire ne peut pas
# les lire, et c'est voulu. Ce script demande a systemd une unite ephemere,
# sous le compte "inventaire", qui recoit les memes secrets que la collecte
# (tous les .cred de /etc/inventaire-infra/creds). Ils ne passent ni par
# l'environnement, ni par la ligne de commande.

set -euo pipefail

PREFIXE="${PREFIXE:-/opt/inventaire-infra}"
CREDS="${CREDS:-/etc/inventaire-infra/creds}"
UTILISATEUR="${UTILISATEUR:-inventaire}"

[[ $EUID -eq 0 ]] || { echo "lancer avec sudo" >&2; exit 1; }
[[ $# -gt 0 ]] || { sed -n '2,6p' "$0"; exit 2; }

proprietes=(-p "User=$UTILISATEUR" -p "WorkingDirectory=$PREFIXE")
shopt -s nullglob
for f in "$CREDS"/*.cred; do
  nom="$(basename "$f" .cred)"
  proprietes+=(-p "LoadCredentialEncrypted=$nom:$f")
done

# Un terminal : --pty, pour les saisies masquees et pour --compte-local qui
# refuse d'afficher un mot de passe ailleurs que dans un terminal. Sinon
# (script, tube) : --pipe, qui transmet entree et sorties telles quelles.
if [[ -t 0 && -t 1 ]]; then
  mode=--pty
else
  mode=--pipe
fi

exec systemd-run --quiet --wait --collect "$mode" "${proprietes[@]}" \
     /usr/bin/python3 "$PREFIXE/inventory_cli.py" "$@"
