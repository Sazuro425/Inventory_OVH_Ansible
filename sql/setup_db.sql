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


-- ---------------------------------------------------------------------------
-- Compte du service web
-- ---------------------------------------------------------------------------
-- Mot de passe distinct de celui de la collecte : deux comptes partageant le
-- meme mot de passe ne sont qu'un seul compte avec deux noms.

CREATE USER IF NOT EXISTS 'inventory_web'@'localhost'
  IDENTIFIED VIA mysql_native_password USING PASSWORD('MOT_DE_PASSE_WEB_A_DEFINIR');
ALTER USER 'inventory_web'@'localhost'
  IDENTIFIED VIA mysql_native_password USING PASSWORD('MOT_DE_PASSE_WEB_A_DEFINIR');

-- Lecture de l'inventaire. Table par table, jamais inventory.* : un GRANT sur
-- la base entiere couvrirait secret_store, et couvrirait d'office toute table
-- ajoutee plus tard — y compris celle a laquelle personne n'aura pense.
GRANT SELECT ON inventory.v_last_run        TO 'inventory_web'@'localhost';
GRANT SELECT ON inventory.server_snapshot   TO 'inventory_web'@'localhost';
GRANT SELECT ON inventory.node_snapshot     TO 'inventory_web'@'localhost';
GRANT SELECT ON inventory.guest_snapshot    TO 'inventory_web'@'localhost';

-- Les vues v_current_* ne sont pas lues par le service aujourd'hui, mais
-- servent aux requetes d'exploitation a la main sous ce compte.
GRANT SELECT ON inventory.v_current_servers   TO 'inventory_web'@'localhost';
GRANT SELECT ON inventory.v_current_nodes     TO 'inventory_web'@'localhost';
GRANT SELECT ON inventory.v_current_guests    TO 'inventory_web'@'localhost';
GRANT SELECT ON inventory.v_current_inventory TO 'inventory_web'@'localhost';

-- Jetons a usage unique : emission et consommation. Pas de DELETE — la purge
-- des jetons perimes releve de l'administration, pas du service expose.
GRANT SELECT, INSERT, UPDATE ON inventory.client_token
  TO 'inventory_web'@'localhost';

-- Aucun GRANT sur inventory.secret_store. C'est le but de ce compte.
-- A verifier apres toute evolution du schema (voir la requete en fin de
-- fichier) : un GRANT trop large se remarque mal a la relecture.

FLUSH PRIVILEGES;


-- ---------------------------------------------------------------------------
-- Verification
-- ---------------------------------------------------------------------------

SELECT user, host, plugin FROM mysql.user
 WHERE user IN ('inventory', 'inventory_web');

-- Doit ne rien renvoyer : le compte web n'a aucun droit sur les secrets.
-- Une ligne ici signifie que la separation ne tient pas.
SELECT grantee, table_name, privilege_type
  FROM information_schema.table_privileges
 WHERE grantee LIKE "'inventory_web'%"
   AND table_name = 'secret_store';

-- Doit ne rien renvoyer non plus : un droit au niveau de la base couvrirait
-- secret_store sans apparaitre dans la requete precedente.
SELECT grantee, privilege_type
  FROM information_schema.schema_privileges
 WHERE grantee LIKE "'inventory_web'%";
