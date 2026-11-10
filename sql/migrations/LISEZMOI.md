# Migrations du schema

Une migration par evolution, nommee `NNNN_nom.sql` (0002, 0003...) : la
version 1 est `../schema.sql`. `inventory_cli.py --migrer` applique, dans
l'ordre, celles qui manquent a la base et les inscrit dans `schema_version`.

- Rejouable : `IF NOT EXISTS`, `IF EXISTS`. Le DDL de MariaDB n'est pas
  transactionnel, une migration interrompue doit pouvoir repartir.
- Jamais modifiee une fois livree : on en ecrit une nouvelle.
- Pas de GRANT : les droits par table des deux comptes vont dans `../droits.sql`,
  rejoue en root apres chaque migration.
- `schema.sql` reste a jour aussi : une installation neuve doit aboutir au
  meme schema qu'une base migree (tests_mariadb.py le verifie).
