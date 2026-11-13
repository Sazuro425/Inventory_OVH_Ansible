# Mettre l'inventaire en préprod, pas à pas

Ce document se suit **dans l'ordre, sans sauter d'étape**. Chaque étape dit
quoi taper, ce que tu dois voir, et quoi faire si ce n'est pas le cas.
Tu n'as rien à comprendre pour le suivre : copie, colle, compare.

Il y a deux parties :

- **Partie 1 — Préparer le serveur** (environ 15 minutes). Aucun secret
  extérieur n'est demandé.
- **Partie 2 — Poser les accès et lancer** (environ 30 minutes, plus
  5 minutes par cluster Proxmox). Les clés OVH, les jetons Proxmox, la
  première collecte.

On peut faire la partie 1 maintenant et la partie 2 plus tard.

---

## Avant de commencer

### Les deux valeurs à remplacer partout

Dans tout ce document :

| Quand tu lis… | tu mets… |
|---|---|
| `inventaire.preprod.lan` | le nom que tu choisis pour la page (ex. `inventaire.lan.novasys.coop`) |
| `10.0.0.50` | l'adresse IP de la VM |

Note-les ici avant de commencer :

- Nom de la page : ______________________________
- IP de la VM : ______________________________

### Ce qu'il te faut

- [ ] Une VM **Debian 12** neuve, avec au moins 2 Go de RAM et 10 Go de disque.
- [ ] Un accès SSH à cette VM, avec un compte qui peut passer root.
- [ ] Que la VM sorte vers Internet en HTTPS (port 443), pour `apt` et pour
      l'API OVH.
- [ ] Que la VM joigne **chaque hyperviseur Proxmox sur le port 8006**.
- [ ] Pour la partie 2 : un accès à l'espace client OVH, et un accès root à
      **un nœud par cluster** Proxmox.

### Les règles du jeu

- Un bloc gris = une commande. Copie-la **en entier**, colle-la, appuie sur
  Entrée.
- `Tu dois voir` = ce qui doit s'afficher. Si ça ne correspond pas, ne
  continue pas : va voir la section **En cas de problème** en bas.
- Quand on te demande de **coller un secret**, rien ne s'affiche pendant que
  tu colles. C'est normal. Colle une seule fois, puis appuie sur Entrée.
- Aucun secret n'est jamais écrit en clair sur le serveur. Ne les mets pas
  non plus dans un fichier texte « pour plus tard » : un gestionnaire de mots
  de passe, ou rien.

---

# PARTIE 1 — Préparer le serveur

## Étape 1 — Se connecter à la VM et passer root

Depuis ton PC (PowerShell ou terminal) :

```bash
ssh ton-compte@10.0.0.50
```

Puis, sur la VM :

```bash
sudo -i
```

> Si `sudo` n'existe pas (« command not found »), tape `su -` à la place, et
> donne le mot de passe root.

**Tu dois voir** une invite qui se termine par `#` (et non `$`). Toutes les
commandes qui suivent se tapent dans cette session root.

## Étape 2 — Vérifier l'heure

Une VM qui n'est pas à l'heure voit les certificats et les échéances des
jetons de travers, et les dates du journal sont fausses.

```bash
timedatectl | grep synchronized
```

**Tu dois voir** `System clock synchronized: yes`.

> Si tu vois `no` : `apt install -y systemd-timesyncd`, puis mets
> `NTP=172.18.200.254` dans `/etc/systemd/timesyncd.conf`, puis
> `systemctl restart systemd-timesyncd`. Relance la vérification au bout
> d'une minute.

## Étape 3 — Récupérer le code

```bash
apt update && apt install -y git
cd /root
git clone https://github.com/Sazuro425/Inventory_OVH_Ansible.git
cd Inventory_OVH_Ansible
```

**Tu dois voir** `Cloning into 'Inventory_OVH_Ansible'...` puis rien d'autre
d'alarmant.

> Si git te demande un identifiant et un mot de passe, c'est que le dépôt est
> privé. Fais `Ctrl+C`, et copie plutôt le dossier depuis ton PC Windows. Dans
> PowerShell, **sur ton PC** :
>
> ```powershell
> scp -r C:\Users\Evan\Documents\Inventory_OVH_Ansible ton-compte@10.0.0.50:/tmp/
> ```
>
> Puis sur la VM, en root :
> `mv /tmp/Inventory_OVH_Ansible /root/ && cd /root/Inventory_OVH_Ansible`

