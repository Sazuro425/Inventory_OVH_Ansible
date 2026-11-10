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

Puis les migrations, le compte web, et ses droits :

```bash
python3 inventory_cli.py --migrer             # sous le compte de collecte
# remplacer MOT_DE_PASSE_WEB_A_DEFINIR d'abord
mariadb -u root -p < sql/setup_db_web.sql
mariadb -u root -p < sql/droits.sql
```

`setup_db_web.sql` ne fait que creer `inventory_web` (ou changer son mot de
passe) : rejouer tout `setup_db.sql` reposerait le mot de passe du compte de
collecte au marqueur du fichier. `droits.sql` ne contient que les droits,
sans mot de passe, et porte les requetes de verification : c'est lui qu'on
rejoue apres chaque mise a jour.

### Voie B — tout en SQL

```bash
# remplacer MOT_DE_PASSE_A_DEFINIR et MOT_DE_PASSE_WEB_A_DEFINIR d'abord
mariadb -u root -p < sql/setup_db.sql
mariadb -u root -p inventory < sql/schema.sql
python3 inventory_cli.py --migrer             # sous le compte de collecte
mariadb -u root -p < sql/setup_db_web.sql
mariadb -u root -p < sql/droits.sql
```

Dans cet ordre, et pas autrement : `setup_db.sql` cree la base et le compte
de collecte, `schema.sql` cree les tables (version 1), `--migrer` applique
les versions suivantes, `setup_db_web.sql` cree le compte web,
`droits.sql` lui donne ses droits. MariaDB refuse un `GRANT` sur une
table qui n'existe pas encore (erreur 1146) : les droits viennent en dernier.

`tests_mariadb.py` rejoue exactement cette sequence sur un MariaDB jetable.

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

### Deposer les jetons depuis la page (boite scellee)

Plutot que trente `inventory --pve` en SSH, un admin depose les jetons depuis
la page : bouton **Recolte**, ou fiche d'un hyperviseur dans le schema. Le
service web ne peut pas relire ce qu'il recoit :

