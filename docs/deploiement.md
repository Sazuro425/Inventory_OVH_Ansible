# Deploiement sur la VM

Suivi pas a pas, dans cet ordre. Chaque etape se verifie avant de passer a la
suivante : une base sans ses `GRANT` ou une clef maitresse posee avant les
comptes se rattrapent mal.

Rien de ce qui ecrit en base n'a jamais tourne — ni le DDL, ni les `GRANT`,
ni `inventory --ovh`. C'est ce deploiement qui les eprouve. Garde une
sauvegarde de la base avant l'etape 3 si elle contient deja des snapshots.

Cible : Debian 12+, MariaDB 10.11+, systemd 250+ pour `systemd-creds`.

---

## 1. Installer le code

```bash
git clone <depot> inventaire-infra && cd inventaire-infra
sudo ./deploy/installer.sh --simulation     # montre ce qui serait fait
sudo ./deploy/installer.sh
```

L'installeur cree le compte de service `inventaire`, depose le code dans
`/opt/inventaire-infra`, installe les unites systemd, et pose les deux
fichiers d'environnement en `640 root:inventaire`. Il termine par la liste de
ce qu'il reste a faire — qui recoupe ce document.

**Verifier :** `python3 -c "import cryptography"` ne doit rien dire.
L'installeur l'ajoute depuis `python3-cryptography`, mais une VM sans acces
aux depots passerait l'etape en silence.

---

## 2. Renseigner les deux fichiers d'environnement

Deux fichiers, parce que les deux services n'ont pas la meme exposition.

`/etc/inventaire-infra.env` — la collecte. Clefs OVH, jeton PVE, mot de passe
de la base.

`/etc/inventaire-infra-web.env` — le service web. **Le mot de passe de son
compte, et rien d'autre.** Il ne doit pas voir les clefs OVH ni les jetons
PVE : c'est lui qui est joignable depuis le reseau a travers Apache.

Choisis maintenant deux mots de passe de base distincts. Deux comptes qui
partagent un mot de passe ne sont qu'un seul compte avec deux noms.

---

## 3. Creer la base, les tables, les deux comptes

Deux voies, et **il ne faut pas les cumuler telles quelles** :
`setup_db.sql` contient un `ALTER USER` sur le compte `inventory`, qui
ecraserait son mot de passe par le marqueur du fichier.

### Voie A — le script Python, puis la partie web seulement

`setup_inventory_db.py` cree la base, le compte de collecte avec ses droits,
et applique `sql/schema.sql`. Il ne connait pas le compte du service web.

```bash
cd /opt/inventaire-infra
python3 setup_inventory_db.py --db-password '<mdp collecte>'
python3 setup_inventory_db.py --check        # relit et confirme
```

Puis le compte web, en reprenant **a la main** le bloc « Compte du service
web » de `sql/setup_db.sql`, mot de passe remplace. Tout le fichier
rejouerait l'`ALTER USER` du compte de collecte.

### Voie B — tout en SQL

```bash
# remplacer MOT_DE_PASSE_A_DEFINIR et MOT_DE_PASSE_WEB_A_DEFINIR d'abord
mariadb -u root -p < sql/setup_db.sql
mariadb -u root -p inventory < sql/schema.sql
```

Dans cet ordre : `setup_db.sql` cree la base, `schema.sql` la remplit. Les
`GRANT` sur les vues portent sur des objets qui n'existent pas encore, ce que
MariaDB accepte — un `GRANT` ne verifie pas la cible.

> **Attention :** `--db-password` place le mot de passe sur la ligne de
> commande, donc visible dans un `ps` le temps de l'execution, et conserve
> dans l'historique du shell. Prefixe la commande d'une espace si ton shell
> ignore ces lignes (`HISTCONTROL=ignorespace`), et considere ce mot de passe
> comme compromis si la machine est partagee.

### Verifier la separation des comptes

Les deux dernieres requetes de `setup_db.sql` s'executent toutes seules et
**doivent ne rien renvoyer**. Une ligne signifie que `inventory_web` atteint
`secret_store`, et la separation ne tient pas :

