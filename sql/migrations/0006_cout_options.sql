-- 0006 : le cout reel d'un serveur, options comprises.
--
-- Jusqu'ici, server_cost ne gardait que le prix de l'offre nue. Les options
-- OVH (RAM ou disques ajoutes, bande passante, IP supplementaires,
-- licences...) sont des services a part, rattaches au serveur
-- (/services/{id}/options) : le PA etait sous-estime d'autant.
--
-- price_month devient le total facture, ramene au mois : l'offre et ses
-- options. Le detail reste a cote : base_month, options_month, et chaque
-- option (libelle, plan, prix au mois) en JSON, pour la fiche.
--
-- Rejouable : IF NOT EXISTS sur chaque colonne.

ALTER TABLE server_cost
  ADD COLUMN IF NOT EXISTS base_month    DECIMAL(10,2) NULL AFTER price_month,
  ADD COLUMN IF NOT EXISTS options_month DECIMAL(10,2) NULL AFTER base_month,
  ADD COLUMN IF NOT EXISTS options_json  TEXT          NULL AFTER options_month;