- il scelle le jeton des reception avec une clef **publique** (X25519, AES-GCM,
  l'hote lie au chiffre) et l'enregistre « en attente » dans `depot_jeton` ;
- au passage suivant, `collecte.sh` lance `inventory_cli.py --traiter-depots` :
  la collecte ouvre le depot avec la clef privee (elle-meme dans
  `secret_store`, chiffree par la clef maitresse), teste le jeton sur
  l'hyperviseur, puis le range dans `secret_store` ou le refuse avec son
  motif, que la page affiche ;
- un jeton accepte sert directement, sans ligne dans `infra.yml`
  (`pve.jetons_deposes: true`). Un hote nomme sous `pve.tokens` garde le sien.

Une fois, apres l'etape 5 :

```bash
sudo systemd-run --quiet --pty --wait --collect -p User=inventaire \
    -p WorkingDirectory=/opt/inventaire-infra \
    -p LoadCredentialEncrypted=master_key:/etc/inventaire-infra/creds/master_key.cred \
    -p LoadCredentialEncrypted=inventory_db_password:/etc/inventaire-infra/creds/inventory_db_password.cred \
    python3 inventory_cli.py --init-depot
```

Le compte de recolte se cree sur **un noeud par cluster** (la page rappelle
les trois commandes `pveum`) ; au depot, les autres noeuds du meme cluster
sont coches d'office. Pour ne pas attendre le timer :
`sudo systemctl start inventaire-collecte.service`.

Ce qu'un service web compromis obtiendrait : les jetons deposes **apres** la
compromission, qu'il voit passer le temps de les sceller. Ni les depots
anterieurs, ni un seul jeton deja en service.

**Une echeance par jeton.** Le formulaire de depot propose un an et affiche
la commande `pveum ... --expire` qui la pose sur l'hyperviseur ; la date est
gardee avec le depot (migration 0004). La vue Recolte trie et signale les
jetons expires ou a renouveler sous 30 jours. Un jeton pose autrement
(`infra.yml`, `inventory --pve`) a une echeance inconnue de l'application.

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

## 9. Apache et l'authentification

Deux modes, choisis par `web.auth` dans `infra.yml`, jamais les deux a la
fois :

| `web.auth` | Qui authentifie | Vhost |
|---|---|---|
| `local` | le service, comptes locaux en base | `deploy/apache-inventaire-local.conf` |
| `oidc` | Apache, par Authentik | `deploy/apache-inventaire.conf` |

On commence en `local` tant que le client OIDC n'existe pas, et on bascule
ensuite (9b) : les comptes locaux restent en base, ils ne servent plus.

### 9a. Comptes locaux, en attendant l'OIDC

Apache termine le TLS et passe tout au service, page comprise. Le service
exige une session, sert la carte a une session ouverte seulement, et
renvoie le reste vers `/connexion`. Les en-tetes `X-Remote-*` sont ignores
dans ce mode, meme si un client les envoie.

```bash
sudo a2enmod ssl headers proxy proxy_http
sudo cp deploy/apache-inventaire-local.conf /etc/apache2/sites-available/inventaire.conf
# ajuster ServerName et le certificat
sudo a2ensite inventaire && sudo apache2ctl configtest
sudo systemctl reload apache2
```

Le compte admin, genere sous le compte de collecte (le compte web ne peut
pas creer de compte, il n'a pas INSERT sur `compte_local`) :

```bash
cd /opt/inventaire-infra
sudo systemd-run --quiet --pty --wait --collect -p User=inventaire \
    -p WorkingDirectory=/opt/inventaire-infra \
    -p LoadCredentialEncrypted=inventory_db_password:/etc/inventaire-infra/creds/inventory_db_password.cred \
    python3 inventory_cli.py --compte-local admin
```

Le mot de passe est tire au hasard et **affiche une seule fois** ; la
commande refuse une sortie redirigee vers un fichier. A la premiere
connexion, la page impose d'en choisir un autre (12 caracteres au moins,
sans l'identifiant) ; d'ici la, le compte n'a aucun role, l'API lui repond
403. Relancer la meme commande reinitialise le compte : nouveau mot de
passe, sessions fermees, compte debloque. D'autres comptes se creent de la
meme facon, avec `--roles lecture` ou `--roles commercial`.

Ce que le mode local garantit :

- mot de passe stocke en scrypt sale, jamais en clair ni reversible ;
- cinq echecs de suite bloquent le compte quinze minutes ;
- session dans un cookie `__Host-` Secure, HttpOnly, SameSite=Strict ; la
  base n'en garde que l'empreinte ;
- fin de session apres une heure d'inactivite, huit heures au plus, a la
  deconnexion, ou quand le mot de passe change ailleurs ;
- connexions, echecs, deconnexions et changements au journal des saisies.

Ce qu'il ne fait pas : de MFA, ni de limite par adresse. Sans OIDC, la
restriction reseau du vhost (`Require ip`, commentee) est la protection la
moins chere a ajouter.

**Verifier :**

```bash
# sans session : la carte renvoie vers /connexion, l'API refuse
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' https://inventaire.exemple.lan/
curl -s -o /dev/null -w '%{http_code}\n' https://inventaire.exemple.lan/api/rentabilite   # 401
# un en-tete d'identite forge ne vaut rien en mode local
curl -s -H 'X-Remote-User: admin' https://inventaire.exemple.lan/api/moi                 # 401
```

### 9b. OIDC

Un seul mode d'authentification, OIDC par Authentik, pour la page comme pour
l'API. Dans `infra.yml`, `web.auth: oidc` ; puis redemarrer le service. Le detail cote Authentik (groupes, scope mapping, provider) est en tete
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

#### MFA (NIS 2)

L'application ne fait pas la MFA elle-meme : c'est Authentik qui l'exige, et
l'application n'accepte que ce qu'Authentik a laisse passer.

1. *Flows and Stages > Stages* : un **Authenticator Validation Stage**, classes
   TOTP et WebAuthn, *Not configured action* sur **Force the user to configure
   an authenticator** — un compte sans second facteur doit en enregistrer un a
   sa prochaine connexion, au lieu de passer.
2. *Flows* : un flow d'authentification propre a l'inventaire (copie de
   `default-authentication-flow`), avec ce stage lie apres le mot de passe.
3. Sur le **provider** de l'inventaire, *Authentication flow* : ce flow. Les
   autres applications d'Authentik ne sont pas touchees.
4. Verifier avec un compte de test : sans TOTP enregistre, Authentik doit
   demander d'en configurer un avant de revenir sur l'inventaire.

Pour aller plus loin, Apache peut refuser une session qui n'aurait pas passe
la MFA, si Authentik la signale dans le jeton (claim `amr` ou `acr`). Ouvrir
l'onglet *Preview* du provider apres une connexion avec MFA ; seulement si
la claim y figure, ajouter dans le `<Location />` du vhost la ligne
`Require claim amr:mfa` (a adapter a la valeur vue). Sans la claim, la ligne
refuserait tout le monde.

**Le mode local devient un recours.** Une fois l'OIDC en service
(`web.auth: oidc`), les routes de connexion locale n'existent plus (404) et
les comptes locaux, restes en base, ne servent plus. Revenir en arriere si
Authentik tombe : `web.auth: local`, vhost `apache-inventaire-local.conf`,
redemarrer le service — puis revenir a l'OIDC des qu'il est retabli.

#### Journal des saisies chaine

Chaque saisie, connexion ou depot de jeton s'inscrit dans `journal_saisie`,
et chaque ligne porte l'empreinte de la precedente (migration 0003). Une
ligne modifiee ou effacee, meme par un administrateur de la base, casse la
chaine a cet endroit :

```bash
sudo systemd-run --quiet --pty --wait --collect -p User=inventaire \
    -p WorkingDirectory=/opt/inventaire-infra \
    -p LoadCredentialEncrypted=inventory_db_password:/etc/inventaire-infra/creds/inventory_db_password.cred \
    python3 inventory_cli.py --verifier-journal
```

La page l'affiche aussi a un admin, en tete du tiroir *Journal*. Reste le
cas d'une chaine entierement reecrite : a chaque passage, la collecte
recopie la tete de la chaine dans **son journal systeme**, hors de la base
(`journalctl -u inventaire-collecte | grep ancre`). Une tete qui ne retombe
plus sur une ancre deja ecrite signale une reecriture.

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
   cout peut aussi etre saisi a la main (bouton *Modifier*, ou *Prix et
   role* sur la fiche de l'hyperviseur), avec un motif.
2. Le schema a jour, puis les droits du compte web, **dans cet ordre** :
   un `GRANT` sur une table qui n'existe pas encore echoue (voir 12).

   La derniere requete de `droits.sql` doit lister exactement :
   `client_token`, `commercial`, `element`, `role`, `role_carte`,
   `session_locale`, `surcharge`, `vue_rentabilite` en INSERT,UPDATE,
   `compte_local` en UPDATE seul, `depot_jeton` et `journal_saisie` en
   INSERT seul. Le compte web ne peut ni corriger ni effacer le journal.
3. Le role commercial. En comptes locaux, il est porte par le compte
   (`--roles commercial`, l'admin genere l'a). En OIDC, dans `infra.yml` :
   `web.groupe_commercial` (le groupe Authentik) ou `web.commerciaux` (une
   liste de comptes) ; sans l'un des deux, aucune saisie n'est acceptee.
   Voir l'etape 9 pour les roles.
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

**Saisir depuis la topologie.** Un clic sur un hyperviseur ou une VM du
schema ouvre sa fiche : son bilan, son role, ses contrats et ses enfants
declares. Pour un commercial, trois boutons :

- *Prix et role* : le role de la carte (serveur web, LAMP, proxy,
  applicatif...), son cout et son prix de vente propres. Un prix corrige
  demande un motif ; le role non.
- *+ Enfants* : un ou plusieurs elements d'un coup, un nom par ligne, le
  role et les prix en commun. Une VM LAMP qui heberge quatre sites a 40 EUR :
  quatre lignes, 40, et le formulaire annonce avant l'envoi que la VM
  rapportera 160 EUR de plus. Le lot entre en base en une transaction, tout
  ou rien, une ligne de journal par element.
- *Editer* sur un enfant : son nom, ses prix, son contrat, son client.

Les memes saisies existent dans l'arbre de la vue Rentabilite.

**Comment une VM est designee.** Par sa reference `cluster/vmid` (`prod-1/105`,
`standalone:ns1.eu/100` pour un noeud seul), pas par son nom : une VM
renommee garde ses ventes, ses enfants, son role et ses corrections, et deux
VM homonymes dans deux clusters ne se melangent plus. Une saisie qui ne mene
plus a aucune VM (supprimee, ou restauree sous un autre vmid) n'est jamais
perdue : elle part « hors parc », ou en alerte pour une correction. Les
saisies faites par nom avant la migration 0002 sont converties quand le nom
est unique, et restent lues par leur nom sinon.

**Les roles** forment un catalogue (table `role`), pre-rempli par le schema :
serveur web, LAMP, proxy, applicatif, base de donnees, messagerie, DNS,
stockage, sauvegarde, supervision, site, vhost, domaine, licence, IP. Un
commercial l'etend depuis n'importe quel choix de role (*Nouveau role...*) ;
rien n'en est supprime. Un role se filtre, s'affiche en colonne dans le
tableau et sort dans la colonne *Role* du CSV. Le filtre garde aussi les
parents : filtrer « Serveur LAMP » au niveau serveur montre les hyperviseurs
qui font tourner une VM LAMP.

**Les vues.** La rentabilite s'ouvre sur la vue d'ensemble, en graphiques.
Chaque graphique bascule en tableau et s'exporte en CSV, filtres compris.
Les filtres (clusters, datacenters, clients, roles, texte, a
perte, avec alerte) valent pour l'arbre, le tableau et les graphiques ; ils
choisissent des cartes, ils ne recalculent pas leurs montants.

**Les tableaux de bord.** Chaque vue porte aussi ses colonnes et ses panneaux :

- *Tableau > Colonnes…* : choisir et ordonner les colonnes, niveau par niveau ;
  l'export « Exporter cette vue » suit ce choix.
- *Graphiques > Personnaliser* : ajouter, retirer, deplacer, elargir des
  panneaux. Les quatre figures d'origine sont des panneaux « prédéfinis » ;
  les panneaux libres — barres, chiffre clé, tableau — prennent une mesure
  (CA, PA, ROI, marge, coût OVH, VM, vCPU, RAM allouée ou libre, vCPU par
  thread...) a un niveau, regroupee ou non par cluster, serveur,
  datacenter, client ou rôle. Exemple pour anticiper un achat : barres,
  niveau VM, « RAM allouée (Go) », regroupée par cluster.

Un ratio (marge, RAM allouée %, vCPU par thread) se recalcule depuis ses
termes pour un groupe ou un total, il n'est jamais une moyenne de
pourcentages. La vue d'ensemble garde les quatre figures ; une vue
enregistrée rouvre ses panneaux et ses colonnes telles qu'elle les a
laissees, et le serveur controle leur forme champ par champ
(`db_vues.verifier_panneau`).

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

---

## 12. Mettre a jour

Le schema est versionne : `schema.sql` est la version 1, chaque evolution
une migration numerotee de `sql/migrations/` (voir son `LISEZMOI.md`).

```bash
cd /opt/inventaire-infra && git pull           # ou le nouvel installeur
sudo systemd-run --quiet --pty --wait --collect -p User=inventaire \
    -p WorkingDirectory=/opt/inventaire-infra \
    -p LoadCredentialEncrypted=inventory_db_password:/etc/inventaire-infra/creds/inventory_db_password.cred \
    python3 inventory_cli.py --migrer
sudo mariadb < sql/droits.sql
sudo systemctl restart inventaire-web.service
```

Avec `db.auto_schema: true`, la collecte applique aussi les migrations en
attente a chaque passage ; `--migrer` reste le geste explicite du
deploiement. Tant que la base est en retard sur le code, `/api/sante`
repond `degrade` (« schema en retard sur le code ») et le journal du
service donne les deux versions.

Avant une mise a jour qui porte une migration, une sauvegarde :
`mariadb-dump inventory > inventory-$(date +%F).sql`. Le DDL de MariaDB
n'est pas transactionnel : une migration interrompue ne se defait pas
seule, elle se relance (elle est ecrite pour), ou la base se restaure.
