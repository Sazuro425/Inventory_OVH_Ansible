# Inventaire d'infrastructure OVH → Proxmox → Ansible

Chaîne d'inventaire : les serveurs dédiés chez OVH, les VM et conteneurs des
hyperviseurs Proxmox installés dessus, le tout stocké en base et servi à
Ansible comme inventaire dynamique.

```
API OVH  ──>  dédiés (reverse, offre, datacenter, expiration)
   └──>  API Proxmox de chaque dédié  ──>  VM et LXC
            └──>  MariaDB (un snapshot par collecte)
                     └──>  inventaire dynamique Ansible
```

## Contenu

| Fichier | Rôle |
|---|---|
| `ovh_inventory.py` | dédiés, VPS, Public Cloud, Hosted Private Cloud |
| `proxmox_inventory.py` | VM et LXC d'un nœud PVE |
| `infra_inventory.py` | orchestre les deux, sort l'inventaire Ansible |
| `inventory_db.py` | couche MariaDB (snapshots) |
| `setup_inventory_db.py` | crée la base, le compte et les tables |
| `infra.yml` | configuration, sans aucun secret |
| `sql/schema.sql` | tables et vues |
| `sql/setup_db.sql` | variante SQL du setup, à lancer à la main |
| `pinned/` | certificats des nœuds PVE, épinglés |
| `exports/` | sorties CSV |

Chaque script tourne aussi seul : `ovh_inventory.py` et `proxmox_inventory.py`
ont leur propre ligne de commande et leurs sorties `table` / `csv` / `json`.

## Mise en route

### 1. Accès OVH

Créer un token sur <https://api.ovh.com/createToken/> avec au minimum
`GET /dedicated/server*` et `GET /vps*`, puis renseigner `~/.ovh.conf` :

```ini
[ovh-eu]
application_key=...
application_secret=...
consumer_key=...
```

L'endpoint `ovh-eu` pointe sur `api.ovh.com` et non `eu.api.ovh.com`, qui sert
un certificat auto-signé.

### 2. Accès Proxmox

Sur chaque nœud (ou un par cluster), un token en lecture seule :

```bash
pveum user add inventory@pve
pveum aclmod / --user inventory@pve --role PVEAuditor
pveum user token add inventory@pve readonly --privsep 0
```

`--privsep 0` est indispensable : sans lui le token n'hérite d'aucun droit et
toutes les routes répondent 403. Le token se met dans `PVE_TOKEN`, ou par hôte
dans la section `pve.tokens` de `infra.yml`.

### 3. Certificats

Proxmox signe ses certificats avec la CA interne du cluster, absente des
magasins publics. Deux options, aucune ne désactive la vérification :

- `pin_certs: true` (défaut) — le certificat de chaque nœud est épinglé dans
  `pinned/` à la première connexion, puis toute substitution est refusée.
  Vérifier l'empreinte sur le nœud avec `pvenode cert info`.
- `ca_bundle` — la CA du cluster, rapatriée par
  `python infra_inventory.py --collect-ca`.

### 4. Base de données

```bash
python setup_inventory_db.py --db-password '<mot-de-passe>'
export INVENTORY_DB_PASSWORD='<mot-de-passe>'
```

Le script est idempotent et ne supprime jamais de données. `--check` vérifie
l'état sans rien écrire. `--client` passe par le client `mariadb` quand le
compte administrateur utilise un plugin que PyMySQL ne gère pas — c'est le cas
sur MariaDB Windows, dont le plugin par défaut est `gssapi` ; le compte
applicatif est donc créé explicitement en `mysql_native_password`.

**Création automatique** (`db.auto_schema: true`, par défaut) : si la base ou
les tables manquent, les scripts les créent au premier passage. Cela suppose
que le compte applicatif ait les droits de structure sur sa base :

```sql
GRANT CREATE, CREATE VIEW, DROP, INDEX, REFERENCES, ALTER
  ON inventory.* TO 'inventory'@'localhost';
```

C'est un arbitrage : ces droits restent cantonnés à la base `inventory`, mais
ils permettent au compte de supprimer ses propres tables. Si la collecte tourne
sans surveillance sur un serveur partagé, préfère `auto_schema: false` et un
setup fait par un administrateur — le compte n'a alors que les quatre verbes de
manipulation de données.

