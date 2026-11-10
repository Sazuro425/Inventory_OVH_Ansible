-- 0002 : une VM se designe par sa reference cluster/vmid, plus par son nom.
--
-- Le nom change (renommage), deux clusters peuvent avoir deux VM homonymes ;
-- le couple (cluster, vmid) est unique dans un snapshot (uq_guest) et
-- survit a un renommage comme a une migration de noeud. La reference
-- s'ecrit "prod-1/105", ou "standalone:ns1.eu/100" pour un noeud seul.
--
-- Une valeur sans "/" reste lue comme un nom, par rentabilite.py : rien ne
-- se perd si une saisie n'a pas pu etre convertie ici.

ALTER TABLE commercial
  ADD COLUMN IF NOT EXISTS vm_ref VARCHAR(190) NULL AFTER vm_name;
ALTER TABLE commercial
  ADD INDEX IF NOT EXISTS idx_vente_ref (vm_ref, actif);

CREATE OR REPLACE VIEW v_vente_active AS
  SELECT
    id, vm_name, vm_ref, cluster_name, contrat, client, devise,
    prix_vente, periode,
    CASE WHEN periode = 'an' THEN prix_vente / 12 ELSE prix_vente END
      AS vente_mois
  FROM commercial
  WHERE actif = 1
    AND (fin IS NULL OR fin >= CURDATE());

-- Les saisies existantes : un nom porte par une seule VM du dernier
-- snapshot devient sa reference. Un nom ambigu (homonymes) ou introuvable
-- reste tel quel, et continue d'etre resolu par le nom.
--
-- La meme sous-requete dans chaque UPDATE plutot qu'une table temporaire :
-- celle-ci demanderait au compte de collecte un droit de plus
-- (CREATE TEMPORARY TABLES), pour un usage unique.

UPDATE commercial c JOIN (SELECT g.name AS nom,
           MIN(CONCAT(COALESCE(g.cluster_name, CONCAT('standalone:', g.via_host)),
                      '/', g.vmid)) AS ref
      FROM guest_snapshot g JOIN v_last_run r ON r.id = g.run_id
     WHERE g.name IS NOT NULL
     GROUP BY g.name HAVING COUNT(*) = 1) u ON u.nom = c.vm_name
   SET c.vm_ref = u.ref
 WHERE c.vm_ref IS NULL;

UPDATE element e JOIN (SELECT g.name AS nom,
           MIN(CONCAT(COALESCE(g.cluster_name, CONCAT('standalone:', g.via_host)),
                      '/', g.vmid)) AS ref
      FROM guest_snapshot g JOIN v_last_run r ON r.id = g.run_id
     WHERE g.name IS NOT NULL
     GROUP BY g.name HAVING COUNT(*) = 1) u ON u.nom = e.parent_ref
   SET e.parent_ref = u.ref
 WHERE e.parent_type = 'vm';

-- IGNORE : une correction ou un role deja pose sous la reference garde la
-- place ; l'ancienne ligne par nom reste, et le calcul la lit encore.
UPDATE IGNORE surcharge s JOIN (SELECT g.name AS nom,
           MIN(CONCAT(COALESCE(g.cluster_name, CONCAT('standalone:', g.via_host)),
                      '/', g.vmid)) AS ref
      FROM guest_snapshot g JOIN v_last_run r ON r.id = g.run_id
     WHERE g.name IS NOT NULL
     GROUP BY g.name HAVING COUNT(*) = 1) u ON u.nom = s.cible_ref
   SET s.cible_ref = u.ref
 WHERE s.cible_type = 'vm';

UPDATE IGNORE role_carte rc JOIN (SELECT g.name AS nom,
           MIN(CONCAT(COALESCE(g.cluster_name, CONCAT('standalone:', g.via_host)),
                      '/', g.vmid)) AS ref
      FROM guest_snapshot g JOIN v_last_run r ON r.id = g.run_id
     WHERE g.name IS NOT NULL
     GROUP BY g.name HAVING COUNT(*) = 1) u ON u.nom = rc.cible_ref
   SET rc.cible_ref = u.ref
 WHERE rc.cible_type = 'vm';
