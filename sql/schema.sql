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

-- Contrats actifs dont la VM n'est plus dans l'inventaire : soit la VM a ete
-- detruite sans resilier, soit elle a ete renommee. Dans les deux cas on
-- facture quelque chose qui n'existe pas.
CREATE OR REPLACE VIEW v_vente_orpheline AS
  SELECT v.*
  FROM v_vente_active v
  LEFT JOIN v_current_guests g ON g.name = v.vm_name
  WHERE g.id IS NULL;

-- Chaque contrat rattache a UN cluster, celui de sa VM. Passer par la vente
-- et non par l'invite est volontaire : deux invites peuvent porter le meme
-- nom dans deux clusters, et sommer depuis les invites compterait alors le
-- contrat deux fois. MIN() tranche, et la somme des ventes par cluster reste
-- egale au total des contrats non orphelins — ce qui se verifie.
CREATE OR REPLACE VIEW v_vente_localisee AS
  SELECT v.*,
         (SELECT MIN(g.cluster_name) FROM v_current_guests g
           WHERE g.name = v.vm_name) AS cluster_effectif
  FROM v_vente_active v;

-- Quel dedie porte quel cluster. C'est la vue que regarde un technicien :
-- une ligne par serveur, avec son noeud PVE, son cluster et son cout propre.
--
-- Un hote jamais sonde ou injoignable n'a pas de cluster connu. Lui en
-- fabriquer un, 'inconnu:<reverse>', le garde isole : son cout continue de
-- compter sans etre verse dans le resultat d'un cluster auquel rien ne dit
-- qu'il appartient.
CREATE OR REPLACE VIEW v_serveur_cluster AS
  SELECT
    s.ovh_name,
    s.reverse_name,
    s.offer,
    s.datacenter,
    s.expiration,
    n.pve_node,
    COALESCE(n.reachable, 0)                AS reachable,
    COALESCE(n.cluster_name,
             CONCAT('inconnu:', s.reverse_name))  AS cluster_name,
    (n.cluster_name IS NULL
       OR n.cluster_name LIKE 'standalone:%')     AS seul,
    k.price_month                           AS cout_mois,
    k.price_currency                        AS devise
  FROM v_current_servers s
  LEFT JOIN v_current_nodes n ON n.host     = s.reverse_name
  LEFT JOIN v_current_costs k ON k.ovh_name = s.ovh_name;

-- Le resultat se calcule par cluster, pas par serveur.
--
-- Un cluster de quatre noeuds est une unite economique unique : une VM y
-- migre librement, elle n'appartient a aucun noeud. La collecte n'interroge
-- d'ailleurs qu'un representant par cluster, donc tous les invites portent
-- le via_host de ce seul hote. Rapporter les ventes au serveur, c'est faire
-- porter a un noeud la recette de ses trois freres, qui affichent alors leur
-- cout face a zero et passent pour etre a perte. Sur un parc de 30 dedies en
-- clusters de 2 a 4, cela designe une vingtaine de serveurs a perte sur un
-- parc parfaitement sain.
--
-- Un noeud seul est un cluster d'un seul membre : les deux cas se traitent
-- donc de la meme facon, et rien n'echappe au calcul.
CREATE OR REPLACE VIEW v_rentabilite_cluster AS
  SELECT
    x.cluster_name,
    x.seul,
    x.nb_serveurs,
    x.serveurs,
    x.datacenters,
    x.cout_mois,
    x.serveurs_sans_cout,
    x.devise,
    COALESCE(i.nb_invites, 0)                              AS nb_invites,
    COALESCE(w.nb_contrats, 0)                             AS nb_contrats,
    COALESCE(w.vente_mois, 0)                              AS vente_mois,
    COALESCE(w.vente_mois, 0) - COALESCE(x.cout_mois, 0)   AS marge_mois
  FROM (
    SELECT cluster_name,
           MAX(seul)                  AS seul,
           COUNT(*)                   AS nb_serveurs,
           GROUP_CONCAT(reverse_name ORDER BY reverse_name SEPARATOR ' ')
                                      AS serveurs,
           GROUP_CONCAT(DISTINCT datacenter ORDER BY datacenter SEPARATOR ' ')
                                      AS datacenters,
           SUM(cout_mois)             AS cout_mois,
           SUM(cout_mois IS NULL)     AS serveurs_sans_cout,
           MIN(devise)                AS devise
      FROM v_serveur_cluster
     GROUP BY cluster_name
  ) x
  LEFT JOIN (
    SELECT cluster_name, COUNT(*) AS nb_invites
      FROM v_current_guests GROUP BY cluster_name
  ) i ON i.cluster_name = x.cluster_name
  LEFT JOIN (
    SELECT cluster_effectif,
           COUNT(*)         AS nb_contrats,
           SUM(vente_mois)  AS vente_mois
      FROM v_vente_localisee
     WHERE cluster_effectif IS NOT NULL
     GROUP BY cluster_effectif
  ) w ON w.cluster_effectif = x.cluster_name;