```sql
-- droit sur la table des secrets
SELECT grantee, table_name FROM information_schema.table_privileges
 WHERE grantee LIKE "'inventory_web'%" AND table_name = 'secret_store';

-- droit au niveau du schema, qui couvrirait la table sans apparaitre ci-dessus
SELECT grantee, privilege_type FROM information_schema.schema_privileges
 WHERE grantee LIKE "'inventory_web'%";
```

**Verifier aussi :** six tables et cinq vues.

```bash
mariadb -u root -p -e "SHOW TABLES FROM inventory;"
```

---

## 4. Eprouver la collecte avant tout chiffrement

Secrets encore en clair dans le fichier d'environnement, sur un seul hote :

```bash
cd /opt/inventaire-infra
set -a; . /etc/inventaire-infra.env; set +a
python3 infra_inventory.py --refresh --seulement 'ns1234*' --table
python3 infra_inventory.py --refresh --seulement 'ns1234*' --store-db
```

**Verifier :** une ligne dans `collect_run` avec `status = 'ok'`.

```bash
mariadb -u root -p -e "SELECT id,status,nb_servers,nb_nodes,nb_guests \
  FROM inventory.collect_run ORDER BY id DESC LIMIT 3;"
```

C'est ici qu'on eprouve la deduplication de cluster, jamais testee sur un
vrai parc : les donnees generees par `generer_demo.py` arrivent deja
dedupliquees. Prends un cluster a trois noeuds au moins, et eteins-en un
volontairement. Attendu : les trois noeuds dans `node_snapshot`, le noeud
eteint avec `reachable = 0`, et **chaque VM une seule fois** dans
`guest_snapshot`.

---

## 5. Sceller la clef maitresse

Rien avant cette etape n'en depend ; tout ce qui suit en depend.

```bash
systemd-creds has-tpm2 && echo "TPM present"
sudo mkdir -p /etc/inventaire-infra/creds && sudo chmod 700 /etc/inventaire-infra/creds

cd /opt/inventaire-infra
python3 inventory_cli.py --init-key | sudo systemd-creds encrypt \
    --name=master_key - /etc/inventaire-infra/creds/master_key.cred
```

La clef sort sur la sortie standard et les instructions sur l'erreur
standard : le tube ci-dessus ne la fait jamais toucher le disque en clair.

Sans TPM, systemd se rabat sur une clef hote dans `/var/lib/systemd/`,
lisible par root seul. La protection contre le vol de disque tient, celle
contre un root compromis non.

> **Point de non-retour.** Perdre ce fichier rend tous les secrets en base
> definitivement illisibles. Il est lie a cette machine : une sauvegarde
> restauree ailleurs ne le dechiffrera pas. Si la VM est recreee, il faut
> reposer les secrets, pas restaurer le fichier.

Decommente dans `/etc/systemd/system/inventaire-collecte.service` :

```ini
LoadCredentialEncrypted=master_key:/etc/inventaire-infra/creds/master_key.cred
```

**Uniquement dans cette unite.** Le service web touche la meme base ; s'il
portait cette clef, un defaut dans le service expose derriere Apache
donnerait tous les jetons PVE.

---

## 6. Chiffrer les autres secrets

```bash
sudo ./deploy/chiffrer-secrets.sh
sudo ./deploy/chiffrer-secrets.sh \
    --source /etc/inventaire-infra-web.env \
    --dest   /etc/inventaire-infra/creds-web
```

Deux dossiers, et non un prefixe de nom : le nom du credential doit rester
`inventory_db_password` dans les deux services, puisque c'est ce que le code
cherche. C'est le chemin qui les distingue.

Le script deplace le fichier en clair vers `.avant-chiffrement` plutot que de
le supprimer — si une unite n'etait pas encore convertie, le service ne
demarrerait plus.

Dans chaque unite : decommenter les `LoadCredentialEncrypted`, commenter
`EnvironmentFile`. Puis :

```bash
sudo systemctl daemon-reload
sudo systemctl restart inventaire-collecte.service
sudo journalctl -u inventaire-collecte.service -n 30 --no-pager
```

**Verifier :** le journal dit `secrets fournis par systemd-creds`. Si ce
n'est pas le cas, le service lit encore l'environnement, et les deux dernieres
lignes du fichier en clair ne sont pas encore inutiles.

Une fois les deux services repartis :

