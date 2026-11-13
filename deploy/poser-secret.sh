#!/usr/bin/env bash
# Pose un secret, chiffre par systemd-creds, sans jamais l'ecrire en clair.
#
#   sudo bash deploy/poser-secret.sh ovh       les trois clefs OVH (testees avant)
#   sudo bash deploy/poser-secret.sh pve       le jeton PVE par defaut (facultatif :
#                                           les jetons se deposent aussi depuis
#                                           la page, bouton Recolte)
#   sudo bash deploy/poser-secret.sh unites    regenere seulement le branchement
#                                           des secrets dans les services
#
# La saisie est masquee (rien ne s'affiche quand tu colles, c'est normal).
# Le secret va directement de la saisie a systemd-creds : ni fichier, ni
# historique, ni ligne de commande visible dans un "ps".
#
# Chaque service ne recoit que ses secrets, par un fichier de surcharge
# systemd (/etc/systemd/system/<service>.d/10-secrets.conf) regenere ici :
#   - la collecte : tout ce qui est dans /etc/inventaire-infra/creds
#   - le service web : son seul mot de passe de base (creds-web)

set -euo pipefail

CREDS="${CREDS:-/etc/inventaire-infra/creds}"
CREDS_WEB="${CREDS_WEB:-/etc/inventaire-infra/creds-web}"
PREFIXE="${PREFIXE:-/opt/inventaire-infra}"
ICI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

vert()  { printf '\033[32m%s\033[0m\n' "$*"; }
jaune() { printf '\033[33m%s\033[0m\n' "$*"; }
rouge() { printf '\033[31m%s\033[0m\n' "$*" >&2; }

[[ $EUID -eq 0 ]] || { rouge "lancer avec sudo"; exit 1; }
command -v systemd-creds >/dev/null \
  || { rouge "systemd-creds absent (systemd 250+ requis)"; exit 1; }

# --------------------------------------------------------------------------
# Branchement des secrets dans les unites
# --------------------------------------------------------------------------

ecrire_surcharge() {
  local service="$1" dossier="$2" rep fichier contenu f
  rep="/etc/systemd/system/$service.d"
  fichier="$rep/10-secrets.conf"
  contenu="# Genere par deploy/poser-secret.sh : ne pas modifier a la main.
# Les secrets arrivent chiffres, dans un dossier que seul le service lit.
[Service]
# aucun fichier d'environnement en clair
EnvironmentFile="
  shopt -s nullglob
  for f in "$dossier"/*.cred; do
    contenu+=$'\n'"LoadCredentialEncrypted=$(basename "$f" .cred):$f"
  done
  shopt -u nullglob
  mkdir -p "$rep"
  printf '%s\n' "$contenu" > "$fichier"
  chmod 644 "$fichier"
}

unites() {
  ecrire_surcharge inventaire-collecte.service "$CREDS"
  ecrire_surcharge inventaire-web.service "$CREDS_WEB"
  systemctl daemon-reload
}

# --------------------------------------------------------------------------
# Saisie et chiffrement
# --------------------------------------------------------------------------

# Lit une valeur sans echo et la chiffre dans $2 sous le nom $1.
# printf et read sont internes a bash : la valeur ne passe jamais en argument
# d'un programme, donc jamais dans un "ps".
saisir() {
  local nom="$1" cible="$2" invite="$3" valeur=""
  while [[ -z "$valeur" ]]; do
    read -r -s -p "  $invite (colle puis Entree, rien ne s'affiche) : " valeur
    echo
    # une espace recopiee avec la clef fausse la signature sans le dire
    valeur="${valeur#"${valeur%%[![:space:]]*}"}"
    valeur="${valeur%"${valeur##*[![:space:]]}"}"
    [[ -n "$valeur" ]] || jaune "  vide, recommence"
  done
  printf '%s' "$valeur" | systemd-creds encrypt --name="$nom" - "$cible" \
    2> >(grep -v 'not located on encrypted media' >&2)
  chmod 600 "$cible"
  valeur=""
}

poser_ovh() {
  [[ -t 0 ]] || { rouge "saisie interactive : lance-le depuis un terminal"; exit 2; }
  mkdir -p "$CREDS" && chmod 700 "$CREDS"
  local tmp
  tmp="$(mktemp -d "$CREDS/.ovh.XXXXXX")"
  trap 'rm -rf "$tmp"' EXIT

  echo "Les trois clefs OVH (https://api.ovh.com/createToken/) :"
  saisir ovh_application_key    "$tmp/ovh_application_key.cred"    "Application Key   "
  saisir ovh_application_secret "$tmp/ovh_application_secret.cred" "Application Secret"
  saisir ovh_consumer_key       "$tmp/ovh_consumer_key.cred"       "Consumer Key      "

  echo
  echo "Test des clefs aupres d'OVH (rien n'est encore en place) :"
  if CREDS="$tmp" bash "$ICI/inventory.sh" --tester-ovh; then
    mv -f "$tmp"/*.cred "$CREDS"/
    unites
    vert "Clefs OVH en place, chiffrees dans $CREDS."
  else
    rouge "Clefs refusees : rien n'a ete change. Verifie-les et relance."
    exit 1
  fi
}

poser_pve() {
  [[ -t 0 ]] || { rouge "saisie interactive : lance-le depuis un terminal"; exit 2; }
  mkdir -p "$CREDS" && chmod 700 "$CREDS"
  echo "Jeton PVE par defaut, au format  inventory@pve!collecte=xxxxxxxx-xxxx-..."
  saisir pve_token "$CREDS/pve_token.cred" "Jeton PVE"
  unites
  vert "Jeton PVE par defaut en place. La prochaine collecte le testera."
}

case "${1:-}" in
  ovh)    poser_ovh ;;
  pve)    poser_pve ;;
  unites) unites; vert "branchement des secrets regenere" ;;
  *)      sed -n '2,10p' "$0"; exit 2 ;;
esac
