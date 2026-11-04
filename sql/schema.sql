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

-- Un serveur et son resultat : ce qu'il coute, ce que ses VM rapportent.
-- LEFT JOIN partout : un serveur sans cout releve, ou sans aucune VM vendue,
-- doit apparaitre quand meme — c'est precisement celui qu'on cherche.
CREATE OR REPLACE VIEW v_rentabilite_serveur AS
  SELECT
    s.ovh_name,
    s.reverse_name,
    s.offer,
    s.datacenter,
    s.expiration,
    k.price_month                           AS cout_mois,
    k.price_currency                        AS devise,
    COALESCE(SUM(v.vente_mois), 0)          AS vente_mois,
    COALESCE(SUM(v.vente_mois), 0) - COALESCE(k.price_month, 0)
                                            AS marge_mois,
    COUNT(DISTINCT g.id)                    AS nb_invites,
    COUNT(DISTINCT v.contrat)               AS nb_contrats
  FROM v_current_servers s
  LEFT JOIN v_current_costs  k ON k.ovh_name = s.ovh_name
  LEFT JOIN v_current_guests g ON g.via_host = s.reverse_name
  LEFT JOIN v_vente_active   v ON v.vm_name  = g.name
  GROUP BY s.ovh_name, s.reverse_name, s.offer, s.datacenter, s.expiration,
           k.price_month, k.price_currency;

-- Contrats actifs dont la VM n'est plus dans l'inventaire : soit la VM a ete
-- detruite sans resilier, soit elle a ete renommee. Dans les deux cas on
-- facture quelque chose qui n'existe pas.
CREATE OR REPLACE VIEW v_vente_orpheline AS
  SELECT v.*
  FROM v_vente_active v
  LEFT JOIN v_current_guests g ON g.name = v.vm_name
  WHERE g.id IS NULL;

-- Le total du parc, en une ligne.
CREATE OR REPLACE VIEW v_rentabilite_globale AS
  SELECT
    (SELECT COALESCE(SUM(price_month), 0) FROM v_current_costs)
      AS cout_mois,
    (SELECT COUNT(*) FROM v_current_costs WHERE price_month IS NULL)
      AS serveurs_sans_cout,
    (SELECT COALESCE(SUM(vente_mois), 0) FROM v_rentabilite_serveur)
      AS vente_mois,
    (SELECT COALESCE(SUM(vente_mois), 0) FROM v_rentabilite_serveur)
      - (SELECT COALESCE(SUM(price_month), 0) FROM v_current_costs)
      AS marge_mois,
    (SELECT COUNT(*) FROM v_vente_active)        AS nb_contrats,
    (SELECT COUNT(*) FROM v_vente_orpheline)     AS nb_orphelins,
    (SELECT COUNT(*) FROM v_rentabilite_serveur WHERE marge_mois < 0)
      AS serveurs_a_perte;
