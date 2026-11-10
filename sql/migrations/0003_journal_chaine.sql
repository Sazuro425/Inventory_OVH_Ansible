-- 0003 : le journal des saisies devient une chaine.
--
-- Chaque ligne porte son rang (n), l'empreinte de la ligne precedente et la
-- sienne : sha256 de son contenu et de l'empreinte precedente. Modifier ou
-- effacer une ligne, meme en administrateur de la base, casse la chaine a
-- cet endroit, et `inventory_cli.py --verifier-journal` le dit.
--
-- journal_tete garde le dernier maillon. Ecrire une ligne verrouille cette
-- unique ligne (SELECT ... FOR UPDATE) : deux saisies simultanees se
-- suivent au lieu de partir du meme maillon. L'index unique sur n le
-- garantit en dernier ressort.
--
-- Les lignes d'avant cette migration restent hors chaine (n a NULL).

ALTER TABLE journal_saisie
  ADD COLUMN IF NOT EXISTS n          BIGINT    NULL AFTER id,
  ADD COLUMN IF NOT EXISTS precedente CHAR(64)  NULL,
  ADD COLUMN IF NOT EXISTS empreinte  CHAR(64)  NULL;
ALTER TABLE journal_saisie
  ADD UNIQUE INDEX IF NOT EXISTS uq_journal_n (n);

CREATE TABLE IF NOT EXISTS journal_tete (
  id         TINYINT   NOT NULL PRIMARY KEY,
  n          BIGINT    NOT NULL,
  empreinte  CHAR(64)  NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO journal_tete (id, n, empreinte)
  VALUES (1, 0, REPEAT('0', 64));
