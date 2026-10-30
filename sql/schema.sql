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
