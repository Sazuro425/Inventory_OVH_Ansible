# Inventaire OVH → Proxmox → Ansible

Inventaire et planification de capacité pour un parc de serveurs dédiés OVH
qui portent des hyperviseurs Proxmox.

La collecte interroge l'API OVH (les dédiés, leur prix et leurs options), puis
l'API Proxmox de chaque hyperviseur (le matériel, les VM, les conteneurs). Elle
range une photo complète du parc en base, à chaque passage. De cette base
sortent trois choses :

- **une page web** : la rentabilité et la capacité, du cluster jusqu'à la VM,
  le schéma du parc, le dépôt des jetons Proxmox, la gestion des comptes ;
- **un inventaire dynamique Ansible**, lu en base, sans toucher aux API
  pendant un playbook ;
- **un comparatif avec The Bastion** : ce qui est dans le parc sans accès
  déclaré, et les accès qui ne mènent plus à rien.

La question de fond est celle de la capacité : Zabbix et Grafana mesurent ce
que les VM consomment, pas ce qui leur a été promis. Quand 180 Go de RAM sont
alloués sur un serveur qui en a 192, il est temps d'en commander un autre,
même si la RAM consommée est à 40 %. L'inventaire suit ces deux chiffres,
alloué et physique, et projette la date à laquelle la RAM sera épuisée.

---

## Sommaire

