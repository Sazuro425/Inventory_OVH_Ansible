-- Compte du service web, seul. Sans risque sur une base existante.
--
--   mariadb -u root -p < setup_db_web.sql
--
-- A utiliser quand la base a ete creee par setup_inventory_db.py : rejouer
-- tout setup_db.sql reposerait le mot de passe du compte de collecte au
-- marqueur du fichier, et la collecte ne pourrait plus ouvrir la base.
--
-- Ce fichier ne touche que 'inventory_web' : il le cree, ou change son mot
-- de passe. Remplace le mot de passe avant de le lancer. Ses droits sont
-- dans sql/droits.sql, a lancer juste apres, puis apres chaque mise a
-- jour du schema.
--
-- Pourquoi un compte a part : le service web est joignable depuis Apache,
-- donc depuis le reseau. C'est le compte qu'un defaut dans ce service
-- mettrait entre les mains d'un attaquant, et il n'a aucune raison
-- d'atteindre secret_store.

CREATE USER IF NOT EXISTS 'inventory_web'@'localhost'
  IDENTIFIED VIA mysql_native_password USING PASSWORD('MOT_DE_PASSE_WEB_A_DEFINIR');
ALTER USER 'inventory_web'@'localhost'
  IDENTIFIED VIA mysql_native_password USING PASSWORD('MOT_DE_PASSE_WEB_A_DEFINIR');

FLUSH PRIVILEGES;

-- Suite : mariadb < sql/droits.sql