## Étape 4 — Lancer l'installation

Remplace `inventaire.preprod.lan` par ton nom, puis :

```bash
bash deploy/preprod.sh --nom inventaire.preprod.lan
```

Le script prend 2 à 5 minutes. Il affiche des sections numérotées de 0 à 8 ;
chacune se termine par des lignes vertes.

Ce qu'il fait tout seul, pour info :

- il installe MariaDB, Apache et Python ;
- il crée la base et ses deux comptes, avec des mots de passe tirés au
  hasard que personne ne voit ;
- il chiffre tous les secrets avec `systemd-creds` ;
- il génère un certificat HTTPS ;
- il démarre le service web.

**Tu dois voir**, vers la fin :

```
== 6. Service web
   service web demarre : {"etat": "degrade", "erreur": "aucun snapshot en base"}
   https://inventaire.preprod.lan/connexion repond (200)
```

> `degrade` et `aucun snapshot` sont **normaux** à ce stade : aucune collecte
> n'a encore tourné.

Puis la section 8, **la plus importante** :

```
== 8. Compte admin
   Note le mot de passe ci-dessous : il ne sera plus jamais affiche.

  compte local : admin
  roles        : lecture, commercial, admin
  mot de passe : Xy7...........................
```

**Copie ce mot de passe tout de suite dans ton gestionnaire de mots de
passe.** Il ne sera plus jamais affiché.

> Tu l'as raté ? Pas grave :
> `/opt/inventaire-infra/deploy/inventory.sh --compte-local admin` en donne un
> nouveau.

La dernière ligne doit être :

```
La preprod tourne. Suite : la partie 2 de docs/preprod-pas-a-pas.md
```

> Le script s'est arrêté sur une ligne rouge `ARRET : ...` ? Lis la raison
> donnée, corrige, et **relance exactement la même commande**. On peut le
> relancer autant de fois que nécessaire : il saute ce qui est déjà fait et ne
> change aucun mot de passe.

## Étape 5 — Rendre le nom joignable depuis ton PC

Ton navigateur doit savoir que `inventaire.preprod.lan` = `10.0.0.50`. Il y a
deux façons de le faire.

**A. Pour toute l'équipe (recommandé) : dans OPNsense.**

1. Va dans *Services → Dnsmasq DNS & DHCP → Hosts*, puis **+**.
2. Remplis *Host* = `inventaire`, *Domain* = le reste du nom, *IP* = `10.0.0.50`.
3. **Ne coche PAS la case « Local ».** Elle rendrait dnsmasq autoritaire sur
   tout le domaine, et casserait les noms publics.
4. Clique *Save* puis *Apply*.

**B. Juste pour ton PC : le fichier hosts.**

1. Ouvre le Bloc-notes **en tant qu'administrateur** (clic droit → Exécuter en
   tant qu'administrateur).
2. Ouvre `C:\Windows\System32\drivers\etc\hosts` (choisis « Tous les fichiers »
   pour le voir).
3. Ajoute à la fin la ligne `10.0.0.50  inventaire.preprod.lan`, puis
   enregistre.

**Vérifie** dans PowerShell, sur ton PC :

```powershell
Test-NetConnection inventaire.preprod.lan -Port 443
```

**Tu dois voir** `TcpTestSucceeded : True`.

## Étape 6 — Première connexion

1. Ouvre `https://inventaire.preprod.lan/` dans ton navigateur.
2. Le navigateur affiche un avertissement de sécurité. **C'est normal** : le
   certificat est auto-signé (préprod).
   - Firefox : *Avancé…* → *Accepter le risque et poursuivre*.
   - Chrome / Edge : *Paramètres avancés* → *Continuer vers le site*.
3. La page de connexion s'affiche. Identifiant `admin`, et le mot de passe de
   l'étape 4.
4. La page te demande d'en **choisir un nouveau** : 12 caractères au moins,
   sans le mot `admin` dedans. Range-le dans ton gestionnaire de mots de passe.

**Tu dois voir** la page *« Inventaire : en attente de la première
collecte »*. C'est normal : la carte apparaîtra après la partie 2.

> Après 5 mauvais mots de passe, le compte est bloqué 15 minutes.
> Pour le débloquer tout de suite : `/opt/inventaire-infra/deploy/inventory.sh --compte-local admin`
> sur la VM (nouveau mot de passe).