-- Une ligne par serveur : son cout propre, et le resultat du cluster dont il
-- fait partie. Pas de marge par serveur : elle n'a pas de sens, et en
-- afficher une serait reinstaller l'erreur qu'on vient d'ecarter.
CREATE OR REPLACE VIEW v_rentabilite_serveur AS
  SELECT
    c.ovh_name,
    c.reverse_name,
    c.offer,
    c.datacenter,
    c.expiration,
    c.pve_node,
    c.reachable,
    c.cluster_name,
    c.seul,
    c.cout_mois,
    c.devise,
    r.nb_serveurs   AS cluster_nb_serveurs,
    r.serveurs      AS cluster_serveurs,
    r.cout_mois     AS cluster_cout_mois,
    r.vente_mois    AS cluster_vente_mois,
    r.marge_mois    AS cluster_marge_mois,
    r.nb_invites    AS cluster_nb_invites,
    r.nb_contrats   AS cluster_nb_contrats
  FROM v_serveur_cluster c
  LEFT JOIN v_rentabilite_cluster r ON r.cluster_name = c.cluster_name;

-- Le total du parc, en une ligne.
--
-- vente_mois compte TOUS les contrats actifs, orphelins compris : un contrat
-- dont la VM a disparu est facture quand meme, et l'omettre ferait paraitre
-- le parc moins rentable qu'il ne l'est. vente_hors_parc isole ce montant
-- pour que l'ecart avec la somme des clusters soit lisible au lieu d'etre
-- une difference inexpliquee.
CREATE OR REPLACE VIEW v_rentabilite_globale AS
  SELECT
    (SELECT COALESCE(SUM(cout_mois), 0) FROM v_serveur_cluster)
      AS cout_mois,
    (SELECT COUNT(*) FROM v_serveur_cluster WHERE cout_mois IS NULL)
      AS serveurs_sans_cout,
    (SELECT COALESCE(SUM(vente_mois), 0) FROM v_vente_active)
      AS vente_mois,
    (SELECT COALESCE(SUM(vente_mois), 0) FROM v_vente_active)
      - (SELECT COALESCE(SUM(cout_mois), 0) FROM v_serveur_cluster)
      AS marge_mois,
    (SELECT COALESCE(SUM(vente_mois), 0) FROM v_vente_orpheline)
      AS vente_hors_parc,
    (SELECT COUNT(*) FROM v_serveur_cluster)     AS nb_serveurs,
    (SELECT COUNT(*) FROM v_rentabilite_cluster) AS nb_clusters,
    (SELECT COUNT(*) FROM v_vente_active)        AS nb_contrats,
    (SELECT COUNT(*) FROM v_vente_orpheline)     AS nb_orphelins,
    (SELECT COUNT(*) FROM v_rentabilite_cluster WHERE marge_mois < 0)
      AS clusters_a_perte;
