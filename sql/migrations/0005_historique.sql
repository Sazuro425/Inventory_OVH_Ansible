-- 0005 : l'historique, un point par jour.
--
-- La collecte y inscrit, a chaque passage, l'etat du parc entier, de chaque
-- cluster (ou noeud seul) et de chaque serveur : ce qu'il coute et
-- rapporte, et sa capacite allouee contre sa capacite physique. Un passage
-- remplace le point du jour : il en reste un par jour et par carte.
--
-- La photo d'un mois est son dernier point ; la projection de capacite
-- (capacite.py) part des points quotidiens des derniers mois. Rien ne
-- s'efface : quelques dizaines de lignes par jour, de quoi tenir des
-- annees.

CREATE TABLE IF NOT EXISTS historique (
  jour              DATE          NOT NULL,
  niveau            ENUM('parc','cluster','serveur') NOT NULL,
  cle               VARCHAR(190)  NOT NULL,
  nom               VARCHAR(190)  NOT NULL,
  pa                DECIMAL(12,2) NULL,
  pv                DECIMAL(12,2) NULL,
  roi               DECIMAL(12,2) NULL,
  ram_alloue_mb     BIGINT        NULL,
  ram_physique_mb   BIGINT        NULL,
  vcpu_alloue       INT           NULL,
  vcpu_physique     INT           NULL,
  nb_vm             INT           NULL,
  maj_le            DATETIME      NOT NULL,
  PRIMARY KEY (jour, niveau, cle),
  INDEX idx_historique_cle (niveau, cle, jour)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