### ✅ Fin de la partie 1

- [ ] Le script s'est terminé par `La preprod tourne`.
- [ ] Le mot de passe admin est rangé.
- [ ] Tu vois la page « en attente de la première collecte ».

Tu peux t'arrêter ici et reprendre la partie 2 plus tard.

---

# PARTIE 2 — Poser les accès et lancer

Reconnecte-toi à la VM et passe root, comme à l'étape 1. Puis place-toi dans
le dossier de l'application :

```bash
cd /opt/inventaire-infra
```

## Étape 7 — Créer les clés OVH

1. Ouvre **https://api.ovh.com/createToken/** (pas `eu.api.ovh.com`, dont le
   certificat est refusé).
2. Connecte-toi avec le compte OVH qui voit les serveurs.
3. Remplis :
   - *Application name* : `inventaire-preprod`
   - *Application description* : `inventaire, lecture seule`
   - *Validity* : **1 year**. Note la date d'expiration dans ton agenda : ce
     jour-là, la collecte s'arrêtera.
   - *Rights* : ajoute ces **4 lignes**, toutes en **GET** (le **+** ajoute
     une ligne) :

     | Méthode | Chemin |
     |---|---|
     | GET | `/me` |
     | GET | `/dedicated/server*` |
     | GET | `/services*` |
     | GET | `/vps*` |

4. Clique **Create keys**.

**Tu dois voir** trois valeurs : *Application Key*, *Application Secret*,
*Consumer Key*. **Laisse cette page ouverte** pour l'étape suivante.

> Uniquement des **GET**. Jamais de POST, PUT ou DELETE : l'inventaire ne fait
> que lire.

## Étape 8 — Poser les clés OVH sur le serveur

```bash
bash deploy/poser-secret.sh ovh
```

