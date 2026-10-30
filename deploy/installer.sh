#!/usr/bin/env bash
# Deploiement de l'inventaire sur une machine Debian/Ubuntu.
#
#   sudo ./deploy/installer.sh --simulation     montre ce qui serait fait
#   sudo ./deploy/installer.sh                  installe
#   sudo ./deploy/installer.sh --sans-apache    sans le vhost
#
# Le script est idempotent : on peut le rejouer pour mettre a jour le code.
# Il ne remplace jamais un fichier de configuration deja renseigne — ni
# /etc/inventaire-infra.env, ni infra.yml, ni les certificats epingles — et il
# sauvegarde ce qu'il remplace.
#
# Il ne touche pas a la base : le schema se cree au premier passage de la
# collecte, et le compte applicatif se cree avec setup_inventory_db.py, qui
# demande les acces administrateur.

set -euo pipefail

PREFIXE="${PREFIXE:-/opt/inventaire-infra}"
UTILISATEUR="${UTILISATEUR:-inventaire}"
FICHIER_ENV="/etc/inventaire-infra.env"
FICHIER_ENV_WEB="/etc/inventaire-infra-web.env"
RACINE_WEB="${RACINE_WEB:-/var/www/inventaire}"
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SIMULATION=0
AVEC_APACHE=1
AVEC_WEB=1
AVEC_TIMER=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --simulation|-n) SIMULATION=1 ;;
    --sans-apache)   AVEC_APACHE=0 ;;
    --sans-web)      AVEC_WEB=0 ;;
    --sans-timer)    AVEC_TIMER=0 ;;
    --prefixe)       PREFIXE="$2"; shift ;;
    -h|--help)       sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "option inconnue : $1" >&2; exit 2 ;;
  esac
  shift
done

# --------------------------------------------------------------------------
vert()  { printf '\033[32m%s\033[0m\n' "$*"; }
jaune() { printf '\033[33m%s\033[0m\n' "$*"; }
rouge() { printf '\033[31m%s\033[0m\n' "$*" >&2; }
etape() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

faire() {
  if [[ $SIMULATION -eq 1 ]]; then
    printf '   [simulation] %s\n' "$*"
  else
    "$@"
  fi
}

sauvegarder() {
  local cible="$1"
  [[ -e "$cible" ]] || return 0
  local copie="${cible}.$(date +%Y%m%d%H%M%S).bak"
  faire cp -a "$cible" "$copie"
  jaune "   ancienne version sauvegardee : $copie"
}

# --------------------------------------------------------------------------
etape "Verifications"

if [[ $EUID -ne 0 && $SIMULATION -eq 0 ]]; then
  rouge "ce script doit etre lance avec sudo (ou --simulation)"
  exit 1
fi

if [[ ! -r /etc/debian_version ]]; then
  jaune "   distribution non Debian/Ubuntu : les paquets et chemins peuvent differer"
fi

for outil in python3 systemctl; do
  command -v "$outil" >/dev/null || { rouge "$outil est requis"; exit 1; }
done
vert "   python3 $(python3 --version 2>&1 | cut -d' ' -f2), systemd present"

MANQUANTS=()
for module in yaml pymysql requests cryptography; do
  python3 -c "import $module" 2>/dev/null || MANQUANTS+=("$module")
done
if [[ ${#MANQUANTS[@]} -gt 0 ]]; then
  jaune "   modules Python absents : ${MANQUANTS[*]}"
  PAQUETS=()
  for m in "${MANQUANTS[@]}"; do
    case "$m" in
      yaml)    PAQUETS+=("python3-yaml") ;;
      pymysql) PAQUETS+=("python3-pymysql") ;;
      requests) PAQUETS+=("python3-requests") ;;
      # AES-GCM pour secret_store : les secrets chiffres en base
      cryptography) PAQUETS+=("python3-cryptography") ;;
    esac
  done
  faire apt-get install -y "${PAQUETS[@]}"
else
  vert "   modules Python presents"
fi

# --------------------------------------------------------------------------
etape "Compte de service"

if id -u "$UTILISATEUR" >/dev/null 2>&1; then
  vert "   $UTILISATEUR existe deja"
else
  faire useradd --system --home-dir "$PREFIXE" --shell /usr/sbin/nologin \
        --comment "Inventaire d'infrastructure" "$UTILISATEUR"
  vert "   $UTILISATEUR cree"
fi

# --------------------------------------------------------------------------
etape "Code applicatif dans $PREFIXE"

faire mkdir -p "$PREFIXE"
# deploy/ est indispensable : le service systemd lance deploy/collecte.sh
for element in *.py assets sql exemples client deploy docs; do
  [[ -e "$SOURCE/$element" ]] || continue
  faire cp -r "$SOURCE/$element" "$PREFIXE/"
done
faire cp "$SOURCE/README.md" "$PREFIXE/" 2>/dev/null || true
faire mkdir -p "$PREFIXE/pinned" "$PREFIXE/exports"

# La configuration n'est deposee que si elle manque : la remplacer effacerait
# les jetons par hote, la liste skip et le reglage du bastion.
if [[ -e "$PREFIXE/infra.yml" ]]; then
  vert "   infra.yml conserve (deja present)"
else
  faire cp "$SOURCE/infra.yml" "$PREFIXE/infra.yml"
  jaune "   infra.yml depose : a renseigner avant la premiere collecte"
fi

faire chown -R "$UTILISATEUR":"$UTILISATEUR" "$PREFIXE"
faire chmod 750 "$PREFIXE"
faire chmod +x "$PREFIXE/deploy/collecte.sh" 2>/dev/null || true
vert "   code en place"

# --------------------------------------------------------------------------
etape "Secrets"

