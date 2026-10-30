#!/usr/bin/env bash
# Chiffre les secrets avec systemd-creds et retire le fichier en clair.
#
#   sudo ./deploy/chiffrer-secrets.sh
#
# Part de /etc/inventaire-infra.env, chiffre chaque variable separement, et
# depose les fichiers dans /etc/inventaire-infra/creds/. Les unites systemd
# les chargent avec LoadCredentialEncrypted : le secret est alors depose dans
# un tmpfs que seul le service peut lire, et n'apparait jamais dans son
# environnement — donc ni dans /proc/<pid>/environ, ni dans un vidage memoire.
#
# Avec un TPM, la cle ne quitte pas la puce et les fichiers chiffres sont
# inutilisables sur une autre machine. Sans TPM, systemd se rabat sur une cle
# hote dans /var/lib/systemd/, lisible par root seul.
#
# Dans les deux cas, ce qui change vraiment : une sauvegarde, un snapshot ou
# un disque recupere ne contiennent plus rien d'exploitable.

set -euo pipefail

SOURCE_ENV="${SOURCE_ENV:-/etc/inventaire-infra.env}"
DEST="${DEST:-/etc/inventaire-infra/creds}"
GARDER_CLAIR=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --garder-clair) GARDER_CLAIR=1 ;;
    --source) SOURCE_ENV="$2"; shift ;;
    --dest)   DEST="$2"; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "option inconnue : $1" >&2; exit 2 ;;
  esac
  shift
done

[[ $EUID -eq 0 ]] || { echo "lancer avec sudo" >&2; exit 1; }
command -v systemd-creds >/dev/null \
  || { echo "systemd-creds absent (systemd 250+ requis)" >&2; exit 1; }
[[ -r "$SOURCE_ENV" ]] || { echo "introuvable : $SOURCE_ENV" >&2; exit 1; }

# Un TPM rend les fichiers inutilisables ailleurs ; sans lui, on chiffre quand
# meme, avec la cle hote.
if systemd-creds has-tpm2 >/dev/null 2>&1; then
  echo "TPM2 present : les secrets seront lies a cette machine"
else
  echo "pas de TPM2 : chiffrement par cle hote (/var/lib/systemd/)"
fi

mkdir -p "$DEST"
chmod 700 "$DEST"

compte=0
while IFS= read -r ligne; do
  [[ "$ligne" =~ ^[[:space:]]*# ]] && continue
  [[ "$ligne" =~ ^[[:space:]]*$ ]] && continue
  [[ "$ligne" == *=* ]] || continue

  nom="${ligne%%=*}"
  valeur="${ligne#*=}"
  nom="$(echo "$nom" | tr -d '[:space:]')"
  [[ -n "$valeur" ]] || { echo "  $nom : vide, ignore"; continue; }

  cible="$DEST/$(echo "$nom" | tr '[:upper:]' '[:lower:]').cred"
  # printf sans saut de ligne : un \n final se retrouverait dans le secret
  printf '%s' "$valeur" \
    | systemd-creds encrypt --name="$(echo "$nom" | tr '[:upper:]' '[:lower:]')" \
        - "$cible"
  chmod 600 "$cible"
  echo "  $nom -> $cible"
  compte=$((compte + 1))
done < "$SOURCE_ENV"

echo
echo "$compte secret(s) chiffre(s) dans $DEST"

if [[ $GARDER_CLAIR -eq 1 ]]; then
  echo "fichier en clair conserve : $SOURCE_ENV (--garder-clair)"
else
  # Sauvegarde hors d'atteinte avant suppression : si une unite n'etait pas
  # encore convertie, le service ne demarrerait plus.
  copie="${SOURCE_ENV}.avant-chiffrement"
  mv "$SOURCE_ENV" "$copie"
  chmod 600 "$copie"
  echo "fichier en clair deplace vers $copie"
  echo "Verifie que les services demarrent, puis supprime-le :"
  echo "    shred -u $copie"
fi

echo
echo "Les unites doivent charger ces secrets. Dans chaque unite :"
echo "    LoadCredentialEncrypted=ovh_application_key:$DEST/ovh_application_key.cred"
echo "    ... une ligne par secret, et retirer EnvironmentFile="
echo "Puis : systemctl daemon-reload && systemctl restart inventaire-collecte.service"
