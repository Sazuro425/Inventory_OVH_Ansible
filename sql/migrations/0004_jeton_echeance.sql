-- 0004 : l'echeance d'un jeton PVE depose.
--
-- Un jeton sans fin reste valable pour toujours, y compris oublie sur un
-- hyperviseur rendu. La page propose une echeance (un an par defaut) et la
-- commande pveum qui la pose (--expire) ; la date est gardee avec le depot,
-- et la vue Recolte signale ce qui expire sous 30 jours.
--
-- NULL : echeance inconnue (depot anterieur, ou jeton pose autrement).

ALTER TABLE depot_jeton
  ADD COLUMN IF NOT EXISTS expire_le DATE NULL AFTER depose_par;
