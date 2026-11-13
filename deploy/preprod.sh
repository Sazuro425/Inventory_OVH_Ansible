#!/usr/bin/env bash
# Monte l'inventaire sur une Debian 12 vierge, en une commande.
#
#   sudo bash deploy/preprod.sh --nom inventaire.preprod.lan
#
# Options :
#   --nom NOM          nom DNS de la page (demande s'il manque)
#   --cert F --cle F   certificat et clef TLS a utiliser ; sans eux, un
#                      certificat auto-signe est genere pour --nom
#
# Ce que fait le script, dans l'ordre :
#    1. installe les paquets (MariaDB, Apache, modules Python)
#    2. installe le code dans /opt/inventaire-infra (deploy/installer.sh)
#    3. cree la base et ses deux comptes, avec des mots de passe tires au
#       hasard que personne ne voit : ils vont directement dans systemd-creds
#    4. cree les tables, applique les migrations et les droits
#    5. tire la clef maitresse et la clef de la boite de depot des jetons PVE
#    6. branche Apache en HTTPS devant le service web, et le demarre
#    7. cree le compte admin (mot de passe affiche UNE fois)
#
# Ce qu'il ne fait pas, parce qu'il ne peut pas les inventer : les clefs OVH
# (deploy/poser-secret.sh ovh) et les jetons PVE (depuis la page). La
# collecte ne demarre qu'une fois les clefs OVH posees.
#
# Aucun secret n'est jamais ecrit en clair, ni sur le disque, ni dans un
# argument de commande. On peut relancer le script sans risque : chaque
# etape regarde d'abord si elle est deja faite.

set -euo pipefail

SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIXE=/opt/inventaire-infra
CREDS=/etc/inventaire-infra/creds
CREDS_WEB=/etc/inventaire-infra/creds-web
RACINE_WEB=/var/www/inventaire
CERT=/etc/ssl/certs/inventaire.pem
CLE=/etc/ssl/private/inventaire.key

NOM=""
CERT_FOURNI=""
CLE_FOURNIE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --nom)  NOM="$2"; shift ;;
    --cert) CERT_FOURNI="$2"; shift ;;
    --cle)  CLE_FOURNIE="$2"; shift ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "option inconnue : $1" >&2; exit 2 ;;
  esac
  shift
done

vert()  { printf '\033[32m%s\033[0m\n' "$*"; }
jaune() { printf '\033[33m%s\033[0m\n' "$*"; }
rouge() { printf '\033[31m%s\033[0m\n' "$*" >&2; }
etape() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
stop()  { rouge "ARRET : $*"; exit 1; }
trap 'rouge "ARRET : la commande ligne $LINENO a echoue. Rien n est perdu : corrige et relance le script."' ERR

sql()   { mariadb --batch --skip-column-names "$@"; }
# systemd-creds sans TPM previent a chaque appel que sa clef hote n'est pas
# sur un disque chiffre : dit une fois a l'etape 0, inutile de le repeter
chiffrer() {
  systemd-creds encrypt --name="$1" - "$2" \
    2> >(grep -v 'not located on encrypted media' >&2)
}
mdp()   { python3 -c 'import secrets; print(secrets.token_urlsafe(32))'; }

# --------------------------------------------------------------------------
etape "0. Verifications"

[[ $EUID -eq 0 ]] || stop "lance-le avec sudo (ou en root)"
[[ -r /etc/debian_version ]] || stop "Debian attendue"
case "$(cut -d. -f1 /etc/debian_version)" in
  12|13) vert "   Debian $(cat /etc/debian_version)" ;;
  *) jaune "   Debian $(cat /etc/debian_version) : teste sur la 12 seulement" ;;
esac
command -v systemd-creds >/dev/null || stop "systemd-creds absent (systemd 250+)"
if systemd-creds has-tpm2 >/dev/null 2>&1; then
  vert "   TPM2 present : les secrets chiffres ne s'ouvriront que sur cette VM"
else
  jaune "   pas de TPM2 : les secrets sont chiffres par la clef de la VM"
  jaune "   (/var/lib/systemd/credential.secret, lisible par root seul). Normal sans vTPM."
fi
[[ -f "$SOURCE/inventory_cli.py" ]] || stop "lance-le depuis le dossier du code"

if [[ -z "$NOM" ]]; then
  if [[ -t 0 ]]; then
    read -r -p "   Nom DNS de la page (ex. inventaire.preprod.lan) : " NOM
  fi
  [[ -n "$NOM" ]] || stop "--nom manquant"
fi
[[ "$NOM" =~ ^[A-Za-z0-9.-]+$ ]] || stop "nom invalide : $NOM"
vert "   page publiee sur https://$NOM/"

if [[ -n "$CERT_FOURNI$CLE_FOURNIE" ]]; then
  [[ -r "$CERT_FOURNI" && -r "$CLE_FOURNIE" ]] \
    || stop "--cert et --cle vont ensemble, et doivent etre lisibles"
