-- Creation de la base et des comptes applicatifs de l'inventaire.
-- A lancer en root :  mariadb -u root -p < setup_db.sql
--
-- ATTENTION si la base existe deja : les ALTER USER ci-dessous reposent les
-- mots de passe aux valeurs de ce fichier. Sur une installation faite avec
-- setup_inventory_db.py, rejouer tout le fichier ecraserait le mot de passe
-- du compte de collecte par le marqueur. Dans ce cas, ne reprendre que le
-- bloc « Compte du service web ».
-- Remplace MOT_DE_PASSE_A_DEFINIR avant execution, ou utilise
-- setup_inventory_db.py qui en genere un.
--
-- Le plugin est force a mysql_native_password : le plugin par defaut du
-- serveur peut etre gssapi (authentification Windows), que les clients
-- Python ne savent pas negocier.
--
-- Deux comptes, parce qu'ils n'ont pas les memes besoins ni la meme exposition.
--
--   inventory      la collecte. Ecrit les snapshots, lit et ecrit les secrets
--                  chiffres. Tourne derriere un timer systemd, sans facade.
--
--   inventory_web  le service HTTP. Lit l'inventaire, gere les jetons, et
--                  **n'a aucun droit sur secret_store**. Il est joignable
--                  depuis Apache, donc depuis le reseau : c'est le compte
--                  qu'un defaut dans le service web mettrait entre les mains
--                  d'un attaquant.
--
-- Les valeurs de secret_store sont chiffrees, donc inutilisables sans la clef
-- maitresse — que seul le service de collecte possede. Refuser la table au
-- compte web ne remplace pas ce chiffrement, il evite d'avoir a compter
-- dessus : deux barrieres plutot qu'une.

CREATE DATABASE IF NOT EXISTS inventory
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- Compte de collecte
-- ---------------------------------------------------------------------------

-- Depuis la machine locale (collecte sur le meme hote que la base)
CREATE USER IF NOT EXISTS 'inventory'@'localhost'
  IDENTIFIED VIA mysql_native_password USING PASSWORD('MOT_DE_PASSE_A_DEFINIR');
ALTER USER 'inventory'@'localhost'
  IDENTIFIED VIA mysql_native_password USING PASSWORD('MOT_DE_PASSE_A_DEFINIR');
-- Droits d'ecriture des donnees. Pas de DELETE sur la base entiere : il
-- l'a sur les deux seules tables qu'il purge (sql/droits.sql, apres le
-- schema). Le GRANT puis REVOKE retire un DELETE global laisse par une
-- installation anterieure, sans echouer s'il n'y en avait pas.
GRANT SELECT, INSERT, UPDATE ON inventory.* TO 'inventory'@'localhost';
GRANT DELETE ON inventory.* TO 'inventory'@'localhost';
REVOKE DELETE ON inventory.* FROM 'inventory'@'localhost';

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


-- ---------------------------------------------------------------------------
-- Compte du service web : PAS ICI
-- ---------------------------------------------------------------------------
-- Il est cree par setup_db_web.sql, a lancer APRES schema.sql. Ses droits
-- sont accordes table par table, et MariaDB refuse un GRANT sur une table
-- ou une vue qui n'existe pas encore (erreur 1146) : les poser ici, avant
-- le schema, faisait echouer ce fichier.
--
--   mariadb < sql/setup_db.sql
--   mariadb inventory < sql/schema.sql
--   mariadb < sql/setup_db_web.sql

FLUSH PRIVILEGES;


-- ---------------------------------------------------------------------------
-- Verification
-- ---------------------------------------------------------------------------

SELECT user, host, plugin FROM mysql.user
 WHERE user = 'inventory';

-- La separation des comptes se verifie en fin de setup_db_web.sql.
