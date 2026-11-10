-- Droits du compte du service web, et rien d'autre.
--
--   mariadb < sql/droits_web.sql
--
-- A rejouer apres chaque mise a jour du schema (inventory_cli.py --migrer) :
-- une table ajoutee n'est lisible du service qu'une fois son GRANT pose.
-- Aucun mot de passe ici, le fichier se rejoue sans rien savoir du compte,
-- que sql/setup_db_web.sql a cree une fois pour toutes.

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

-- Cout OVH par serveur, en lecture seule : c'est la collecte qui l'ecrit.
GRANT SELECT ON inventory.server_cost     TO 'inventory_web'@'localhost';
GRANT SELECT ON inventory.v_current_costs TO 'inventory_web'@'localhost';

-- Rentabilite : le calcul est fait par rentabilite.py, qui ne lit que les
-- tables ci-dessus et les saisies ci-dessous. Une seule vue, qui ramene les
-- ventes au mois.
GRANT SELECT ON inventory.v_vente_active TO 'inventory_web'@'localhost';

-- Saisies humaines : ventes, elements declares, corrections. SELECT, INSERT,
-- UPDATE, et pas DELETE : une resiliation ou un retrait repasse actif a 0,
-- une correction levee repasse sa valeur a NULL. Ce qui a ete facture doit
-- rester lisible, et un defaut du service web ne doit pas pouvoir faire
-- disparaitre l'historique.
GRANT SELECT, INSERT, UPDATE ON inventory.commercial
  TO 'inventory_web'@'localhost';
GRANT SELECT, INSERT, UPDATE ON inventory.element
  TO 'inventory_web'@'localhost';
GRANT SELECT, INSERT, UPDATE ON inventory.surcharge
  TO 'inventory_web'@'localhost';

-- Roles : le catalogue s'etend depuis la page (INSERT), un role retire
-- repasse actif a 0 (UPDATE) ; l'attribution a une carte se pose et se leve
-- comme une correction.
GRANT SELECT, INSERT, UPDATE ON inventory.role
  TO 'inventory_web'@'localhost';
GRANT SELECT, INSERT, UPDATE ON inventory.role_carte
  TO 'inventory_web'@'localhost';

-- Comptes locaux : le service verifie un mot de passe, compte les echecs,
-- enregistre un changement (SELECT, UPDATE). Il ne cree aucun compte : pas
-- d'INSERT, c'est inventory_cli.py --compte-local, sous le compte de
-- collecte, qui le fait. Les sessions, elles, naissent ici.
GRANT SELECT, UPDATE ON inventory.compte_local
  TO 'inventory_web'@'localhost';
GRANT SELECT, INSERT, UPDATE ON inventory.session_locale
  TO 'inventory_web'@'localhost';

-- Boite de depot des jetons PVE : la clef publique en lecture, le depot en
-- INSERT. Ni UPDATE (l'etat d'un depot est l'affaire de la collecte), ni
-- rien sur secret_store, ou atterrit le jeton une fois teste.
GRANT SELECT ON inventory.cle_depot
  TO 'inventory_web'@'localhost';
GRANT SELECT, INSERT ON inventory.depot_jeton
  TO 'inventory_web'@'localhost';

-- Journal des saisies : INSERT et SELECT seulement. Le service ecrit la
-- trace de chaque saisie, il ne peut ni la corriger ni l'effacer — meme
-- entre les mains d'un attaquant.
GRANT SELECT, INSERT ON inventory.journal_saisie
  TO 'inventory_web'@'localhost';

-- Vues de rentabilite enregistrees : des filtres nommes, pas des donnees.
-- Meme regle que le reste, pas de DELETE.
GRANT SELECT, INSERT, UPDATE ON inventory.vue_rentabilite
  TO 'inventory_web'@'localhost';


-- Jetons a usage unique : emission et consommation. Pas de DELETE — la purge
-- des jetons perimes est faite par la collecte (purger_jetons_clients).
GRANT SELECT, INSERT, UPDATE ON inventory.client_token
  TO 'inventory_web'@'localhost';

-- Version du schema : /api/sante dit si la base est en retard sur le code.
GRANT SELECT ON inventory.schema_version
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

-- Un DELETE ou un DROP ou que ce soit : ce compte ne detruit rien.
SELECT grantee, table_name, privilege_type
  FROM information_schema.table_privileges
 WHERE grantee LIKE "'inventory_web'%"
   AND privilege_type IN ('DELETE', 'DROP', 'ALTER', 'CREATE');


-- ---------------------------------------------------------------------------
-- Verification : les tables en ecriture doivent etre exactement
--   client_token, commercial, element, role, role_carte,
--   session_locale, surcharge, vue_rentabilite      INSERT,UPDATE
--   compte_local                                    UPDATE
--   depot_jeton, journal_saisie                     INSERT
-- ---------------------------------------------------------------------------

SELECT table_name, GROUP_CONCAT(privilege_type ORDER BY privilege_type) AS droits
  FROM information_schema.table_privileges
 WHERE grantee LIKE "'inventory_web'%"
   AND privilege_type <> 'SELECT'
 GROUP BY table_name;
