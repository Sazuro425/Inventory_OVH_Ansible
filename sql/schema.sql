-- Inventaire OVH -> Proxmox : snapshots complets
-- MariaDB 10.11+ (Debian 12)
--
--   mariadb -u inventory -p inventory < schema.sql
--
-- Chaque collecte ecrit un run et la photo complete du parc a cet instant.
-- L'etat courant se lit par les vues v_current_*, qui pointent sur le dernier
-- run exploitable.

SET NAMES utf8mb4;

-- Indispensable quand le fichier est lance depuis un client sans
-- schema par defaut (Workbench en root, par exemple).
USE inventory;

CREATE TABLE IF NOT EXISTS collect_run (
  id             INT AUTO_INCREMENT PRIMARY KEY,
  started_at     DATETIME     NOT NULL,
  finished_at    DATETIME     NULL,
  status         ENUM('running', 'ok', 'partial', 'error') NOT NULL DEFAULT 'running',
  nb_servers     INT          NOT NULL DEFAULT 0,
  nb_nodes       INT          NOT NULL DEFAULT 0,
  nb_guests      INT          NOT NULL DEFAULT 0,
  nb_unreachable INT          NOT NULL DEFAULT 0,
  message        VARCHAR(500) NULL,
  INDEX idx_run_status_date (status, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Machines physiques cote OVH
CREATE TABLE IF NOT EXISTS server_snapshot (
  id           BIGINT AUTO_INCREMENT PRIMARY KEY,
  run_id       INT          NOT NULL,
  ovh_name     VARCHAR(190) NOT NULL,
  reverse_name VARCHAR(190) NULL,
  ip           VARCHAR(45)  NULL,
  offer        VARCHAR(190) NULL,
  datacenter   VARCHAR(40)  NULL,
  os           VARCHAR(100) NULL,
  state        VARCHAR(40)  NULL,
  expiration   DATE         NULL,
  cpu_model    VARCHAR(190) NULL,
  cpu_sockets  INT          NULL,
  cpu_cores    INT          NULL,
  cpu_threads  INT          NULL,
  ram_mb       INT          NULL,
  disk_count   INT          NULL,
  disk_desc    VARCHAR(300) NULL,
  UNIQUE KEY uq_server (run_id, ovh_name),
  INDEX idx_server_name (ovh_name),
  INDEX idx_server_expiration (expiration),
  CONSTRAINT fk_server_run FOREIGN KEY (run_id)
    REFERENCES collect_run (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Hyperviseurs : un par dedie sonde, joignable ou non
CREATE TABLE IF NOT EXISTS node_snapshot (
  id           BIGINT AUTO_INCREMENT PRIMARY KEY,
  run_id       INT          NOT NULL,
  host         VARCHAR(190) NOT NULL,
  pve_node     VARCHAR(100) NULL,
  cluster_name VARCHAR(190) NULL,
  reachable    TINYINT(1)   NOT NULL DEFAULT 1,
  error        VARCHAR(300) NULL,
  cpu_model    VARCHAR(190) NULL,
  cpu_sockets  INT          NULL,
  cpu_cores    INT          NULL,
  cpu_logical  INT          NULL,
  cpu_mhz      INT          NULL,
  ram_total_mb INT          NULL,
  ram_used_mb  INT          NULL,
  disk_count   INT          NULL,
  disk_total_gb DECIMAL(10,1) NULL,
  disk_desc    VARCHAR(300) NULL,
  pve_version  VARCHAR(60)  NULL,
  kernel       VARCHAR(120) NULL,
  UNIQUE KEY uq_node (run_id, host),
  INDEX idx_node_cluster (cluster_name),
  CONSTRAINT fk_node_run FOREIGN KEY (run_id)
    REFERENCES collect_run (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- VM et conteneurs
CREATE TABLE IF NOT EXISTS guest_snapshot (
  id           BIGINT AUTO_INCREMENT PRIMARY KEY,
  run_id       INT          NOT NULL,
  cluster_name VARCHAR(190) NULL,
  pve_node     VARCHAR(100) NULL,
  via_host     VARCHAR(190) NULL,
  vmid         INT          NOT NULL,
  guest_type   VARCHAR(10)  NULL,
  name         VARCHAR(190) NULL,
  status       VARCHAR(20)  NULL,
  cpus         INT          NULL,
  ram_mb       INT          NULL,
  disk_gb      DECIMAL(10,1) NULL,
  uptime       VARCHAR(30)  NULL,
  os           VARCHAR(190) NULL,
  ipv4         VARCHAR(200) NULL,
  tags         VARCHAR(300) NULL,
  UNIQUE KEY uq_guest (run_id, cluster_name, vmid),
  INDEX idx_guest_name (name),
  INDEX idx_guest_vmid (vmid),
  INDEX idx_guest_status (status),
  CONSTRAINT fk_guest_run FOREIGN KEY (run_id)
    REFERENCES collect_run (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Jetons a usage unique remis aux postes d'administration.
-- Le jeton n'est jamais stocke en clair : seule son empreinte l'est, donc une
-- lecture de la base ne permet pas de s'en servir.
CREATE TABLE IF NOT EXISTS client_token (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  empreinte   CHAR(64)     NOT NULL,
  emis_par    VARCHAR(190) NOT NULL,
  emis_le     DATETIME     NOT NULL,
  expire_le   DATETIME     NOT NULL,
  consomme_le DATETIME     NULL,
  consomme_de VARCHAR(45)  NULL,
  UNIQUE KEY uq_empreinte (empreinte),
  INDEX idx_token_expire (expire_le)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Secrets chiffres : jetons PVE, clefs OVH. Chiffrement d'enveloppe, la clef
-- maitresse restant protegee par systemd-creds. Un vidage de cette table ne
-- donne rien sans elle, et elle n'est lisible que sur la machine de collecte.
--
-- Le mot de passe de la base et la clef maitresse elle-meme n'ont pas leur
-- place ici : il faut le premier pour atteindre la table, et la seconde est
-- tout l'interet du montage. Ces deux-la restent dans systemd-creds.
--
-- Le service web ne doit avoir aucun droit sur cette table : il partage la
-- base, pas les secrets.
CREATE TABLE IF NOT EXISTS secret_store (
  nom         VARCHAR(64)    NOT NULL,
  version     INT            NOT NULL,
  nonce       VARBINARY(12)  NOT NULL,
  chiffre     VARBINARY(512) NOT NULL,
  cle_version INT            NOT NULL DEFAULT 1,
  cree_le     DATETIME       NOT NULL,
  cree_par    VARCHAR(190)   NOT NULL,
  actif       TINYINT(1)     NOT NULL DEFAULT 1,
  -- (nom, version) plutot qu'un id auto : deux ecritures concurrentes qui
  -- calculeraient la meme version se heurtent a la contrainte au lieu de
  -- s'ecraser l'une l'autre
  PRIMARY KEY (nom, version),
  -- la lecture courante fait toujours :
  --   WHERE nom = ? AND actif = 1 ORDER BY version DESC LIMIT 1
  INDEX idx_secret_actif (nom, actif, version),
  -- quelles lignes restent a rechiffrer pendant une rotation de clef
  INDEX idx_secret_cle (cle_version, actif)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- Cout du serveur chez OVH, releve a chaque collecte.
--
-- Table a part plutot que des colonnes sur server_snapshot : le prix vient
-- d'une autre API (/services) et demande un droit supplementaire sur la clef
-- OVH. Si ce droit manque, la collecte materielle continue et cette table
-- reste vide, au lieu de semer des colonnes nulles dans l'instantane
-- principal.
--
-- price_month est le prix ramene au mois a l'ecriture : price_period vaut
-- P1M, P1Y ou P3M selon l'engagement, et personne n'a envie de parser de
-- l'ISO8601 dans une vue SQL.
CREATE TABLE IF NOT EXISTS server_cost (
  run_id         INT           NOT NULL,
  ovh_name       VARCHAR(190)  NOT NULL,
  service_id     BIGINT        NULL,
  price_value    DECIMAL(10,2) NULL,
  price_currency CHAR(3)       NULL,
  price_period   VARCHAR(16)   NULL,
  price_month    DECIMAL(10,2) NULL,
  next_billing   DATE          NULL,
  erreur         VARCHAR(300)  NULL,
  PRIMARY KEY (run_id, ovh_name),
  INDEX idx_cost_nom (ovh_name),
  CONSTRAINT fk_cost_run FOREIGN KEY (run_id)
    REFERENCES collect_run (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- Volet commercial : ce qu'une VM est vendue, et sous quel contrat.
--
-- Ce n'est PAS un instantane : pas de run_id, la donnee est saisie a la main
-- et survit aux collectes. Elle est rattachee a la VM par son nom, parce que
-- c'est la seule chose qu'un commercial connaisse ; cluster_name leve
-- l'ambiguite quand deux clusters portent une VM du meme nom.
--
-- Rien n'est jamais supprime : on repasse actif a 0. Un contrat resilie doit
-- rester lisible, c'est la trace de ce qui a ete facture.
CREATE TABLE IF NOT EXISTS commercial (
  id           INT AUTO_INCREMENT PRIMARY KEY,
  vm_name      VARCHAR(190)  NOT NULL,
  cluster_name VARCHAR(190)  NULL,
  contrat      VARCHAR(100)  NOT NULL,
  client       VARCHAR(190)  NULL,
  prix_vente   DECIMAL(10,2) NOT NULL,
  devise       CHAR(3)       NOT NULL DEFAULT 'EUR',
  periode      ENUM('mois','an') NOT NULL DEFAULT 'mois',
  debut        DATE          NULL,
  fin          DATE          NULL,
  actif        TINYINT(1)    NOT NULL DEFAULT 1,
  cree_le      DATETIME      NOT NULL,
  cree_par     VARCHAR(190)  NOT NULL,
  maj_le       DATETIME      NULL,
  maj_par      VARCHAR(190)  NULL,
  -- un numero de contrat ne designe qu'une vente : la contrainte attrape la
  -- double saisie, l'erreur la plus courante quand deux commerciaux
  -- travaillent sur le meme parc
  UNIQUE KEY uq_contrat (contrat),
  INDEX idx_vente_vm (vm_name, actif)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- Elements declares a la main : ce que la collecte ne voit pas.
--
-- Un site dans une VM mutualisee, un vhost, un domaine, une boite mail, une
-- licence, un stockage partage au niveau du cluster. Chacun porte un prix de
-- vente et un prix d'achat, et se rattache a une carte parente : un cluster,
-- un serveur, une VM, ou un autre element. La relation est toujours la meme,
-- a n'importe quelle profondeur, et la carte parente cumule ses enfants.
--
-- parent_ref vaut, selon parent_type : le nom du cluster, le nom OVH du
-- serveur, le nom de la VM, ou l'id d'un autre element.
--
-- Comme pour commercial, rien n'est supprime : actif passe a 0.
CREATE TABLE IF NOT EXISTS element (
  id           INT AUTO_INCREMENT PRIMARY KEY,
  parent_type  ENUM('cluster','serveur','vm','element') NOT NULL,
  parent_ref   VARCHAR(190)  NOT NULL,
  nom          VARCHAR(190)  NOT NULL,
  genre        VARCHAR(40)   NULL,
  contrat      VARCHAR(100)  NULL,
  client       VARCHAR(190)  NULL,
  prix_vente   DECIMAL(10,2) NOT NULL DEFAULT 0,
  prix_achat   DECIMAL(10,2) NOT NULL DEFAULT 0,
  devise       CHAR(3)       NOT NULL DEFAULT 'EUR',
  periode      ENUM('mois','an') NOT NULL DEFAULT 'mois',
  note         VARCHAR(500)  NULL,
  actif        TINYINT(1)    NOT NULL DEFAULT 1,
  cree_le      DATETIME      NOT NULL,
  cree_par     VARCHAR(190)  NOT NULL,
  maj_le       DATETIME      NULL,
  maj_par      VARCHAR(190)  NULL,
  INDEX idx_element_parent (parent_type, parent_ref, actif)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- Corrections manuelles des valeurs remontees par la collecte.
--
-- Une ligne remplace la valeur PROPRE d'une carte : le cout OVH d'un serveur
-- (tarif negocie, droit /services absent), le prix d'achat d'une VM (licence),
-- ou le cluster d'un serveur (regroupement commercial de noeuds seuls).
-- Les enfants continuent de s'ajouter par-dessus : corriger le cout d'un
-- serveur ne fait pas disparaitre ce que ses VM rapportent.
--
-- valeur a NULL leve la correction. On ne supprime pas la ligne : le compte
-- web n'a pas DELETE, et qui a leve quoi reste lisible dans le journal.
CREATE TABLE IF NOT EXISTS surcharge (
  cible_type   ENUM('cluster','serveur','vm') NOT NULL,
  cible_ref    VARCHAR(190)  NOT NULL,
  champ        ENUM('prix_achat','prix_vente','cluster') NOT NULL,
  valeur       VARCHAR(190)  NULL,
  motif        VARCHAR(300)  NOT NULL,
  maj_le       DATETIME      NOT NULL,
  maj_par      VARCHAR(190)  NOT NULL,
  PRIMARY KEY (cible_type, cible_ref, champ)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- Journal de toutes les saisies : ventes, elements, corrections.
--
-- Le compte web y a INSERT et SELECT, jamais UPDATE ni DELETE : un defaut du
-- service peut ecrire une saisie fausse, il ne peut pas effacer qu'elle a eu
-- lieu. avant et apres sont du JSON, pour qu'un litige se tranche sans
-- reconstituer l'etat de la base a une date.
-- ---------------------------------------------------------------------------
-- Vues de rentabilite enregistrees : un nom ("Commercial", "Compta mensuelle")
-- et ce qu'il faut pour la rouvrir — filtres, presentation, niveau du
-- tableau. definition est du JSON, controle champ par champ par
-- inventory_db.verifier_vue() avant d'etre ecrit : la page le relit et
-- l'applique, elle ne l'execute pas.
--
-- Partagee par defaut : une vue sert a une equipe. Seul son auteur, ou un
-- admin, la modifie ou la retire. Comme ailleurs, rien n'est supprime.
CREATE TABLE IF NOT EXISTS vue_rentabilite (
  id           INT AUTO_INCREMENT PRIMARY KEY,
  nom          VARCHAR(80)   NOT NULL,
  definition   TEXT          NOT NULL,
  partagee     TINYINT(1)    NOT NULL DEFAULT 1,
  actif        TINYINT(1)    NOT NULL DEFAULT 1,
  cree_le      DATETIME      NOT NULL,
  cree_par     VARCHAR(190)  NOT NULL,
  maj_le       DATETIME      NULL,
  maj_par      VARCHAR(190)  NULL,
  INDEX idx_vue_actif (actif, partagee)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS journal_saisie (
  id           BIGINT AUTO_INCREMENT PRIMARY KEY,
  le           DATETIME      NOT NULL,
  par          VARCHAR(190)  NOT NULL,
  action       VARCHAR(40)   NOT NULL,
  cible        VARCHAR(300)  NOT NULL,
  avant        TEXT          NULL,
  apres        TEXT          NULL,
  INDEX idx_journal_le (le),
  INDEX idx_journal_cible (cible(190))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- Migration : ajoute les colonnes manquantes sur une base deja en place.
-- ADD COLUMN IF NOT EXISTS est idempotent, ce bloc peut etre rejoue.
-- ---------------------------------------------------------------------------

ALTER TABLE server_snapshot
  ADD COLUMN IF NOT EXISTS cpu_model   VARCHAR(190)  NULL,
  ADD COLUMN IF NOT EXISTS cpu_sockets INT           NULL,
  ADD COLUMN IF NOT EXISTS cpu_cores   INT           NULL,
  ADD COLUMN IF NOT EXISTS cpu_threads INT           NULL,
  ADD COLUMN IF NOT EXISTS ram_mb      INT           NULL,
  ADD COLUMN IF NOT EXISTS disk_count  INT           NULL,
  ADD COLUMN IF NOT EXISTS disk_desc   VARCHAR(300)  NULL;

ALTER TABLE node_snapshot
  ADD COLUMN IF NOT EXISTS cpu_model     VARCHAR(190)  NULL,
  ADD COLUMN IF NOT EXISTS cpu_sockets   INT           NULL,
  ADD COLUMN IF NOT EXISTS cpu_cores     INT           NULL,
  ADD COLUMN IF NOT EXISTS cpu_logical   INT           NULL,
  ADD COLUMN IF NOT EXISTS cpu_mhz       INT           NULL,
  ADD COLUMN IF NOT EXISTS ram_total_mb  INT           NULL,
  ADD COLUMN IF NOT EXISTS ram_used_mb   INT           NULL,
  ADD COLUMN IF NOT EXISTS disk_count    INT           NULL,
  ADD COLUMN IF NOT EXISTS disk_total_gb DECIMAL(10,1) NULL,
  ADD COLUMN IF NOT EXISTS disk_desc     VARCHAR(300)  NULL,
  ADD COLUMN IF NOT EXISTS pve_version   VARCHAR(60)   NULL,
  ADD COLUMN IF NOT EXISTS kernel        VARCHAR(120)  NULL;

-- Dernier run exploitable : 'partial' compte, des hotes injoignables ne
-- rendent pas la photo inutilisable ; 'error' et 'running' sont exclus.
CREATE OR REPLACE VIEW v_last_run AS
  SELECT * FROM collect_run
  WHERE status IN ('ok', 'partial')
  ORDER BY started_at DESC, id DESC
  LIMIT 1;

CREATE OR REPLACE VIEW v_current_servers AS
  SELECT s.* FROM server_snapshot s
  JOIN v_last_run r ON r.id = s.run_id;

CREATE OR REPLACE VIEW v_current_nodes AS
  SELECT n.* FROM node_snapshot n
  JOIN v_last_run r ON r.id = n.run_id;

CREATE OR REPLACE VIEW v_current_guests AS
  SELECT g.* FROM guest_snapshot g
  JOIN v_last_run r ON r.id = g.run_id;

-- Le croisement qui justifie toute la chaine : chaque VM avec le materiel
-- OVH qui l'heberge.
CREATE OR REPLACE VIEW v_current_inventory AS
  SELECT
    g.name          AS guest_name,
    g.vmid,
    g.guest_type,
    g.status,
    g.cpus,
    g.ram_mb,
    g.disk_gb,
    g.os,
    g.ipv4,
    g.tags,
    g.cluster_name,
    g.pve_node,
    s.ovh_name      AS ovh_server,
    s.offer         AS ovh_offer,
    s.datacenter    AS ovh_datacenter,
    s.expiration    AS ovh_expiration,
    s.cpu_model     AS host_cpu_model,
    s.cpu_cores     AS host_cpu_cores,
    s.cpu_threads   AS host_cpu_threads,
    s.ram_mb        AS host_ram_mb,
    s.disk_count    AS host_disk_count,
    s.disk_desc     AS host_disks
  FROM v_current_guests g
  LEFT JOIN v_current_servers s
    ON s.reverse_name = g.via_host;

-- ---------------------------------------------------------------------------
-- Rentabilite
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_current_costs AS
  SELECT c.* FROM server_cost c
  JOIN v_last_run r ON r.id = c.run_id;

-- Les ventes en cours, ramenees au mois pour etre comparables au cout OVH,
-- qui est mensuel lui aussi.
CREATE OR REPLACE VIEW v_vente_active AS
  SELECT
    id, vm_name, cluster_name, contrat, client, devise,
    prix_vente, periode,
    CASE WHEN periode = 'an' THEN prix_vente / 12 ELSE prix_vente END
      AS vente_mois
  FROM commercial
  WHERE actif = 1
    AND (fin IS NULL OR fin >= CURDATE());

-- Le calcul lui-meme — cumul des enfants, corrections, rattachement des
-- ventes, capacite — vit dans rentabilite.py et non dans des vues.
--
-- Une vue SQL sait sommer un niveau ; elle sait mal cumuler une arborescence
-- de profondeur libre (un domaine dans un vhost dans une VM dans un cluster)
-- et appliquer des corrections a chaque etage. Garder des vues a cote du
-- calcul Python, c'etait deux resultats qui finiraient par diverger, et le
-- tableau de bord contredirait l'export. Il n'y a donc plus qu'un calcul,
-- eprouve sans base par tests_rentabilite.py.
--
-- Les vues de la version precedente sont retirees ici pour qu'une base qui
-- les aurait recues ne garde pas des chiffres que plus rien ne maintient.
DROP VIEW IF EXISTS v_rentabilite_globale;
DROP VIEW IF EXISTS v_rentabilite_serveur;
DROP VIEW IF EXISTS v_rentabilite_cluster;
DROP VIEW IF EXISTS v_serveur_cluster;
DROP VIEW IF EXISTS v_vente_localisee;
DROP VIEW IF EXISTS v_vente_orpheline;