## Usage courant

```bash
# collecte et enregistrement d'un snapshot
python infra_inventory.py --refresh --store-db

# lecture humaine
python infra_inventory.py --table

# inventaire Ansible, lu en base, sans toucher aux API
ansible-inventory -i infra_inventory.py --graph
python infra_inventory.py --from-db --list
```

### Éprouver la chaîne sur un parc généré

Avant de brancher la production, `generer_demo.py` fabrique un parc fictif et
l'écrit en base. Tout l'aval fonctionne ensuite normalement dessus.

```bash
python generer_demo.py --store-db --acces exports/demo_acces.json
python infra_inventory.py --from-db --table
python topologie.py --bastion-acces exports/demo_acces.json
python bastion_diff.py --acces exports/demo_acces.json
```

Le parc par défaut : 30 dédiés répartis en clusters et nœuds isolés, 4 à 16
invités par nœud, avec des machines arrêtées, des Windows, des invités sans IP
et quelques hyperviseurs injoignables. Le tirage est reproductible à `--seed`
constant.

**Les adresses sont prises dans les plages de documentation** — 198.51.100.0/24
et 203.0.113.0/24 (RFC 5737) pour le public, 10.x pour le privé. Elles ne sont
routables nulle part : un playbook lancé par erreur sur cet inventaire ne peut
atteindre aucune machine réelle. Avec des IP OVH plausibles, il aurait pu taper
chez quelqu'un d'autre. Le snapshot est également marqué « demo » dans
`collect_run.message`.