- [Ce que fait l'application](#ce-que-fait-lapplication)
- [Démarrage rapide](#démarrage-rapide)
- [Mettre à jour](#mettre-à-jour)
- [Architecture](#architecture)
- [Sécurité](#sécurité)
- [La collecte](#la-collecte)
- [Configuration : infra.yml](#configuration--infrayml)
- [Inventaire Ansible et client](#inventaire-ansible-et-client)
- [The Bastion](#the-bastion)
- [Administration courante](#administration-courante)
- [Développement](#développement)
- [Dépannage](#dépannage)
- [Documentation](#documentation)

---

## Ce que fait l'application

La page s'ouvre sur quatre onglets. Les deux derniers ne sont visibles que des
administrateurs.

### Rentabilité (onglet par défaut)

L'arbre du parc : **cluster → serveur → VM → éléments déclarés**. Chaque
carte cumule ce qu'il y a en dessous.

| Colonne | Ce qu'elle dit |
|---|---|
| PA / mois | ce que la carte coûte : le prix OVH **options comprises**, plus les achats saisis à la main |
| CA / mois | ce qu'elle rapporte : les ventes saisies sur ses VM et ses éléments |
| ROI / mois | CA − PA |
| Marge | ROI / CA |
| Capacité | `RAM 70 / 125 Go (56 %) · vCPU 23 / 24 (0,96×)` sur un serveur ou un cluster, `8 Go · 5 vCPU` sur une VM |

Le bouton **Colonnes…** retire, ajoute et ordonne les colonnes. Le catalogue
en compte vingt-cinq : Coût OVH, nombre de VM, RAM allouée et physique,
vCPU, threads, rôle, offre, datacenter, nœud, client, contrat, corrections,
alertes…

Trois présentations :

- **Arbre** : la hiérarchie, dépliable ;
- **Tableau** : une ligne par carte d'un niveau, triable, exportable en CSV ;
- **Graphiques** : un tableau de bord configurable (résultat par cluster,
  origine du chiffre, capacité, clients, courbes dans le temps, projection de
  capacité).

Une présentation avec ses filtres et ses colonnes s'enregistre comme **vue**,
privée ou partagée.

**Ce qui se saisit** (rôle commercial) :

- une vente sur une VM (contrat, client, prix) ;
- des **enfants** sur une VM, un par un ou par lot (4 sites à 40 € sur un
  serveur LAMP) ;
- un rôle sur une carte (serveur web, LAMP, proxy, applicatif…), pris dans un
  catalogue qui s'étend depuis la page ;
- une correction d'un prix collecté, avec son motif.

Chaque saisie entre dans un **journal chaîné** que personne ne peut réécrire
sans que ça se voie (voir [Sécurité](#sécurité)).

**L'historique et la projection.** La collecte inscrit chaque jour un point
par carte : PA, CA, ROI, RAM et vCPU alloués et physiques. La projection
trace la tendance de la RAM allouée sur 90 jours (moindres carrés) et donne la
date à laquelle chaque cluster passera 85 % puis 100 % de sa RAM physique.
Elle demande au moins 5 points étalés sur 14 jours et ne s'avance pas au-delà
de 5 ans.

### Schéma

Le parc dessiné : **datacenter → serveur dédié → hyperviseur → invité**.

- La carte d'un serveur affiche en gros son reverse, ou le nom du nœud
  Proxmox s'il n'a pas de reverse personnalisé. Le nom OVH complet est
  affiché dessous.
- Un clic ouvre la fiche, avec le coût OVH détaillé (offre et options), les
  saisies et l'état du jeton Proxmox.
- La page propose des filtres (datacenter, cluster, type, état) et une
  recherche (nom, IP, VMID, OS, étiquettes). Les totaux de l'en-tête suivent
  les filtres.
- Les positions, les replis et le zoom sont mémorisés par navigateur.

### Jetons PVE (admins)

Un tableau des hyperviseurs : cluster, état, jeton en service et son
échéance, motif d'un refus ou d'une panne en clair. Le dépôt d'un jeton se
fait ici, sans SSH :

1. le formulaire donne les trois commandes `pveum` à lancer sur un nœud du
   cluster, échéance comprise ;
2. tu colles la valeur du jeton, telle quelle ou avec la ligne du tableau de
   `pveum` autour ;
3. le service la **scelle** aussitôt. À partir de là, il ne peut plus la
   relire.

La collecte suivante ouvre le dépôt, teste le jeton sur l'hyperviseur, puis le
range chiffré ou le refuse avec son motif. Un jeton vaut pour tout son cluster.

### Comptes (admins, mode comptes locaux)

Créer un compte (identifiant, rôles), générer un nouveau mot de passe, changer
les rôles, désactiver ou réactiver.

- Le mot de passe généré ne s'affiche qu'une fois, et la personne le change à
  sa première connexion.
- Un admin ne peut ni se désactiver ni se retirer le rôle admin. Il reste
  toujours au moins un admin actif.

| Rôle | Ce qu'il permet |
|---|---|
| lecture | tout voir, rien saisir |
| commercial | saisir ventes, enfants, rôles, corrections |
| admin | en plus : jetons PVE, comptes, intégrité du journal, jeton Ansible |

---

## Démarrage rapide

Cible : une VM **Debian 12** (MariaDB 10.11, Python 3.11, systemd 252),
joignable en HTTPS, qui sort vers `api.ovh.com` et joint chaque hyperviseur
sur le port 8006.

```bash
git clone https://github.com/Sazuro425/Inventory_OVH_Ansible.git
cd Inventory_OVH_Ansible
sudo bash deploy/preprod.sh --nom inventaire.exemple.lan
```

Le script monte tout, en une fois et sans jamais écrire de secret en clair :

- les paquets ;
- le code dans `/opt/inventaire-infra` ;
- la base et ses deux comptes, avec des mots de passe tirés au hasard et
  chiffrés aussitôt ;
- le schéma et les migrations ;
- la clé maîtresse et la boîte de dépôt des jetons ;
- un certificat (auto-signé, ou le tien avec `--cert` et `--cle`) ;
- Apache et le service web ;
- le compte `admin`, dont le mot de passe s'affiche **une seule fois**.

Ensuite :

```bash
cd /opt/inventaire-infra
sudo bash deploy/poser-secret.sh ovh              # les trois clés OVH, testées avant d'être posées
sudo systemctl start inventaire-collecte.service  # première collecte
sudo systemctl enable --now inventaire-collecte.timer
```

Puis tu déposes un jeton Proxmox par cluster depuis l'onglet **Jetons PVE**,
et tu relances une collecte.

**La procédure complète, pas à pas, avec ce que tu dois voir à chaque étape
et quoi faire sinon : [docs/preprod-pas-a-pas.md](docs/preprod-pas-a-pas.md).**

### Les droits à donner aux clés OVH

Sur <https://api.ovh.com/createToken/> (pas `eu.api.ovh.com`, dont le
certificat est auto-signé), quatre lignes, **toutes en GET** :

| Méthode | Chemin | Pour |
|---|---|---|
| GET | `/me` | vérifier la signature |
| GET | `/dedicated/server*` | les dédiés, leur matériel |
| GET | `/services*` | le prix de chaque serveur **et de ses options** |
| GET | `/vps*` | les VPS |

---

## Mettre à jour

```bash
cd ~/Inventory_OVH_Ansible && git pull
sudo bash deploy/preprod.sh --nom inventaire.exemple.lan
```

Le script est rejouable. Il réinstalle le code, applique les migrations de la
base et rejoue les droits SQL. Il refait aussi la page avec le nouveau code, à
partir du dernier relevé. Il ne touche à aucun secret, et un deuxième passage
ne change aucun mot de passe.

Ce qui demande une nouvelle collecte pour apparaître (de nouvelles données
OVH ou Proxmox, comme le détail des options) apparaît à la collecte suivante,
au plus une heure plus tard, ou tout de suite avec
`sudo systemctl start inventaire-collecte.service`.

---

## Architecture

```
                      ┌────────────────────── VM Debian ──────────────────────┐
 API OVH ─────────┐   │                                                        │
 (dédiés, prix,   ├──►│  collecte (timer horaire, compte système inventaire)   │
  options)        │   │   infra_inventory.py ── secrets : systemd-creds        │
 API Proxmox ─────┘   │          │  compte SQL « inventory »                   │
 (8006, par cluster)  │          ▼                                             │
                      │       MariaDB  ◄── compte SQL « inventory_web »        │
                      │          │         (sans accès aux secrets)            │
                      │          ▼                                             │
                      │   serveur_web.py (127.0.0.1:8080) ◄── Apache (HTTPS) ◄─┼── navigateur
                      │          │                                             │
                      │          └── /api/inventaire ──────────────────────────┼── client Ansible
                      └────────────────────────────────────────────────────────┘
```

**Deux processus, deux comptes de base, deux jeux de secrets.**

| | Collecte | Service web |
|---|---|---|
| Lancée par | `inventaire-collecte.timer`, toutes les heures | `inventaire-web.service`, en permanence |
| Exposée | non | oui, derrière Apache |
| Compte SQL | `inventory` : lecture, écriture, schéma ; `DELETE` sur deux tables seulement | `inventory_web` : droits table par table, rien sur `secret_store` |
| Secrets | clés OVH, mot de passe SQL, clé maîtresse | son mot de passe SQL, rien d'autre |

Le service web est la partie joignable depuis le réseau. Il ne voit donc ni
les clés OVH, ni les jetons Proxmox, ni la clé maîtresse. Compromis, il
n'obtiendrait que les jetons déposés **après** la compromission.

### Les fichiers

**Collecte et données**

| Fichier | Rôle |
|---|---|
| `infra_inventory.py` | orchestre la collecte, écrit le snapshot, sort l'inventaire Ansible |
| `ovh_inventory.py` | API OVH : dédiés, VPS, prix et options (`/services`) |
| `proxmox_inventory.py` | API Proxmox, épinglage des certificats |
| `inventory_db.py` | façade de la couche base |
| `db_base.py` | connexion, schéma, migrations, journal chaîné |
| `db_snapshots.py` | snapshots, purge, historique |
| `db_saisies.py` | ventes, éléments, rôles, corrections |
| `db_vues.py` | vues enregistrées, tableaux de bord |
| `rentabilite.py` | l'arbre de rentabilité et de capacité, l'export CSV |
| `capacite.py` | historique et projection de capacité |
| `generer_demo.py` | un parc fictif (adresses RFC 5737), pour éprouver la chaîne |

**Service web et page**

| Fichier | Rôle |
|---|---|
| `serveur_web.py` | le service : routes déclarées par décorateur, contrôle d'accès, API |
| `auth_locale.py` | comptes locaux : scrypt, sessions, blocage, gestion depuis l'onglet Comptes |
| `topologie.py` | assemble la page et y injecte les données |
| `csp.py` | politique de sécurité de la page, par empreintes SHA-256 |
| `assets/page/` | sources de la page : `page.html`, `style.css`, `js/*.js` (un fichier par domaine) |
| `assets/connexion.html` | la page de connexion |

**Secrets**

| Fichier | Rôle |
|---|---|
| `secrets_infra.py` | lit un secret : systemd-creds d'abord, l'environnement ensuite |
| `secret_store.py` | secrets chiffrés en base (AES-GCM, clé maîtresse) |
| `depot_scelle.py` | boîte de dépôt des jetons PVE (X25519, HKDF-SHA256, AES-GCM) |
| `inventory_cli.py` | administration : clé maîtresse, migrations, comptes, dépôts, journal |

**Base**

| Fichier | Rôle |
|---|---|
| `sql/schema.sql` | le schéma, version 1 |
| `sql/migrations/NNNN_*.sql` | les versions suivantes (0002 à 0006), appliquées une fois, dans l'ordre |
| `sql/droits.sql` | les droits des deux comptes, rejoués à chaque mise à jour |
| `sql/setup_db.sql`, `sql/setup_db_web.sql` | création manuelle des comptes |

**Déploiement**

| Fichier | Rôle |
|---|---|
| `deploy/preprod.sh` | installation complète et mise à jour |
| `deploy/poser-secret.sh` | pose les clés OVH ou un jeton PVE, chiffrés |
| `deploy/inventory.sh` | lance `inventory_cli.py` sous le compte de collecte, avec ses secrets |
| `deploy/installer.sh` | installe le code et les unités (appelé par `preprod.sh`) |
| `deploy/collecte.sh` | ce que lance le timer : dépôts, collecte, page |
| `deploy/inventaire-*.service`, `.timer` | unités systemd, durcies |
| `deploy/apache-inventaire-local.conf` | vhost en mode comptes locaux |
| `deploy/apache-inventaire.conf` | vhost en mode OIDC (Authentik) |

**Ansible et bastion**

| Fichier | Rôle |
|---|---|
| `client/infra.py` | le client du poste d'administration : `update`, `list`, `simulate`, `run` |
| `bastion_diff.py` | comparatif inventaire / accès The Bastion |
| `deploy/ansible.cfg.example` | Ansible derrière the-bastion-ansible-wrapper |

### Les routes du service

| Accès | Routes |
|---|---|
| public | `GET /api/sante`, `GET /connexion`, `POST /api/connexion`, `GET /api/inventaire` (jeton à usage unique) |
| lecture | `GET /api/rentabilite`, `/api/export.csv`, `/api/vues`, `/api/historique`, `/api/projection`, `/api/journal` |
| commercial | `POST /api/commercial`, `/api/element(s)`, `/api/role(s)`, `/api/surcharge`… |
| admin | `/api/recolte`, `/api/recolte/jeton`, `/api/comptes*`, `/api/journal/integrite` |
| jeton | `POST /api/token` : admin dès qu'un admin est déclaré (toujours en mode local), lecture sinon |

Une seule fonction d'aiguillage applique dans l'ordre : le mode
d'authentification, la même origine pour tout `POST`, le rôle, puis un corps
JSON borné.

---

## Sécurité

Conçu pour tenir une revue NIS 2 sur ce qui est du ressort de l'application.

**Secrets.**

- Aucun secret n'est écrit en clair, ni sur le disque, ni dans un argument de
  commande.
- Les mots de passe SQL, les clés OVH et la clé maîtresse sont chiffrés par
  `systemd-creds`. Ils sont liés à la VM, et au TPM s'il y en a un.
- systemd les dépose dans un tmpfs que seul le service concerné lit. Ils
  n'entrent jamais dans l'environnement d'un processus.
- Les jetons Proxmox sont chiffrés en base sous la clé maîtresse.
- `infra.yml` ne contient que des références (`env:NOM`, `cred:nom`,
  `db:nom`).

**Boîte de dépôt scellée.**

- Le service web scelle un jeton Proxmox avec une clé **publique**, dès la
  réception : X25519, HKDF-SHA256, AES-GCM, avec l'hôte comme donnée
  associée.
- Seule la collecte détient la clé privée.
- Un service web compromis ne relit donc aucun dépôt antérieur.

**Comptes locaux** (en attendant l'OIDC) :

- **Mots de passe :** stockés en scrypt salé, 12 caractères au moins, sans
  l'identifiant.
- **Blocage :** 5 échecs bloquent le compte 15 minutes, et un identifiant
  inconnu coûte le même temps de calcul qu'un vrai.
- **Session :** cookie `__Host-` en `Secure`, `HttpOnly` et `SameSite=Strict`.
  La base n'en garde que l'empreinte. La session expire au bout d'une heure
  d'inactivité, et de huit heures au plus.
- **Premier mot de passe :** un mot de passe généré est à changer à la
  première connexion. D'ici là, le compte n'a aucun rôle.

**Journal chaîné.**

- Chaque saisie, connexion, changement de compte ou dépôt entre au journal
  avec l'empreinte SHA-256 de la ligne précédente.
- La collecte recopie la tête de chaîne dans le journal système à chaque
  passage.
- `deploy/inventory.sh --verifier-journal` dit si la chaîne est intacte.

**Certificats Proxmox.**

- Le certificat de chaque nœud est épinglé au premier contact (`pinned/`).
  Ensuite, seul ce certificat est accepté : il sert d'ancre unique, et urllib3
  compare en plus son empreinte SHA-256.
- Le nom d'hôte n'est pas comparé. Proxmox émet son certificat au nom du nœud
  (`pve-13`), pas au nom OVH par lequel on le joint, et l'épinglage prouve
  déjà l'identité.
- Rien n'est désactivé. `tests_pve_tls.py` le vérifie contre un vrai serveur
  TLS.

**La page.**

- CSP par empreintes SHA-256, sans `'unsafe-inline'`.
- `X-Frame-Options`, `no-store`.
- Le service n'écoute que `127.0.0.1`. Apache termine le TLS et efface tout
  en-tête d'identité venu du client.

**Base.** Le compte du service web n'atteint pas `secret_store`, et n'a
`DELETE` nulle part. Un compte se désactive, il ne s'efface pas. Le compte de
collecte n'a `DELETE` que sur `collect_run` (purge des vieux snapshots) et
`client_token`.

**Ce qui reste à faire.** Le MFA viendra avec l'OIDC (Authentik), déjà prévu
par `web.auth: oidc` et `deploy/apache-inventaire.conf`. En attendant, une
restriction réseau dans le vhost (`Require ip`, en commentaire) est la
protection la moins chère à ajouter.

---

## La collecte

Lancée par `inventaire-collecte.timer` toutes les heures, avec 5 minutes de
décalage aléatoire, et rattrapée au démarrage si la VM était éteinte.
`deploy/collecte.sh` enchaîne :

1. **les dépôts de jetons** : ouverts, testés sur l'hyperviseur, rangés ou
   refusés avec leur motif ;
2. **OVH** : la liste des dédiés et leur matériel facturé. Le prix vient de
   `/services/{id}`, et **les options** (RAM ou disques ajoutés, IP, bande
   passante, licences) de `/services/{id}/options`. Chaque option est
   ramenée au mois, puisqu'un engagement annuel est divisé par 12. Le PA
   d'un serveur est le total facturé ;
3. **Proxmox** : le matériel vu par l'hyperviseur, les VM et LXC, leur
   agent invité (IP, OS). **Un seul nœud est interrogé par cluster**, puisque
   `/cluster/resources` rend déjà tout le cluster ;
4. **la base** : un snapshot, la purge des anciens (`db.keep_runs`), le point
   du jour de l'historique, l'ancre du journal ;
5. **la page**, réécrite de façon atomique : un visiteur ne voit jamais une
   page tronquée.

Un hyperviseur injoignable, ou un droit manquant sur une route, n'arrête pas
la collecte : l'hôte passe « injoignable », avec son motif. Une clé OVH
invalide l'arrête, et systemd voit l'échec.

Les deux reverses OVH se valent mal :

- un reverse personnalisé (`pve01-si.exemple.bzh`) sert tel quel, débarrassé
  de son point final DNS ;
- sans reverse, OVH rend `nsXXXX.ip-a-b-c.eu`. La carte prend alors le nom du
  nœud Proxmox dès que son jeton passe.

```bash
sudo systemctl start inventaire-collecte.service                     # tout de suite
journalctl -u inventaire-collecte.service -n 40 --no-pager -o cat    # ce qu'elle a fait
systemctl list-timers inventaire-collecte.timer                      # le prochain passage
```

---

## Configuration : infra.yml

Installé une fois dans `/opt/inventaire-infra/infra.yml`, puis jamais écrasé.
**Aucun secret**, seulement des références.

| Clé | Rôle |
|---|---|
| `ovh.endpoint` | `ovh-eu` (pointe sur `api.ovh.com`) |
| `pve.pin_certs` | épingle le certificat de chaque nœud (défaut : oui) |
| `pve.jetons_deposes` | un jeton déposé depuis la page sert sans autre réglage |
| `pve.tokens` | un jeton nommé pour un hôte précis, prioritaire |
| `pve.skip` | dédiés qui ne sont pas des hyperviseurs (ESXi, sauvegarde…) : ni sondés, ni comptés injoignables |
| `pve.seulement` | motifs glob pour éprouver la collecte sur une partie du parc |
| `pve.detail` | interroge l'agent invité de chaque VM (IP, OS) ; `false` va plus vite |
| `db.auto_schema` | applique les migrations au passage de la collecte |
| `db.keep_runs` | nombre de snapshots gardés (200) |
| `web.auth` | `local` (comptes de l'onglet Comptes) ou `oidc` (Authentik par Apache) |
| `ansible.comptes` | comptes cibles : `root` sur les hyperviseurs, `ansible` sur les invités |
| `ansible.bastion` | hôte, port et groupe The Bastion ; règles « hors périmètre » |

---

## Inventaire Ansible et client

La collecte remplit la base. Ansible lit la base, sans jamais appeler une API
pendant un playbook.

```bash
python3 infra_inventory.py --from-db --list      # l'inventaire, lu en base
ansible-inventory -i infra_inventory.py --graph  # vu par Ansible
python3 infra_inventory.py --from-db --table     # lecture humaine
```

**Le poste d'administration ne détient aucun accès aux API.**

1. Un admin clique **Jeton** dans la page et obtient une commande prête à
   coller.
2. Le client récupère l'inventaire avec ce jeton, qui ne sert qu'une fois. La
   base ne garde que son empreinte, et un rejeu est refusé avec la date du
   premier usage.

```bash
infra update --token <jeton>    # récupère l'inventaire et le met en cache
infra list [motif]              # ce que contient le cache ; --groupes pour les groupes
infra simulate <playbook>       # --check --diff, n'écrit rien
infra run <playbook>            # simulation, confirmation, puis exécution
```

`run` simule toujours d'abord, et n'exécute rien si la simulation échoue.
Sans terminal, il faut `--oui`. Le cache signale l'âge de la **collecte**, pas
celui du téléchargement. Un playbook de test sans machine joignable :
`exemples/playbook_test.yml`.

### Groupes générés

`pve_nodes`, `pve_guests`, `type_qemu` / `type_lxc`, `status_running` /
`status_stopped`, `cluster_<nom>`, `node_<nom>`, `dc_<datacenter>`,
`tag_<tag>` (étiquettes Proxmox), plus deux groupes de contrôle :
`guests_sans_ip` (agent invité absent) et `pve_unreachable`.

### Variables

Les hyperviseurs portent deux jeux de variables, volontairement séparés :

- `ovh_*` : ce qu'OVH facture (`ovh_cpu_model`, `ovh_ram_mb`, `ovh_disks`…) ;
- `hw_*` : ce que l'hyperviseur voit vraiment (`hw_cpu_logical`,
  `hw_ram_total_mb`, `hw_pve_version`, `hw_kernel`…).

Un écart franc entre les deux signale une pièce remplacée ou une barrette
morte. Les VM portent leurs variables `pve_*` et les `ovh_*` de leur serveur,
ce qui permet de cibler toutes les VM d'un datacenter, ou d'un dédié qui
arrive à échéance.

---

## The Bastion

Les connexions Ansible passent par [The Bastion](https://github.com/ovh/the-bastion)
avec [the-bastion-ansible-wrapper](https://github.com/ovh/the-bastion-ansible-wrapper).
`deploy/ansible.cfg.example` le branche. Chaque opérateur déclare **son**
compte (`BASTION_USER`) : `bastion_user` n'est volontairement jamais dans
l'inventaire. Le wrapper donne la priorité à l'inventaire sur
l'environnement, si bien qu'un compte écrit dans l'inventaire s'imposerait à
tout le monde et effacerait la traçabilité nominative.

```bash
python3 infra_inventory.py --from-db --bastion-commands   # les groupAddServer à jouer
bssh --osh selfListAccesses --json-greppable > acces.json
python3 bastion_diff.py --acces acces.json                # manquants, orphelins, commandes
python3 topologie.py --bastion-acces acces.json           # la même chose sur le schéma
```

Le comparatif comprend les plages (`10.10.20.0/24` couvre `10.10.20.10`). Il
distingue trois états, et pas deux : « déclaré », « manquant », et « hors
périmètre ». Ce dernier vient de la règle `ansible.bastion.hors_perimetre` :
`win*`, l'étiquette `no-bastion`, ou des hôtes en dur. Son code de sortie vaut
1 en cas de dérive, ce qui en fait un contrôle périodique.

---

## Administration courante

Sur la VM, en root, depuis `/opt/inventaire-infra`. `deploy/inventory.sh`
lance `inventory_cli.py` sous le compte de collecte, avec ses secrets
chiffrés : un shell ordinaire ne peut pas les lire, et c'est voulu.

| Besoin | Commande |
|---|---|
| Changer les clés OVH | `bash deploy/poser-secret.sh ovh` |
| Tester les clés OVH en place | `bash deploy/inventory.sh --tester-ovh` |
| Plus aucun admin ne peut se connecter | `bash deploy/inventory.sh --compte-local admin` |
| Vérifier le journal des saisies | `bash deploy/inventory.sh --verifier-journal` |
| Appliquer les migrations à la main | `bash deploy/inventory.sh --migrer` |
| État des secrets en base | `bash deploy/inventory.sh --list` |
| Collecte immédiate | `systemctl start inventaire-collecte.service` |
| Santé du service | `curl -s http://127.0.0.1:8080/api/sante` |

Les comptes de tous les jours se gèrent depuis l'onglet **Comptes**.

**À ne jamais faire :**

- supprimer `/etc/inventaire-infra/creds/master_key.cred` : les jetons en base
  deviendraient définitivement illisibles ;
- copier les fichiers `.cred` sur une autre machine : ils sont liés à cette
  VM ;
- modifier à la main les `10-secrets.conf` des unités : ils sont régénérés
  par `poser-secret.sh`.

---

## Développement

### La page

Ses sources sont dans `assets/page/` : `page.html`, `style.css` et un fichier
JavaScript par domaine dans `js/`.

| Fichier | Domaine |
|---|---|
| `01-carte.js` | le schéma |
| `02-rentabilite.js` | l'arbre |
| `03-tableau.js` | le tableau et le catalogue de colonnes |
| `04*-*.js` | les graphiques, les panneaux, l'historique |
| `05-filtres-vues.js` | les filtres et les vues |
| `06-saisies.js` | les saisies |
| `09-recolte.js` | les jetons |
| `09b-onglets.js` | les onglets |
| `09c-comptes.js` | les comptes |

`topologie.py` les concatène **dans l'ordre des noms**, en un seul script, et
y injecte les données. Deux conséquences :

- toutes les déclarations de premier niveau partagent la même portée : deux
  `const` du même nom, dans deux fichiers, cassent la page entière ;
- une `const` lue au chargement doit être déclarée dans un fichier qui passe
  avant.

### La base

`sql/schema.sql` est la version 1, et chaque évolution passe par une
migration `sql/migrations/NNNN_nom.sql`.

- Une migration est **rejouable** (`IF NOT EXISTS`) et ne contient aucun
  `GRANT`.
- Les droits vont dans `sql/droits.sql`.
- MariaDB refuse un `GRANT` sur une table qui n'existe pas encore : les droits
  passent toujours en dernier.

### Les tests

```bash
python3 tests_rentabilite.py    # calcul de l'arbre, coûts et options, CSV
python3 tests_web.py            # routes et accès, comptes, dépôt, vues, CSP, cartes
python3 tests_secret_store.py   # chiffrement des secrets
python3 tests_capacite.py       # historique et projection
python3 tests_pve_tls.py        # épinglage TLS, contre un vrai serveur local
INVENTAIRE_TEST_MARIADB=detruire python3 tests_mariadb.py
```

`tests_mariadb.py` rejoue l'installation entière sur un vrai MariaDB :
`setup_db.sql`, le schéma, les migrations, les droits. Puis il éprouve
chaque compte avec ses seuls droits. **Il efface la base `inventory`** : à ne
lancer que sur un MariaDB jetable, jamais sur une installation en service.

### Intégration continue

`.github/workflows/tests.yml` tourne à chaque push :

- tous les tests dans un conteneur `debian:12` avec les paquets Debian,
  c'est-à-dire les mêmes versions que la VM, `tests_mariadb.py` compris ;
- les scripts shell vérifiés par `bash -n` ;
- `ruff` en parallèle.

### Conventions

- Code, messages et documentation en français. Les commentaires du code
  s'écrivent sans accents.
- **Fins de ligne LF** (`.gitattributes`). Un `.sh` en CRLF donne
  `$'': command not found`.
- Rien de la page n'est appelé hors de son origine. Une ressource externe
  casserait la CSP.

---

## Dépannage

| Symptôme | Cause probable | Que faire |
|---|---|---|
| `Access denied for user 'inventory'` (1045) | un autre MariaDB tient le port 3306, ou le mot de passe chiffré ne correspond plus | `ss -tlnp \| grep 3306` doit montrer `mariadbd` ; sinon arrêter l'intrus. Sinon : supprimer `creds/inventory_db_password.cred` et relancer `preprod.sh` |
| `preprod.sh` s'arrête sur « MariaDB ne démarre pas » | le port 3306 est pris (le script affiche le journal de MariaDB) | arrêter l'autre serveur, relancer |
| rien ne change à l'écran après une mise à jour | la page n'avait pas été refaite | relancer `preprod.sh` (il la refait), ou lancer une collecte |
| un hyperviseur reste « aucun token » | aucun jeton déposé pour lui | onglet **Jetons PVE**, cocher tous les nœuds du cluster |
| dépôt refusé « certificat refusé » | ancienne version : le nom du certificat était comparé | mettre à jour, redéposer |
| dépôt refusé 401 | la `value` a été mal copiée | `pveum user token remove inventory@pve readonly`, recréer, redéposer |
| « délai dépassé », « certificat injoignable » | le port 8006 du nœud est fermé à la VM | pare-feu |
| `echec de la collecte`, `cles API manquantes` | clés OVH absentes | `bash deploy/poser-secret.sh ovh` |
| `identifiant ou mot de passe incorrect`, compte bloqué | 5 échecs : blocage de 15 min | onglet **Comptes** → Nouveau mot de passe, ou `inventory.sh --compte-local <login>` |
| la page ne s'ouvre pas | nom non résolu, ou 443 fermé | DNS ou fichier hosts ; `ss -tlnp \| grep 443` doit montrer apache2 |

Les journaux :

```bash
journalctl -u inventaire-collecte.service -n 50 --no-pager -o cat
journalctl -u inventaire-web.service -n 50 --no-pager -o cat
tail -n 30 /var/log/apache2/inventaire-error.log
```

---

## Documentation

| Document | Pour |
|---|---|
| [docs/preprod-pas-a-pas.md](docs/preprod-pas-a-pas.md) | **la mise en service**, étape par étape, à suivre à la lettre |
| [docs/deploiement.md](docs/deploiement.md) | le détail de chaque étape faite à la main : comptes SQL, chiffrement, OIDC et MFA, journal, retour arrière |
| [sql/migrations/LISEZMOI.md](sql/migrations/LISEZMOI.md) | écrire une migration |
| [docs/architecture.html](docs/architecture.html) | schéma d'architecture |