```bash
sudo shred -u /etc/inventaire-infra.env.avant-chiffrement
sudo shred -u /etc/inventaire-infra-web.env.avant-chiffrement
```

---

## 7. Basculer les jetons PVE en base

Facultatif, et c'est la seule etape qui apporte vraiment la centralisation :
trente jetons dans une table au lieu de trente lignes a redeployer.

```bash
cd /opt/inventaire-infra
sudo -u inventaire python3 inventory_cli.py --list
sudo -u inventaire python3 inventory_cli.py --pve ns1234.ip-1-2-3.eu
```

La commande teste le jeton avant de l'ecrire, affiche le nom du secret cree,
et rappelle la ligne a ajouter dans `infra.yml` :

```yaml
pve:
  tokens:
    ns1234.ip-1-2-3.eu: "db:pve_token_ns1234_ip_1_2_3_eu"
```

Lance la commande sous le compte de service, pas sous root : la clef
maitresse est chargee par l'unite, donc `CREDENTIALS_DIRECTORY` n'existe pas
dans un shell ordinaire. Pour une execution a la main, exporte
`INVENTAIRE_MASTER_KEY` le temps de la session — jamais dans un fichier.

**Verifier :** une collecte sur ce seul hote, avec la reference `db:` en
place.

```bash
sudo -u inventaire python3 infra_inventory.py --refresh \
    --seulement 'ns1234*' --table
```

C'est le premier test du chemin nominal de `db:` — jusqu'ici seul le chemin
d'echec a ete eprouve.

---

## 8. Activer la collecte et le service web

```bash
sudo systemctl enable --now inventaire-collecte.timer
sudo systemctl enable --now inventaire-web.service
systemctl list-timers inventaire-collecte.timer
curl -s localhost:8080/api/sante | python3 -m json.tool
```

**Verifier :** `/api/sante` rend `"etat": "ok"` et l'age du dernier
snapshot. Un `"degrade"` dit lequel des deux manque — la base ou le snapshot.

C'est aussi le test du compte `inventory_web` : si la reponse est
`degrade` avec une erreur de connexion, ses `GRANT` sont incomplets.

---

## 9. Apache et OIDC

```bash
sudo cp deploy/apache-inventaire.conf /etc/apache2/sites-available/inventaire.conf
sudo cp deploy/inventaire-oidc.conf.example /etc/apache2/conf-available/inventaire-oidc.conf
# ajuster ServerName, les URL Authentik, le secret client
sudo a2enmod proxy proxy_http auth_openidc
sudo a2ensite inventaire && sudo apache2ctl configtest
sudo systemctl reload apache2
```

Le parcours OIDC complet n'a jamais ete suivi avec un vrai Authentik. Deux
points a verifier de pres : qu'Apache pose bien `X-Remote-User`, et qu'il
l'**ecrase** au lieu de le transmettre. Un en-tete venant du client et laisse
passer vaut usurpation d'identite.

```bash
# doit echouer : le service n'ecoute que sur la boucle locale
curl -s -H 'X-Remote-User: nimporte' http://<ip-publique>:8080/api/sante
```

---

## 10. Retour arriere

Par ordre de gravite croissante.

**Un secret en base refuse.** Les versions precedentes sont conservees,
inactives. Reactiver la derniere qui fonctionnait :

```sql
UPDATE inventory.secret_store SET actif = 0 WHERE nom = 'pve_token_x';
UPDATE inventory.secret_store SET actif = 1
 WHERE nom = 'pve_token_x' AND version = <la bonne>;
```

**Une rotation de clef interrompue.** `inventory --list` signale deux
versions de clef en service. Relancer `inventory --rotate-key --appliquer` :
les secrets deja traites sont ignores.

**Le chiffrement systemd-creds empeche un service de demarrer.** Recommenter
les `LoadCredentialEncrypted`, decommenter `EnvironmentFile`, et restaurer le
fichier `.avant-chiffrement` — tant qu'il n'a pas ete `shred`.

**La clef maitresse est perdue.** Les secrets en base sont definitivement
illisibles. Repasser `infra.yml` sur des references `env:`, remplir le
fichier d'environnement, repartir a l'etape 5. Les snapshots d'inventaire,
eux, ne sont pas chiffres et restent intacts.
