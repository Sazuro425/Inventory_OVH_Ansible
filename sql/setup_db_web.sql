-- Compte du service web, seul. Sans risque sur une base existante.
--
--   mariadb -u root -p < setup_db_web.sql
--
-- A utiliser quand la base a ete creee par setup_inventory_db.py : rejouer
-- tout setup_db.sql reposerait le mot de passe du compte de collecte au
-- marqueur du fichier, et la collecte ne pourrait plus ouvrir la base.
--
-- Ce fichier ne touche que 'inventory_web'. Remplace le mot de passe avant
-- de le lancer.
--
-- Pourquoi un compte a part : le service web est joignable depuis Apache,
-- donc depuis le reseau. C'est le compte qu'un defaut dans ce service
-- mettrait entre les mains d'un attaquant, et il n'a aucune raison
-- d'atteindre secret_store.

CREATE USER IF NOT EXISTS 'inventory_web'@'localhost'
  IDENTIFIED VIA mysql_native_password USING PASSWORD('MOT_DE_PASSE_WEB_A_DEFINIR');
ALTER USER 'inventory_web'@'localhost'
  IDENTIFIED VIA mysql_native_password USING PASSWORD('MOT_DE_PASSE_WEB_A_DEFINIR');

-- Lecture de l'inventaire. Table par table, jamais inventory.* : un GRANT
-- sur la base entiere couvrirait secret_store, et couvrirait d'office toute
-- table ajoutee plus tard — y compris celle a laquelle personne n'aura pense.
GRANT SELECT ON inventory.v_last_run        TO 'inventory_web'@'localhost';
GRANT SELECT ON inventory.server_snapshot   TO 'inventory_web'@'localhost';
GRANT SELECT ON inventory.node_snapshot     TO 'inventory_web'@'localhost';
GRANT SELECT ON inventory.guest_snapshot    TO 'inventory_web'@'localhost';

GRANT SELECT ON inventory.v_current_servers   TO 'inventory_web'@'localhost';
GRANT SELECT ON inventory.v_current_nodes     TO 'inventory_web'@'localhost';
GRANT SELECT ON inventory.v_current_guests    TO 'inventory_web'@'localhost';
GRANT SELECT ON inventory.v_current_inventory TO 'inventory_web'@'localhost';

-- Jetons a usage unique : emission et consommation. Pas de DELETE — la purge
-- des jetons perimes releve de l'administration, pas du service expose.
GRANT SELECT, INSERT, UPDATE ON inventory.client_token
  TO 'inventory_web'@'localhost';

-- Aucun GRANT sur inventory.secret_store. C'est le but de ce compte.

FLUSH PRIVILEGES;


-- ---------------------------------------------------------------------------
-- Verification : les deux requetes doivent ne rien renvoyer
-- ---------------------------------------------------------------------------

-- Un droit sur la table des secrets.
SELECT grantee, table_name, privilege_type
  FROM information_schema.table_privileges
 WHERE grantee LIKE "'inventory_web'%"
   AND table_name = 'secret_store';

-- Un droit au niveau de la base, qui couvrirait secret_store sans apparaitre
-- dans la requete precedente.
SELECT grantee, privilege_type
  FROM information_schema.schema_privileges
 WHERE grantee LIKE "'inventory_web'%";
