-- Creation de la base et du compte applicatif de l'inventaire.
-- A lancer en root :  mariadb -u root -p < setup_db.sql
-- Remplace MOT_DE_PASSE_A_DEFINIR avant execution, ou utilise
-- setup_inventory_db.py qui en genere un.
--
-- Le plugin est force a mysql_native_password : le plugin par defaut du
-- serveur peut etre gssapi (authentification Windows), que les clients
-- Python ne savent pas negocier.
--
-- Le compte n'a que ce qu'il faut pour ecrire des snapshots et les relire :
-- rien en dehors de la base inventory.

CREATE DATABASE IF NOT EXISTS inventory
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Depuis la machine locale (collecte sur le meme hote que la base)
CREATE USER IF NOT EXISTS 'inventory'@'localhost'
  IDENTIFIED VIA mysql_native_password USING PASSWORD('MOT_DE_PASSE_A_DEFINIR');
ALTER USER 'inventory'@'localhost'
  IDENTIFIED VIA mysql_native_password USING PASSWORD('MOT_DE_PASSE_A_DEFINIR');
-- Droits d'ecriture des donnees
GRANT SELECT, INSERT, UPDATE, DELETE ON inventory.* TO 'inventory'@'localhost';

-- Droits de structure : necessaires seulement si tu veux que les scripts
-- creent la base et les tables tout seuls (db.auto_schema dans infra.yml).
-- Ils restent limites a la base inventory, mais ils permettent au compte de
-- supprimer ses propres tables. Si tu preferes que seul un admin touche a la
-- structure, commente les deux lignes et mets auto_schema: false.
GRANT CREATE, CREATE VIEW, DROP, INDEX, REFERENCES, ALTER
  ON inventory.* TO 'inventory'@'localhost';

-- Depuis WSL ou une autre machine du reseau : decommente et ajuste l'origine
-- ('172.28.%.%' couvre WSL2, dont l'IP change a chaque demarrage)
-- CREATE USER IF NOT EXISTS 'inventory'@'172.28.%.%'
--   IDENTIFIED VIA mysql_native_password USING PASSWORD('MOT_DE_PASSE_A_DEFINIR');
-- GRANT SELECT, INSERT, UPDATE, DELETE ON inventory.* TO 'inventory'@'172.28.%.%';

FLUSH PRIVILEGES;

-- Verification
SELECT user, host, plugin FROM mysql.user WHERE user = 'inventory';