fi

# --------------------------------------------------------------------------
etape "1. Paquets"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq mariadb-server apache2 openssl curl ca-certificates \
  python3 python3-yaml python3-pymysql python3-requests python3-cryptography \
  > /dev/null
systemctl enable --now mariadb >/dev/null 2>&1
vert "   paquets installes, MariaDB demarre"

# --------------------------------------------------------------------------
etape "2. Code"

bash "$SOURCE/deploy/installer.sh" --sans-apache --secrets-chiffres \
  | sed 's/^/   /'
chmod 755 "$PREFIXE"/deploy/*.sh
mkdir -p "$CREDS" "$CREDS_WEB"
chmod 700 /etc/inventaire-infra "$CREDS" "$CREDS_WEB"

# --------------------------------------------------------------------------
etape "3. Base et comptes"

sql -e "CREATE DATABASE IF NOT EXISTS inventory
        CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"

# Un compte et son secret : si l'un des deux manque, on retire un mot de
# passe neuf et on recree les deux. Le mot de passe ne vit que dans une
# variable de ce script ; il part a MariaDB par l'entree standard et a
# systemd-creds par un tube, jamais en argument.
compte() {
  local compte="$1" fichier_sql="$2" marqueur="$3" cred="$4" existe pw script
  existe="$(sql -e "SELECT COUNT(*) FROM mysql.user
                    WHERE user='$compte' AND host='localhost'")"
  if [[ -s "$cred" && "$existe" -gt 0 ]]; then
    vert "   $compte : deja en place"
    return
  fi
  pw="$(mdp)"
  script="$(<"$SOURCE/sql/$fichier_sql")"
  script="${script//$marqueur/$pw}"
  printf '%s\n' "$script" | sql > /dev/null
  printf '%s' "$pw" | chiffrer inventory_db_password "$cred"
  chmod 600 "$cred"
  pw="" script=""
  vert "   $compte : cree, mot de passe chiffre dans $cred"
}
compte inventory     setup_db.sql     MOT_DE_PASSE_A_DEFINIR \
       "$CREDS/inventory_db_password.cred"

if [[ "$(sql -e "SELECT COUNT(*) FROM information_schema.tables
                 WHERE table_schema='inventory' AND table_name='schema_version'")" -eq 0 ]]; then
  sql inventory < "$SOURCE/sql/schema.sql"
  vert "   tables creees (schema v1)"
else
  vert "   tables deja presentes"
fi

bash "$SOURCE/deploy/poser-secret.sh" unites > /dev/null
# (la ligne qui renvoie a droits.sql : fait juste en dessous)
"$PREFIXE/deploy/inventory.sh" --migrer | grep -v 'droits.sql' | sed 's/^/   /'

compte inventory_web setup_db_web.sql MOT_DE_PASSE_WEB_A_DEFINIR \
       "$CREDS_WEB/inventory_db_password.cred"
sql < "$SOURCE/sql/droits.sql" > /dev/null

fuite="$(sql -e "SELECT CONCAT(grantee,' ',table_name) FROM information_schema.table_privileges
                 WHERE grantee LIKE \"'inventory_web'%\" AND table_name='secret_store'
                 UNION ALL
                 SELECT CONCAT(grantee,' ',privilege_type) FROM information_schema.schema_privileges
                 WHERE grantee LIKE \"'inventory_web'%\"")"
[[ -z "$fuite" ]] || stop "le compte web voit trop de choses : $fuite"
vert "   droits poses ; le compte web n'atteint pas secret_store"

# --------------------------------------------------------------------------
etape "4. Clef maitresse et boite de depot"

if [[ -s "$CREDS/master_key.cred" ]]; then
  vert "   clef maitresse deja scellee"
else
  # Des secrets en base sans leur clef : en tirer une autre ne les rendrait
  # pas lisibles, et masquerait le probleme.
  n="$(sql -e "SELECT COUNT(*) FROM inventory.secret_store")"
  [[ "$n" -eq 0 ]] || stop "$n secret(s) en base mais plus de clef maitresse : restaure $CREDS/master_key.cred"
  # dans un fichier temporaire d'abord : un echec ne doit pas laisser un
  # .cred vide, que la relance prendrait pour une clef en place
  tmp="$CREDS/.master_key.tmp"
  if ( cd "$PREFIXE" && runuser -u inventaire -- python3 inventory_cli.py --init-key 2>/dev/null ) \
       | chiffrer master_key "$tmp"; then
    chmod 600 "$tmp"
    mv "$tmp" "$CREDS/master_key.cred"
  else
    rm -f "$tmp"
    stop "tirage de la clef maitresse"
  fi
  vert "   clef maitresse tiree et scellee (elle n'a jamais touche le disque en clair)"
  bash "$SOURCE/deploy/poser-secret.sh" unites > /dev/null
fi

if [[ "$(sql -e "SELECT COUNT(*) FROM inventory.cle_depot WHERE actif=1")" -gt 0 ]]; then
  vert "   boite de depot deja prete"
else
  "$PREFIXE/deploy/inventory.sh" --init-depot | sed 's/^/   /'
fi

# --------------------------------------------------------------------------
etape "5. HTTPS et Apache"

if [[ -n "$CERT_FOURNI" ]]; then
  install -m 644 "$CERT_FOURNI" "$CERT"
  install -m 600 "$CLE_FOURNIE" "$CLE"
  vert "   certificat fourni installe"
elif [[ -s "$CERT" && -s "$CLE" ]]; then
  vert "   certificat deja present ($CERT), conserve"
else
  openssl req -x509 -newkey rsa:3072 -nodes -days 825 \
    -keyout "$CLE" -out "$CERT" -subj "/CN=$NOM" \
    -addext "subjectAltName=DNS:$NOM" 2>/dev/null
  chmod 600 "$CLE"
  jaune "   certificat AUTO-SIGNE genere pour $NOM : le navigateur avertira, c'est normal en preprod"
fi

a2enmod -q ssl headers proxy proxy_http > /dev/null
sed "s/inventaire\.exemple\.lan/$NOM/g" "$SOURCE/deploy/apache-inventaire-local.conf" \
  > /etc/apache2/sites-available/inventaire.conf
a2dissite -q 000-default >/dev/null 2>&1 || true
a2ensite -q inventaire > /dev/null
apache2ctl -t 2>&1 | grep -v '^Syntax OK$' | sed 's/^/   /' || true
apache2ctl -t >/dev/null 2>&1 || stop "configuration Apache invalide (apache2ctl -t)"
systemctl enable apache2 >/dev/null 2>&1
systemctl reload-or-restart apache2
vert "   Apache sert https://$NOM/"

# --------------------------------------------------------------------------
etape "6. Service web"

# Une page d'attente tant qu'aucune collecte n'a produit la carte : sans
# elle, le service rendrait une erreur 503 apres la connexion.
if [[ ! -e "$RACINE_WEB/index.html" ]]; then
  cat > "$RACINE_WEB/index.html" <<'HTML'
<!doctype html><html lang="fr"><meta charset="utf-8">
<title>Inventaire</title>
<h1>Inventaire : en attente de la premiere collecte</h1>
<p>Le service tourne et ta session est ouverte. La carte apparaitra ici
apres la premiere collecte (cles OVH posees, puis collecte lancee).</p>
</html>
HTML
  chown inventaire:www-data "$RACINE_WEB/index.html"
  chmod 640 "$RACINE_WEB/index.html"
fi

systemctl enable inventaire-web.service >/dev/null 2>&1
systemctl restart inventaire-web.service
for _ in $(seq 1 20); do
  curl -sf -o /dev/null http://127.0.0.1:8080/api/sante && break
  sleep 0.5
done
sante="$(curl -s http://127.0.0.1:8080/api/sante || true)"
[[ -n "$sante" ]] || stop "le service web ne repond pas : journalctl -u inventaire-web -n 30"
vert "   service web demarre : $sante"

code="$(curl -sk -o /dev/null -w '%{http_code}' --resolve "$NOM:443:127.0.0.1" \
        "https://$NOM/connexion")"
[[ "$code" == 200 ]] || stop "https://$NOM/connexion rend $code au lieu de 200"
vert "   https://$NOM/connexion repond (200)"

# --------------------------------------------------------------------------
etape "7. Collecte"

if [[ -s "$CREDS/ovh_application_key.cred" ]]; then
  systemctl enable --now inventaire-collecte.timer >/dev/null 2>&1
  vert "   clefs OVH presentes : collecte horaire activee"
else
  jaune "   clefs OVH pas encore posees : collecte en attente (normal a ce stade)"
fi

# --------------------------------------------------------------------------
etape "8. Compte admin"

if [[ "$(sql -e "SELECT COUNT(*) FROM inventory.compte_local WHERE login='admin'")" -gt 0 ]]; then
  vert "   le compte admin existe deja"
  echo "   mot de passe perdu ? sudo $PREFIXE/deploy/inventory.sh --compte-local admin"
elif [[ -t 0 && -t 1 ]]; then
  jaune "   Note le mot de passe ci-dessous : il ne sera plus jamais affiche."
  "$PREFIXE/deploy/inventory.sh" --compte-local admin
else
  jaune "   pas de terminal : cree-le avec"
  echo "   sudo $PREFIXE/deploy/inventory.sh --compte-local admin"
fi

trap - ERR
etape "Termine"
vert "La preprod tourne. Suite : la partie 2 de docs/preprod-pas-a-pas.md"