Mesures obtenues sur ce parc : 338 hôtes et 66 groupes dans l'inventaire
Ansible (227 Ko, lu en moins d'une seconde), 378 cartes dans le schéma rendues
en 16 ms, comparatif bastion en une seconde.

**Ce que ce test ne couvre pas** : la déduplication de cluster. Les données
générées sont déjà dans leur état d'après-déduplication — les invités y sont
rattachés au représentant de leur cluster. L'algorithme lui-même n'est éprouvé
que par le test sur données simulées, et ne le sera réellement qu'au premier
passage sur un vrai parc en clusters.

### Premier passage sur un parc complet

Avant de lancer la collecte sur trente hyperviseurs, l'éprouver sur deux ou
trois :

```bash
python infra_inventory.py --refresh --seulement 'ns1234*,ns5678*' --table
```

`--seulement` l'emporte sur `skip` et n'écrit rien en base tant que
`--store-db` n'est pas passé. Ce qu'il faut regarder dans la sortie :

- la ligne « N cluster(s) distinct(s), M hôte(s) membres d'un cluster déjà
  couvert » — c'est la déduplication, et c'est le point qui n'a jamais été
  éprouvé ailleurs que sur données simulées ;
- les hôtes injoignables, qui sont souvent des dédiés non-hyperviseurs à
  déclarer dans `skip` plutôt que de vrais problèmes.

Un premier passage avec `--no-detail` va beaucoup plus vite : il saute
l'interrogation de l'agent invité de chaque VM, donc deux appels par machine.
De quoi valider la découverte avant de collecter les IP et les systèmes.

En production, la collecte est planifiée à part (cron, timer systemd) et
Ansible lit la base avec `--from-db` : aucune interrogation d'API pendant les
playbooks.

## Service web et client d'administration

Le poste d'administration ne détient aucun accès aux API OVH ou Proxmox : il
récupère l'inventaire déjà collecté auprès du service web, avec un jeton à
usage unique pris sur la page après authentification.

### Service

```bash
python serveur_web.py                   # écoute 127.0.0.1:8080
python serveur_web.py --dev-user evan   # sans Apache devant, pour tester
```

| Route | |
|---|---|
| `POST /api/token` | émet un jeton à usage unique (identité OIDC requise) |
| `GET /api/inventaire` | rend l'inventaire Ansible, consomme le jeton |
| `GET /api/sante` | état du service, sans authentification |

Il se place **derrière Apache**, qui porte l'authentification. Le vhost fourni
contient déjà le nécessaire ; deux points y méritent attention.

**Les deux routes n'ont pas la même politique.** `/api/token` exige l'OIDC :
c'est un humain, dans un navigateur, qui demande un jeton. `/api/inventaire`
en est volontairement exempte, parce que le client n'est pas un navigateur et
ne peut pas suivre une redirection OIDC — c'est le jeton porté par
`Authorization` que le service valide lui-même, et il ne sert qu'une fois.

**L'en-tête d'identité est effacé avant d'être reposé.**

```apache
RequestHeader unset X-Remote-User        # partout
<Location /api/token>
    AuthType openid-connect
    Require valid-user
    RequestHeader set X-Remote-User %{REMOTE_USER}s
</Location>
```

Sans le `unset`, n'importe qui enverrait cet en-tête et s'annoncerait comme
l'administrateur de son choix. Il n'est reposé que là où l'authentification a
réellement eu lieu. Et il n'est digne de confiance **que** parce que le service
n'écoute que la boucle locale. Exposé sur `0.0.0.0`, n'importe qui pourrait le forger et
s'émettre des jetons. Le mode `--dev-user` refuse d'ailleurs de démarrer sur
une autre interface.

Les jetons ne sont **jamais stockés en clair** : la base ne garde que leur
empreinte SHA-256, l'émetteur, les dates d'émission et de consommation, et
l'adresse qui les a utilisés. Une lecture de la base ne permet donc pas de s'en
servir, et un jeton rejoué est refusé en indiquant la date du premier usage —
ce qui distingue une erreur de manipulation d'une interception.

La journalisation est nominative : le format `nominatif` du vhost emploie `%u`,
qui reçoit l'identité OIDC via `OIDCRemoteUserClaim`. Sans cela les journaux ne
contiennent que des adresses, ce qui ne dit pas grand-chose sur qui a consulté
la cartographie.

### Obtenir un jeton

Le bouton **Jeton** de la page appelle `/api/token` sur la même origine — le
cookie de session OIDC part avec la requête — et affiche la commande prête à
coller :

```
infra update --token zEDsyJ9z_Ln4UmJLw-…
```

Validé de bout en bout : jeton pris dans la page, consommé par le client
(338 hôtes récupérés), puis refusé au rejeu avec la date du premier usage. La
base garde l'émetteur, les dates et l'adresse qui a consommé.

Si la page est ouverte sans son service derrière — un fichier local, par
exemple — le bouton le dit au lieu d'échouer en silence.

### Client

```bash
infra update --token <jeton>    récupère l'inventaire et le met en cache
infra list [motif]              ce que contient le cache
infra list --groupes            les groupes disponibles
infra simulate <playbook>       répétition à blanc (--check --diff)
infra run <playbook>            simulation, confirmation, puis exécution
```

`run` enchaîne toujours une simulation d'abord, et **n'exécute rien si elle
échoue**. La confirmation est demandée au clavier ; sans terminal, il faut
`--oui` explicitement plutôt que de laisser passer silencieusement.

Le cache signale l'âge de la **collecte**, pas celui du téléchargement : un
inventaire récupéré il y a cinq minutes peut dater de la veille, et c'est cette
ancienneté-là qui fait manquer des machines. Au-delà de `--ttl` heures, un
avertissement s'affiche sans bloquer.

L'inventaire est écrit pour Ansible au **format natif** (`all.hosts` pour les
variables, `all.children` pour les groupes), et non au format de sortie d'un
script d'inventaire. Passé tel quel à `-i`, ce dernier échoue : Ansible essaie
de l'exécuter comme un script, puis de le lire en YAML, puis en INI. Le fichier
est du JSON, qui est un sous-ensemble de YAML — le client n'a donc pas besoin
de PyYAML.

Validation faite avec Ansible 2.14 : `simulate` affiche les différences sans
rien écrire (aucun fichier créé), `run` enchaîne `--check --diff` puis
l'exécution réelle, et les fichiers apparaissent alors avec les droits
attendus. Sans terminal et sans `--oui`, `run` s'arrête avant d'appliquer.

Un playbook de validation est fourni dans `exemples/playbook_test.yml` : il
tourne en `connection: local`, donc il n'a besoin d'aucune machine joignable.

Sur `simulate` : `--check` d'Ansible ne déroule pas la résolution de paquets.
Pour savoir ce qu'une mise à jour supprimerait, il faut une tâche de simulation
du gestionnaire lui-même (`apt-get -s dist-upgrade`) — le client le rappelle à
chaque lancement.

## Accès par The Bastion

