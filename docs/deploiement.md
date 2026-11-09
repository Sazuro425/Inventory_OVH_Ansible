# Deploiement sur la VM

Suivi pas a pas, dans cet ordre. Chaque etape se verifie avant de passer a la
suivante : une base sans ses `GRANT` ou une clef maitresse posee avant les
comptes se rattrapent mal.

Rien de ce qui ecrit en base n'a jamais tourne — ni le DDL, ni les `GRANT`,
ni `inventory --ovh`. C'est ce deploiement qui les eprouve. Garde une
sauvegarde de la base avant l'etape 3 si elle contient deja des snapshots.

Cible : Debian 12+, MariaDB 10.11+, systemd 250+ pour `systemd-creds`.

Les commandes sont ecrites avec `sudo`. Si tu travailles directement en root
— cas courant sur une VM Debian minimale, ou `sudo` n'est meme pas installe —
retire-le partout.

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

Puis le compte web, avec le fichier qui ne contient que lui :

```bash
# remplacer MOT_DE_PASSE_WEB_A_DEFINIR d'abord
mariadb -u root -p < sql/setup_db_web.sql
```

`setup_db_web.sql` ne touche que `inventory_web`, et porte les deux requetes
de verification. C'est pour cela qu'il existe : rejouer tout `setup_db.sql`
reposerait le mot de passe du compte de collecte au marqueur du fichier, et
la collecte ne pourrait plus ouvrir la base.

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

Un seul mode d'authentification, OIDC par Authentik, pour la page comme pour
l'API. Le detail cote Authentik (groupes, scope mapping, provider) est en tete
de `deploy/inventaire-oidc.conf.example` : le suivre avant de remplir le
fichier.

```bash
sudo apt install libapache2-mod-auth-openidc
sudo a2enmod ssl headers proxy proxy_http auth_openidc
sudo cp deploy/apache-inventaire.conf /etc/apache2/sites-available/inventaire.conf
sudo install -d -m 750 -o root -g www-data /etc/apache2/conf-secrets
sudo install -m 640 -o root -g www-data deploy/inventaire-oidc.conf.example \
     /etc/apache2/conf-secrets/inventaire-oidc.conf
# remplir client, secret, passphrase ; ajuster ServerName et le certificat
sudo a2ensite inventaire && sudo apache2ctl configtest
sudo systemctl reload apache2
```

Le fichier OIDC va dans `conf-secrets`, la ou le vhost l'inclut, et non dans
`conf-available`. Le vhost l'inclut avec `Include` : s'il manque, Apache
refuse de demarrer, au lieu de servir une page dont l'authentification ne
marche pas.

Trois groupes Authentik ouvrent trois roles, declares dans `infra.yml` sous
`web:` avec les memes noms que dans le vhost :

| Groupe | Role | Ce qu'il permet |
|---|---|---|
| `inventaire-lecture` | lecture | la carte, la rentabilite, les exports |
| `inventaire-commercial` | commercial | ventes, elements, corrections (et la lecture) |
| `inventaire-admin` | admin | jeton pour le client Ansible (et la lecture) |

Apache n'admet que les membres de l'un des trois ; le service distingue
ensuite les roles. Les deux controles se recouvrent volontairement : une
erreur dans l'un ne suffit pas a ouvrir l'ecriture.

**Verifier la chaine Authentik > Apache > service.** Une fois connecte dans
le navigateur, ouvrir `https://inventaire.exemple.lan/api/moi` : il rend
l'identite, les groupes et les roles tels que le service les recoit. La page
les affiche aussi en haut a droite.

- `groupes` vide alors que l'utilisateur est membre : la claim n'arrive pas.
  Regarder l'onglet Preview du provider, et que le mapping natif
  `OpenID 'profile'` a bien ete retire.
- `utilisateur` vaut un UUID : `OIDCRemoteUserClaim` n'est pas pris en compte.
- Erreur « three parts » ou id_token rejete : une Encryption Key est posee
  sur le provider. La vider.

Et que personne d'autre qu'Apache ne joint le service :