Le script demande les trois clés, l'une après l'autre. Pour chacune : copie
la valeur depuis la page OVH, colle-la (rien ne s'affiche), puis Entrée.

**Tu dois voir** :

```
Test des clefs aupres d'OVH (rien n'est encore en place) :
  Signature  GET /me ............................ [OK]  compte xx12345-ovh
  Droit  GET /dedicated/server .................. [OK]  31 serveur(s)
  Droit  GET .../serviceInfos ................... [OK]  expirations lisibles

  clefs OVH valides.
Clefs OVH en place, chiffrees dans /etc/inventaire-infra/creds.
```

Tu peux maintenant fermer la page OVH. Les clés ne sont plus nulle part en
clair.

> Une ligne `[KO]` ? Le script n'a **rien** enregistré. Lis le message :
> - `This application key is invalid` : une clé mal copiée (souvent l'Application
>   Key et le Secret inversés). Relance la commande.
> - `This call has not been granted` : il manque un des 4 droits de l'étape 7.
>   Recrée les clés avec les 4 lignes.
> - `Invalid signature` : l'Application Secret ou la Consumer Key ne va pas
>   avec l'Application Key. Recopie les trois depuis la même page OVH.
> - un message qui parle de certificat ou de proxy : la VM ne sort pas
>   proprement vers `api.ovh.com` (proxy, inspection TLS). Voir avec le réseau.

## Étape 9 — Première collecte (OVH seulement)

```bash
systemctl start inventaire-collecte.service
```

La commande rend la main quand la collecte est finie, en général en moins
d'une minute. Ensuite :

```bash
journalctl -u inventaire-collecte.service -n 15 --no-pager -o cat
```

**Tu dois voir**, entre autres :

```
secrets fournis par systemd-creds
...
snapshot #1 enregistre en base
...
collecte terminee en ..s
page publiee : /var/www/inventaire/index.html
```

> À ce stade, les hyperviseurs apparaissent **injoignables** : on ne leur a
> pas encore donné de jeton. C'est l'étape suivante.
>
> `echec de la collecte` ? Lis les lignes au-dessus.
> `cles API manquantes` = l'étape 8 n'est pas faite.

Recharge la page dans ton navigateur (`F5`). **Tu dois voir** la carte, avec
tes serveurs OVH, et en haut les onglets **Rentabilité**, **Schéma**,
**Jetons PVE** et **Comptes**.

## Étape 10 — Créer le compte de lecture sur chaque cluster Proxmox

À faire **sur un seul nœud par cluster**, en root. Le compte et le jeton
valent pour tout le cluster.

1. Dans la page, ouvre l'onglet **Jetons PVE**, puis clique **Déposer un jeton…**.
2. Ouvre *« Créer le compte de récolte sur l'hyperviseur »*, puis clique
   **Copier les commandes**.
3. Connecte-toi en root (SSH ou Shell de l'interface Proxmox) sur **un** nœud du
   cluster, colle les trois commandes, puis Entrée.

**Tu dois voir**, à la fin, un tableau comme celui-ci :

```
┌──────────────┬──────────────────────────────────────┐
│ key          │ value                                │
╞══════════════╪══════════════════════════════════════╡
│ full-tokenid │ inventory@pve!readonly               │
├──────────────┼──────────────────────────────────────┤
│ value        │ 1a2b3c4d-....-....-....-............ │
└──────────────┴──────────────────────────────────────┘
```

**Copie la `value` tout de suite** : Proxmox ne l'affichera plus jamais.

> `user 'inventory@pve' already exists` sur la première commande : le compte
> existe déjà sur ce cluster, ce n'est pas grave. Mais si
> `token 'readonly' already exists` apparaît sur la troisième, supprime
> l'ancien jeton (`pveum user token remove inventory@pve readonly`), puis
> relance la troisième commande.

## Étape 11 — Déposer le jeton dans la page

Toujours dans le formulaire *Déposer un jeton PVE* :

1. *Identifiant du jeton* : laisse `inventory@pve!readonly`.
2. *Valeur du jeton* : colle la `value` de l'étape 10.
3. *Échéance* : laisse la date proposée (dans un an). Les commandes copiées à
   l'étape 10 ont posé la même date sur Proxmox.
4. *Hyperviseurs* : coche **tous les nœuds de ce cluster**. Ceux du même
   cluster sont souvent déjà cochés.
5. Clique **Sceller et déposer**.

**Tu dois voir** *« Jeton scellé pour N hyperviseur(s) : testé à la prochaine
collecte. »*

Recommence les étapes 10 et 11 **pour chaque cluster**. Garde la liste des
clusters déjà faits sous les yeux : la vue *Récolte* les montre aussi, avec
la mention *dépôt en attente*.

> Le service web ne peut pas relire un jeton déposé : il est chiffré dès
> réception, et seule la collecte peut l'ouvrir.

## Étape 12 — Deuxième collecte (OVH + Proxmox)

Sur la VM :

```bash
systemctl start inventaire-collecte.service
journalctl -u inventaire-collecte.service -n 30 --no-pager -o cat
```

Cette fois, la collecte teste chaque jeton déposé, puis interroge les
hyperviseurs. Compte quelques minutes pour 30 clusters.

Recharge la page, puis ouvre l'onglet **Jetons PVE**. **Tu dois voir** les hyperviseurs
**joignables**, avec leur jeton et son échéance.

> Un hyperviseur reste injoignable ? Son motif est affiché à côté de lui dans
> *Récolte*. Les plus courants :
> - jeton refusé (401) : la `value` a été mal copiée. Refais l'étape 10 (en
>   supprimant le jeton) puis l'étape 11 ;
> - délai dépassé, ou certificat injoignable : la VM ne joint pas ce nœud sur
>   le port 8006. Pare-feu ;
> - aucun token : aucun jeton n'a été déposé pour ce nœud. Refais l'étape 11
>   en le cochant.

## Étape 13 — Activer la collecte automatique

```bash
systemctl enable --now inventaire-collecte.timer
systemctl list-timers inventaire-collecte.timer --no-pager
```

**Tu dois voir** une ligne avec la date du prochain passage (toutes les
heures).

### ✅ Fin de la partie 2

- [ ] `poser-secret.sh ovh` a dit `clefs OVH valides`.
- [ ] La carte affiche les serveurs et leurs VM.
- [ ] *Récolte* montre tous les hyperviseurs joignables.
- [ ] Le timer est actif.

La préprod est en service.

---

## En cas de problème

| Ce que tu vois | Ce que ça veut dire | Quoi faire |
|---|---|---|
| `ARRET : ...` pendant `preprod.sh` | une étape a échoué | lire la raison, corriger, relancer la même commande |
| `apt` échoue (« Temporary failure resolving ») | la VM ne sort pas sur Internet | DNS ou proxy de la VM |
| la page ne s'ouvre pas du tout | nom non résolu ou port fermé | refaire l'étape 5 ; sur la VM, `ss -tlnp \| grep 443` doit montrer apache2 |
| `identifiant ou mot de passe incorrect`, ou `trop d'echecs : compte bloque` | mauvais mot de passe, ou 5 échecs (blocage 15 min) | `/opt/inventaire-infra/deploy/inventory.sh --compte-local admin` |
| la page dit `base indisponible` | MariaDB arrêté | `systemctl restart mariadb`, puis `systemctl restart inventaire-web` |
| toujours « en attente de la première collecte » | aucune collecte réussie | étape 9, et lire le journal |
| `echec de la collecte` dans le journal | voir la ligne juste au-dessus | `cles API manquantes` = étape 8 ; `Invalid signature` ou `not been granted` = refaire les étapes 7 et 8 |

Les journaux à regarder, sur la VM :

```bash
journalctl -u inventaire-collecte.service -n 50 --no-pager -o cat
journalctl -u inventaire-web.service -n 50 --no-pager -o cat
tail -n 30 /var/log/apache2/inventaire-error.log
```

---

## Commandes utiles (plus tard)

Toutes sur la VM, en root, depuis `/opt/inventaire-infra`.

**Créer un compte pour un collègue** : onglet **Comptes** de la page (admins
seulement). Saisis son identifiant (`prenom.nom`), coche ses rôles, clique
**Créer le compte**. Le mot de passe s'affiche une fois, en haut de l'onglet :
transmets-le de vive voix ou par le gestionnaire de mots de passe. Il le
change à sa première connexion.

| Rôle | Ce qu'il permet |
|---|---|
| Lecture | tout voir, rien saisir |
| Commercial | saisir ventes, enfants, corrections |
| Admin | en plus : jetons PVE, comptes, journal |

**Mot de passe oublié, compte à couper, rôles à changer** : même onglet,
boutons **Nouveau mot de passe**, **Désactiver**, **Rôles…** sur la ligne du
compte. Un admin ne peut ni se désactiver ni se retirer le rôle admin, et il
reste toujours au moins un admin actif.

Si plus aucun admin ne peut se connecter : sur la VM,
`bash deploy/inventory.sh --compte-local admin` (nouveau mot de passe).

**Changer les clés OVH** (expiration, fuite) : `bash deploy/poser-secret.sh ovh`

**Lancer une collecte tout de suite** : `systemctl start inventaire-collecte.service`

**Mettre à jour l'application** :

```bash
cd /root/Inventory_OVH_Ansible && git pull
bash deploy/preprod.sh --nom inventaire.preprod.lan
```

Le script met le code à jour, applique les migrations de la base, et ne
touche à aucun secret.

**Vérifier que personne n'a réécrit le journal des saisies** :
`bash deploy/inventory.sh --verifier-journal`

---

## À ne jamais faire

- **Supprimer `/etc/inventaire-infra/creds/master_key.cred`.** C'est la clé
  qui ouvre les jetons Proxmox rangés en base. Sans elle, ils sont
  définitivement illisibles : il faudrait tous les redéposer.
- **Modifier à la main les fichiers `10-secrets.conf`** dans
  `/etc/systemd/system/inventaire-*.service.d/`. Ils sont régénérés par
  `poser-secret.sh`.
- **Copier les fichiers `.cred` sur une autre machine** en espérant qu'ils
  marchent. Ils sont liés à cette VM. Si la VM est recréée, on refait ce
  document depuis le début : clés OVH, jetons, comptes.
- **Mettre un secret dans `infra.yml`**, dans un fichier texte, ou dans un
  message.

## Où sont les choses (pour info)

| Quoi | Où |
|---|---|
| le code | `/opt/inventaire-infra` |
| la configuration | `/opt/inventaire-infra/infra.yml` (rien à modifier pour la préprod) |
| les secrets chiffrés de la collecte | `/etc/inventaire-infra/creds/` |
| le secret chiffré du service web | `/etc/inventaire-infra/creds-web/` |
| la page générée | `/var/www/inventaire/index.html` |
| la configuration Apache | `/etc/apache2/sites-available/inventaire.conf` |
| le certificat (auto-signé) | `/etc/ssl/certs/inventaire.pem` |

Le détail technique de chaque choix (pourquoi deux comptes de base, la boîte
de dépôt scellée, le journal chaîné) est dans `docs/deploiement.md`.