Les connexions passent par [The Bastion](https://github.com/ovh/the-bastion)
avec [the-bastion-ansible-wrapper](https://github.com/ovh/the-bastion-ansible-wrapper),
qui remplace le client SSH d'Ansible.

Dans le dépôt de playbooks :

```bash
git submodule add https://github.com/ovh/the-bastion-ansible-wrapper.git extra/bastion
chmod +x extra/bastion/sshwrapper.py extra/bastion/sftpbastion.sh
cp <ce-projet>/deploy/ansible.cfg.example ./ansible.cfg
```

Chaque opérateur déclare **son** compte :

```bash
export BASTION_USER=evan
export BASTION_HOST=bastion.exemple.net
export BASTION_PORT=22
```

### Pourquoi `bastion_user` n'est pas dans l'inventaire

Le wrapper résout sa configuration dans cet ordre : `environment` du playbook,
puis fichier de configuration, puis **inventaire**, puis variables
d'environnement du système. L'inventaire passe donc **avant** l'environnement.

Y écrire un `bastion_user` imposerait le même compte à tout le monde et
réduirait à néant la traçabilité nominative du bastion : les enregistrements de
session porteraient un compte générique au lieu de celui qui a agi. L'inventaire
ne pose donc que `bastion_host` et `bastion_port`, communs à tous, et laisse
chacun fournir son identité. Activer avec `ansible.bastion.actif: true` dans
`infra.yml`.

Les comptes **cibles** (sur les machines, pas sur le bastion) viennent de
`ansible.comptes` : `root` sur les hyperviseurs, `ansible` sur les invités par
défaut.

### Comparatif inventaire / bastion

Les deux listes divergent avec le temps, dans les deux sens, et chacune a son
coût :

- une machine du parc **sans accès déclaré** est injoignable — on s'en aperçoit
  pendant une intervention, au pire moment ;
- un accès qui **subsiste pour une machine disparue** est une entrée morte. Si
  l'IP est réattribuée, l'accès suit la nouvelle machine.

```bash
bssh --osh selfListAccesses --json-greppable > acces.json
python bastion_diff.py --acces acces.json
```

Ou directement, si le client bastion est sur le poste :

```bash
python bastion_diff.py
```

Le rapport liste les manquants, les orphelins, et donne les commandes
`groupAddServer` / `groupDelServer` correspondantes.

**En version graphique**, la même comparaison se superpose au schéma :

```bash
python topologie.py --bastion-acces acces.json
```

Les machines sans accès déclaré prennent un contour ambré et la mention
« sans accès » ; les accès orphelins, qui ne correspondent à aucune machine,
apparaissent dans une branche **ORPHELINS** à part — ils n'ont nulle part où se
rattacher dans un arbre du parc. Un filtre *Bastion* permet de masquer ce qui
va bien pour ne garder que la dérive, et une statistique « Sans accès » s'ajoute
à l'en-tête.

Sans `--bastion-acces`, aucune de ces marques n'apparaît : une page générée sans
la couche d'accès ne laisse pas croire que tout est à déclarer. Il ne modifie rien : il
constate et propose. Le code de sortie vaut 1 en cas de dérive, ce qui permet
d'en faire un contrôle périodique.

Deux précautions prises :

- **Les plages sont comprises.** Un accès `10.10.20.0/24` couvre bien
  `10.10.20.10` ; une comparaison par égalité de chaînes aurait déclaré tout le
  parc manquant dès que tu déclares des sous-réseaux. Les jokers `*` et les
  motifs sur le compte sont gérés de même.
- **Une plage n'est orpheline que si plus aucune machine ne s'y trouve.** Sinon
  chaque sous-réseau serait signalé à tort.

**Trois états, pas deux.** Une machine qui n'a pas vocation à passer par le
bastion n'est pas un oubli : la règle `bastion.hors_perimetre` d'`infra.yml` la
classe « hors périmètre », comptée à part et jamais présentée comme un problème.
Confondre « oublié » et « hors sujet » suffit à rendre le rapport illisible, et
un rapport de dérive qu'on n'ouvre plus ne sert à rien.

La règle accepte des motifs sur l'OS (`win*`), des étiquettes Proxmox
(`no-bastion`) et des hôtes en dur. Le motif sur l'OS couvre les cas massifs
sans rien saisir ; l'étiquette traite les exceptions que la règle manquerait.
Le périmètre est évalué **avant** l'IP : une machine hors périmètre le reste,
qu'on connaisse son adresse ou non.

Les invités sans IP connue sont comptés à part : l'agent QEMU est absent ou la
machine est arrêtée, donc leur accès ne peut être ni vérifié ni déclaré.

La lecture de la réponse osh accepte `--json-greppable`, `--json` et un JSON
nu. La structure exacte de `value` dépend de la version du bastion ; le parcours
est volontairement tolérant, mais si rien n'est reconnu le message te demande
la sortie réelle plutôt que de rendre un rapport faux.

### Qui a le droit de passer

Dans The Bastion, l'accès se porte par **groupe** : un groupe contient des
serveurs, et des comptes en sont membres. Seuls ces membres peuvent atteindre
ces serveurs. Déclarer trente hyperviseurs et leurs VM à la main invite à
l'oubli, donc l'inventaire génère les commandes :

```bash
python infra_inventory.py --from-db --bastion-commands
```

Ce qui produit, à jouer depuis ton compte bastion :

```
bssh --osh groupAddServer --group infra-prod --host 151.80.18.180 --user root --port 22 --comment 'ns375008'
bssh --osh groupAddServer --group infra-prod --host 10.10.20.10 --user ansible --port 22 --comment 'atmons (106)'
```

Le groupe se crée une fois (`groupCreate`), et c'est `groupAddMember` qui décide
qui peut passer — ce qui reste une décision humaine, volontairement hors du
script. Les invités sans IP connue sont comptés et signalés plutôt qu'ignorés
en silence.

Relancer la commande après une collecte donne l'état à jour ; `groupDelServer`
retire ce qui a disparu.

## Schéma de l'infrastructure

`topologie.py` produit une vue en arbre, façon topologie réseau :
**datacenter → serveur dédié → hyperviseur → invité**.

```bash
python topologie.py                       # depuis le dernier snapshot en base
python topologie.py --refresh             # collecte d'abord
python topologie.py --out /var/www/html/infra.html
```

La page est **autonome et interactive** : aucune dépendance, aucun CDN, aucun
appel réseau au rendu. Elle peut donc être servie telle quelle depuis un
serveur web, ou ouverte en local.

| Interaction | |
|---|---|
| Glisser une carte | la déplace ; elle garde sa place |
| Glisser le fond | déplace la vue |
| Molette | zoom centré sur le pointeur |
| Clic sur une carte | fiche de détails complète |
| Bouton `–` / `+n` | replie ou déplie une branche |
| Filtres | datacenter, cluster, type, état |
| Recherche (`/`) | nom, IP, VMID, OS, étiquettes |
| Ajuster / Réinitialiser | recadre, ou remet tout à zéro |
| ◐ | thème sombre (défaut) ou clair |

Déplacements, replis, filtres, zoom et thème sont mémorisés dans le navigateur
(`localStorage`), par visiteur : la page peut être servie à plusieurs personnes,
chacune garde sa disposition. Le bouton Réinitialiser efface tout.

Chaque carte porte le matériel utile à ce niveau, et la bande de couleur donne
l'état (vert en marche, gris arrêté, rouge injoignable).

**Les totaux de l'en-tête suivent les filtres.** Filtrer sur un datacenter
donne immédiatement ce qu'il représente :

```
10 / 30 DÉDIÉS   113 / 308 INVITÉS   420 / 1 135 vCPU   750 Go / 1 902 Go RAM
```

Le total d'origine reste affiché à côté, les cartes prennent une bordure
d'accent et le sous-titre indique « vue filtrée » : sans cela, on croirait
regarder tout le parc. C'est ce qui permet de répondre à « combien de vCPU pour
le client ABC » ou « combien de RAM engagée à rbx6 » sans requête SQL.

Le **pliage n'entre pas dans le calcul** : replier une branche est un confort
d'affichage, pas un filtre. Les totaux ne bougent donc pas quand on ouvre ou
ferme un hyperviseur, ce qui serait déroutant.

**À l'échelle** : mesuré à 423 cartes (30 hyperviseurs, 12 invités chacun), le
rendu prend 21 ms. Au-delà de 60 invités, la page s'ouvre repliée au niveau des
hyperviseurs, sinon elle ferait plusieurs milliers de pixels de haut ; et
l'ajustement ne descend jamais sous un zoom lisible.

Le gabarit (HTML, CSS et JS) est dans `assets/topologie.tmpl.html` ; `topologie.py`
n'y injecte que les données. Pour changer l'allure, c'est le gabarit qu'il faut
modifier, pas le script.

Régénérer le schéma après chaque collecte suffit à le garder à jour ; ajoute la
commande à la fin de `deploy/collecte.sh` pour qu'il se rafraîchisse tout seul.

## Collecte planifiée

La collecte tourne à part et alimente la base ; Ansible lit ensuite la base
avec `--from-db`, sans jamais interroger les API pendant un playbook.

Les secrets vivent dans un fichier d'environnement, jamais dans la définition
de la tâche ni sur la ligne de commande — un `ps aux` ou un `systemctl show`
les exposerait.

### Sur la VM Linux : l'installeur

```bash
sudo ./deploy/installer.sh --simulation    # montre ce qui serait fait
sudo ./deploy/installer.sh                 # installe
```

Il crée le compte de service, dépose le code dans `/opt/inventaire-infra`,
installe les unités systemd et le vhost Apache, prépare `/var/www/inventaire`,
puis affiche ce qu'il reste à faire à la main.

Il est **idempotent** — on le rejoue pour mettre à jour le code — et il ne
remplace jamais un fichier déjà renseigné : ni `/etc/inventaire-infra.env`, ni
`infra.yml`, ni les certificats épinglés. Ce qu'il remplace, il le sauvegarde
d'abord avec un horodatage.

Il ne touche pas à la base : le schéma se crée au premier passage de la
collecte, et le compte applicatif par `setup_inventory_db.py`, qui demande les
accès administrateur.

Options : `--sans-apache`, `--sans-web`, `--sans-timer`, `--prefixe <chemin>`.

**Fins de ligne** : les scripts doivent être en LF. Un `#!/usr/bin/env bash`
donne `$'': command not found`, et une unité systemd en CRLF se comporte mal.
Le `.gitattributes` impose `eol=lf` ; attention si tu édites depuis Windows,
car l'écriture de fichiers texte y ajoute des CRLF par défaut.

Contrôle :

```bash
systemctl list-timers inventaire-collecte.timer
sudo systemctl start inventaire-collecte.service   # collecte immédiate
journalctl -u inventaire-collecte -n 30
```

Le service est en `Type=oneshot` et durci (`ProtectSystem=strict`,
`NoNewPrivileges`, `PrivateTmp`). Le timer est horaire avec
`RandomizedDelaySec=5min`, pour que trente interrogations d'API ne partent pas
toutes à l'heure ronde, et `Persistent=true` pour rattraper un passage manqué.
`collecte.sh` prend un verrou `flock` : une collecte qui traîne n'en croise
jamais une autre.

### Sur Windows (tâche planifiée)

```powershell
copy deploy\inventaire.env.example $env:USERPROFILE\.inventaire-infra.env
notepad $env:USERPROFILE\.inventaire-infra.env    # renseigner les secrets
.\deploy\planifier.ps1 -Minutes 60
```

Pas besoin de droits administrateur : la tâche est enregistrée pour
l'utilisateur courant. `planifier.ps1 -Supprimer` la retire.

```powershell
Start-ScheduledTask -TaskName 'Inventaire OVH-Proxmox'
Get-ScheduledTaskInfo -TaskName 'Inventaire OVH-Proxmox'
Get-Content "$env:USERPROFILE\.inventaire-infra.log" -Tail 20
```

Deux pièges rencontrés, déjà corrigés dans les scripts : la tâche doit lancer
`pwsh.exe` quand il est présent, et `$ErrorActionPreference = 'Stop'` transforme
chaque ligne que Python écrit sur stderr en exception fatale — d'où un retour
en `Continue` le temps de l'appel.

### Hébergement de la page

La page est un fichier statique : n'importe quel serveur web suffit. Les deux
configurations sont fournies, `deploy/apache-inventaire.conf` et
`deploy/nginx-inventaire.conf`.

```bash
sudo mkdir -p /var/www/inventaire
sudo chown inventaire:www-data /var/www/inventaire
sudo chmod 750 /var/www/inventaire
```

**Apache**

```bash
sudo a2enmod headers
sudo cp deploy/apache-inventaire.conf /etc/apache2/sites-available/inventaire.conf
sudo htpasswd -c /etc/apache2/.htpasswd-inventaire evan
sudo a2ensite inventaire
sudo apache2ctl configtest && sudo systemctl reload apache2
```

**nginx**

```bash
sudo cp deploy/nginx-inventaire.conf /etc/nginx/sites-available/inventaire
sudo ln -s /etc/nginx/sites-available/inventaire /etc/nginx/sites-enabled/
sudo htpasswd -c /etc/nginx/.htpasswd-inventaire evan
sudo nginx -t && sudo systemctl reload nginx
```

### Authentification par Authentik (OIDC)

L'authentification basic est le point de départ ; pour brancher Authentik,
Apache passe par `mod_auth_openidc`.

```bash
sudo apt install libapache2-mod-auth-openidc
sudo a2enmod auth_openidc
sudo mkdir -p /etc/apache2/conf-secrets
sudo cp deploy/inventaire-oidc.conf.example         /etc/apache2/conf-secrets/inventaire-oidc.conf
sudo chown root:www-data /etc/apache2/conf-secrets/inventaire-oidc.conf
sudo chmod 640 /etc/apache2/conf-secrets/inventaire-oidc.conf
```

Côté Authentik : un **Provider OAuth2/OpenID** en client confidentiel, avec
comme Redirect URI exactement l'`OIDCRedirectURI` du fichier
(`https://…/oidc-callback`), puis une **Application** dont le slug sert dans
l'URL des métadonnées
(`/application/o/<slug>/.well-known/openid-configuration`).

Reste à remplir `OIDCClientID`, `OIDCClientSecret` et une `OIDCCryptoPassphrase`
(`openssl rand -base64 32`), puis à remplacer dans le vhost le bloc `AuthType
Basic` par :

```apache
AuthType openid-connect
Require valid-user
```

Pour restreindre à un groupe plutôt qu'à tout compte Authentik, ajouter le
scope `groups` au provider et utiliser `Require claim groups:infra-admins`.

Trois points qui coûtent du temps si on les découvre en route :

- **HTTPS est obligatoire en pratique.** Les cookies de session du module sont
  posés en `Secure`, et Authentik refuse une `redirect_uri` en clair hors
  localhost. Passe par certbot ou une CA interne avant d'activer l'OIDC.
- **L'URL de callback ne doit correspondre à aucun fichier.** Apache
  l'intercepte au moment de l'authentification ; c'est normal qu'elle n'existe
  pas sur le disque.
- **Le secret client reste hors du dépôt.** Le vhost l'inclut par
  `IncludeOptional`, donc Apache démarre même sans le fichier — on retombe
  simplement sur l'authentification basic.

Alternative si tu préfères ne rien configurer dans Apache : Authentik sait aussi
faire *Proxy Provider* en forward auth, la page restant servie en local sans
authentification propre. L'OIDC a l'avantage de rester indépendant d'un
reverse proxy devant.

La publication est déclenchée par la collecte elle-même : le service définit
`INVENTAIRE_WEB=/var/www/inventaire/index.html`, et `collecte.sh` régénère la
page juste après avoir écrit le snapshot. Commente cette variable pour ne rien
publier.

Trois points de conception :

- **Écriture atomique.** La page est écrite à côté puis renommée. Un visiteur
  qui charge la page pendant une collecte voit l'ancienne version ou la
  nouvelle, jamais une page tronquée.
- **Un échec de publication ne fait pas échouer la collecte.** Les données sont
  en base, c'est ce qui compte ; le journal signale la page non régénérée.
- **La page n'est pas publique.** Elle cartographie ton infrastructure :
  serveurs, IP, VM, systèmes. La configuration fournie impose une
  authentification, et contient en commentaire une restriction par adresse IP,
  plus solide qu'un mot de passe si tes clients sont sur un réseau connu.

La page ne fait aucun appel réseau, ce qui permet une CSP stricte
(`default-src 'none'`). Le cache est désactivé sur `index.html`, sinon un
navigateur servirait un inventaire périmé sans prévenir.

### Secrets chiffrés (systemd-creds)

Un fichier d'environnement a deux défauts : il est lisible en clair par root et
par toute sauvegarde du système, et surtout les secrets finissent dans
**l'environnement du processus** — donc dans `/proc/<pid>/environ` et dans un
éventuel vidage mémoire.

```bash
sudo ./deploy/chiffrer-secrets.sh
```

Le script lit `/etc/inventaire-infra.env`, chiffre chaque variable séparément
dans `/etc/inventaire-infra/creds/`, et déplace l'original en `.avant-chiffrement`
plutôt que de le supprimer — le temps de vérifier que les services démarrent.

Il reste à activer le chargement dans les unités : commenter `EnvironmentFile=`
et décommenter les lignes `LoadCredentialEncrypted=`, déjà présentes.

systemd dépose alors les secrets dans un tmpfs que **seul ce service** peut
lire. Les scripts les y cherchent avant l'environnement, qui reste accepté en
repli pour les lancements manuels.

Avec un TPM, la clé ne quitte pas la puce et les fichiers chiffrés sont
inutilisables sur une autre machine ; sans TPM, systemd se rabat sur une clé
hôte dans `/var/lib/systemd/`. Dans les deux cas, ce qui change réellement :
une sauvegarde, un snapshot ou un disque récupéré ne contiennent plus rien
d'exploitable. Contre un attaquant déjà root sur la machine, rien ne protège —
le service doit bien obtenir le secret en clair à un moment.

Dans `infra.yml`, les références acceptent les deux formes : `env:NOM` pour une
variable, `cred:nom` pour un secret systemd.

### Rétention

`db.keep_runs` dans `infra.yml` (200 par défaut) limite le nombre de snapshots
conservés ; la purge s'exécute à la fin de chaque collecte et les suppressions
suivent les clés étrangères en cascade.

## Groupes Ansible générés

`pve_nodes`, `pve_guests`, `type_qemu` / `type_lxc`, `status_running` /
`status_stopped`, `cluster_<nom>`, `node_<nom>`, `dc_<datacenter>`,
`tag_<tag>` (depuis les tags Proxmox), plus deux groupes de contrôle :
`guests_sans_ip` (agent invité absent) et `pve_unreachable` (dédié qui ne
répond pas sur 8006 — en panne, ou pas un hyperviseur, à mettre dans
`pve.skip`).

### Variables matérielles

Les hyperviseurs portent deux jeux de variables, volontairement séparés :

- `ovh_*` — ce qu'OVH facture : `ovh_cpu_model`, `ovh_cpu_cores`,
  `ovh_cpu_threads`, `ovh_ram_mb`, `ovh_disk_count`, `ovh_disks`
- `hw_*` — ce que l'hyperviseur voit vraiment : `hw_cpu_model`,
  `hw_cpu_sockets`, `hw_cpu_cores`, `hw_cpu_logical`, `hw_cpu_mhz`,
  `hw_ram_total_mb`, `hw_ram_used_mb`, `hw_disk_count`, `hw_disk_total_gb`,
  `hw_disk_desc`, `hw_pve_version`, `hw_kernel`

Les deux ne coïncident jamais tout à fait (RAM réservée par le firmware,
capacité disque annoncée en Go décimaux) et l'écart est en soi une information :
une divergence franche signale un remplacement de pièce ou une barrette morte.

Les VM reçoivent `host_cpu_model`, `host_cpu_cores` et `host_disks`, le
matériel de la machine physique qui les porte.

Chaque VM porte ses variables `pve_*` **et** les `ovh_*` du serveur qui
l'héberge : c'est ce croisement qui permet de cibler, par exemple, toutes les
VM d'un datacenter ou d'un dédié qui arrive à expiration.

## Points d'attention

- **Cache** : Ansible relance l'inventaire à chaque playbook. Le cache
  (`cache.ttl`, 900 s) et le mode `--from-db` évitent de refaire le tour des
  API à chaque commande.
- **Clusters** : les membres d'un même cluster ne sont interrogés qu'une fois,
  `/cluster/resources` renvoyant déjà tout le cluster.
- **Secrets** : `infra.yml` ne contient aucun mot de passe, uniquement des
  références `env:NOM`.
- **Tolérance** : un nœud injoignable ou un droit manquant sur une route
  n'interrompt pas la collecte ; des identifiants invalides, si.