if [[ -e "$FICHIER_ENV" ]]; then
  vert "   $FICHIER_ENV conserve"
else
  faire cp "$SOURCE/deploy/inventaire.env.example" "$FICHIER_ENV"
  faire chown root:"$UTILISATEUR" "$FICHIER_ENV"
  faire chmod 640 "$FICHIER_ENV"
  jaune "   $FICHIER_ENV cree depuis le modele : A RENSEIGNER"
fi

# Le service web a son propre fichier, et son propre compte de base : il ne
# doit pas voir les clefs OVH ni les jetons PVE, puisque c'est lui qui est
# joignable depuis le reseau. L'unite le charge avec "EnvironmentFile=-", qui
# tolere son absence — le service demarrerait donc sans mot de passe, pour
# echouer a la premiere requete. D'ou le depot ici.
if [[ $AVEC_WEB -eq 1 ]]; then
  if [[ -e "$FICHIER_ENV_WEB" ]]; then
    vert "   $FICHIER_ENV_WEB conserve"
  else
    faire cp "$SOURCE/deploy/inventaire-web.env.example" "$FICHIER_ENV_WEB"
    faire chown root:"$UTILISATEUR" "$FICHIER_ENV_WEB"
    faire chmod 640 "$FICHIER_ENV_WEB"
    jaune "   $FICHIER_ENV_WEB cree depuis le modele : A RENSEIGNER"
  fi
fi

# --------------------------------------------------------------------------
if [[ $AVEC_WEB -eq 1 ]]; then
  etape "Racine web"
  faire mkdir -p "$RACINE_WEB"
  if getent group www-data >/dev/null; then
    faire chown "$UTILISATEUR":www-data "$RACINE_WEB"
    faire chmod 750 "$RACINE_WEB"
  else
    faire chown "$UTILISATEUR":"$UTILISATEUR" "$RACINE_WEB"
  fi
  vert "   $RACINE_WEB prete"
fi

# --------------------------------------------------------------------------
etape "Unites systemd"

installer_unite() {
  local nom="$1"
  [[ -f "$SOURCE/deploy/$nom" ]] || return 0
  sauvegarder "/etc/systemd/system/$nom"
  faire cp "$SOURCE/deploy/$nom" "/etc/systemd/system/$nom"
  # Les fichiers venant d'un montage Windows arrivent en 755 ; systemd
  # avertit qu'une unite ne doit pas etre executable.
  faire chmod 644 "/etc/systemd/system/$nom"
  vert "   $nom installe"
}

installer_unite inventaire-collecte.service
[[ $AVEC_TIMER -eq 1 ]] && installer_unite inventaire-collecte.timer
[[ $AVEC_WEB -eq 1 ]] && installer_unite inventaire-web.service
faire systemctl daemon-reload

# --------------------------------------------------------------------------
if [[ $AVEC_APACHE -eq 1 ]]; then
  etape "Apache"
  if command -v apache2ctl >/dev/null; then
    faire a2enmod headers proxy proxy_http
    sauvegarder /etc/apache2/sites-available/inventaire.conf
    faire cp "$SOURCE/deploy/apache-inventaire.conf" \
             /etc/apache2/sites-available/inventaire.conf
    jaune "   vhost depose : ajuste ServerName avant d'activer"
    jaune "   puis : a2ensite inventaire && apache2ctl configtest && systemctl reload apache2"
  else
    jaune "   apache2 absent : vhost non installe"
    jaune "   la configuration reste disponible dans deploy/"
  fi
fi

# --------------------------------------------------------------------------
etape "Ce qu'il reste a faire"

RESTE=()
grep -q '^OVH_APPLICATION_KEY=.\+' "$FICHIER_ENV" 2>/dev/null \
  || RESTE+=("renseigner $FICHIER_ENV (cles OVH, token PVE, mot de passe base)")
if [[ $AVEC_WEB -eq 1 ]]; then
  grep -q '^INVENTORY_DB_PASSWORD=.\+' "$FICHIER_ENV_WEB" 2>/dev/null \
    || RESTE+=("renseigner $FICHIER_ENV_WEB (mot de passe du compte inventory_web)")
fi
RESTE+=("creer la base et le compte : cd $PREFIXE && python3 setup_inventory_db.py --db-password '<mdp>'")
RESTE+=("creer le compte du service web : mariadb -u root -p < $PREFIXE/sql/setup_db.sql")
RESTE+=("verifier la separation : les deux requetes en fin de sql/setup_db.sql doivent ne rien renvoyer")
RESTE+=("eprouver la collecte : python3 infra_inventory.py --refresh --seulement 'ns1234*' --table")
RESTE+=("chiffrer les secrets : ./deploy/chiffrer-secrets.sh, puis basculer les unites en LoadCredentialEncrypted")
RESTE+=("si secrets en base : python3 inventory_cli.py --init-key | systemd-creds encrypt --name=master_key - /etc/inventaire-infra/creds/master_key.cred")
RESTE+=("le detail de tout cela : $PREFIXE/docs/deploiement.md")
[[ $AVEC_TIMER -eq 1 ]] && RESTE+=("activer la collecte : systemctl enable --now inventaire-collecte.timer")
[[ $AVEC_WEB -eq 1 ]] && RESTE+=("activer le service web : systemctl enable --now inventaire-web.service")
[[ $AVEC_APACHE -eq 1 ]] && RESTE+=("ajuster ServerName puis a2ensite inventaire")

for i in "${!RESTE[@]}"; do
  printf '   %d. %s\n' "$((i+1))" "${RESTE[$i]}"
done

echo
if [[ $SIMULATION -eq 1 ]]; then
  jaune "Simulation terminee : rien n'a ete modifie."
else
  vert "Deploiement termine."
fi