```bash
# doit echouer : le service n'ecoute que sur la boucle locale
curl -s -H 'X-Remote-User: nimporte' http://<ip-publique>:8080/api/sante
# doit renvoyer 302 vers Authentik, ou 401 pour l'API
curl -s -o /dev/null -w '%{http_code}\n' https://inventaire.exemple.lan/
curl -s -o /dev/null -w '%{http_code}\n' https://inventaire.exemple.lan/api/moi
# un en-tete forge par le client est efface par Apache : toujours 401
curl -s -H 'X-Remote-User: admin' -H 'X-Remote-Groups: inventaire-admin' \
     https://inventaire.exemple.lan/api/moi
```

Le parcours complet n'a pas encore ete suivi avec un vrai Authentik.

---

## 10. Rentabilite, saisies et export

La vue **Rentabilite** de la page montre l'arbre cluster > serveur > VM >
elements declares, chaque carte cumulant ses enfants (PA, CA, ROI), avec la
capacite allouee contre la capacite physique. Le calcul est dans
`rentabilite.py`, et nulle part ailleurs : la page, l'export CSV et
`tests_rentabilite.py` lisent le meme resultat.

**Ce qu'il faut pour qu'elle affiche des chiffres :**

1. Le droit `GET /services*` sur la clef OVH. Sans lui, la collecte releve le
   materiel mais aucun cout : chaque serveur est signale « sans tarif ». Un
   cout peut aussi etre saisi a la main (bouton *Corriger*), avec un motif.
2. Le schema a jour, puis les droits du compte web, **dans cet ordre** :
   un `GRANT` sur une table qui n'existe pas encore echoue.

   ```bash
   mariadb -u root -p inventory < sql/schema.sql
   mariadb -u root -p < sql/setup_db_web.sql
   ```

   La derniere requete de `setup_db_web.sql` doit lister exactement :
   `client_token`, `commercial`, `element`, `surcharge`, `vue_rentabilite`
   en INSERT,UPDATE, et `journal_saisie` en INSERT seul. Le compte web ne peut ni corriger ni
   effacer le journal.
3. Le role commercial, dans `infra.yml` : `web.groupe_commercial` (le groupe
   Authentik) ou `web.commerciaux` (une liste de comptes). Sans l'un des deux,
   aucune saisie n'est acceptee. Voir l'etape 9 pour les trois roles.
4. Le bloc `LocationMatch` de `deploy/apache-inventaire.conf` : c'est le seul
   endroit ou `X-Remote-User` et `X-Remote-Groups` sont poses, apres l'OIDC.

**Verifier :**

```bash
python3 tests_rentabilite.py                   # le calcul, sans base
curl -s -o /dev/null -w '%{http_code}\n' https://inventaire.exemple.lan/api/rentabilite
# 302 vers Authentik sans session : l'API n'est pas ouverte
curl -s -H 'X-Remote-Groups: inventaire-commercial' -H 'X-Remote-User: evan' \
     http://127.0.0.1:8080/api/rentabilite | head -c 200
# depuis la VM seulement : rend l'arbre
```

**Les vues.** La rentabilite s'ouvre sur la vue d'ensemble, en graphiques.
Chaque graphique bascule en tableau et s'exporte en CSV, filtres compris.
Les filtres (clusters, datacenters, clients, genres d'element, texte, a
perte, avec alerte) valent pour l'arbre, le tableau et les graphiques ; ils
choisissent des cartes, ils ne recalculent pas leurs montants.

« Enregistrer la vue » garde sous un nom — *Commercial*, *Compta
mensuelle* — les filtres, la presentation, le niveau et le tri du tableau.
Une vue est partagee avec l'equipe par defaut, ou privee. Tout lecteur en
cree ; seul son auteur, ou un admin, la modifie ou la retire. Chaque
navigateur rouvre la derniere vue choisie.

**Ce que valent les chiffres.** Tout est ramene au mois. Le ROI d'un serveur
est une photo : dans un cluster, une VM migre d'un noeud a l'autre, et celui
du cluster est le chiffre stable. Les contrats dont la VM a disparu et les
elements dont le parent a ete retire sont comptes dans le total et listes
« hors parc », jamais perdus en silence.

**L'export CSV** est en UTF-8 avec BOM, point-virgule et virgule decimale :
il s'ouvre tel quel dans Excel et Calc en francais, les montants restent des
nombres. Une saisie commencant par `=`, `+`, `-` ou `@` y est prefixee d'une
apostrophe, pour qu'un nom de site ne s'execute pas comme une formule chez la
personne qui ouvre le fichier.

## 11. Retour arriere

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
