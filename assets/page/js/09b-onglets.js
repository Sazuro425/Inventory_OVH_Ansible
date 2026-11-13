// ------------------------------------------------------------------ onglets
// Quatre vues : Rentabilite (par defaut), Schema, Jetons PVE et Comptes
// (admins seulement ; Comptes en mode comptes locaux). L'onglet choisi est
// garde par navigateur : une commodite, rien qui doive survivre ailleurs.
const onglet = { courant: null };

function ongletDispo(nom) {
  if (nom === 'rent') return commerce.dispo;
  if (nom === 'schema') return true;
  if (nom === 'jetons') return droits.admin;
  if (nom === 'comptes') return droits.admin && droits.locale;
  return false;
}

function majOnglets() {
  document.querySelectorAll('#onglets button').forEach(b => {
    b.hidden = !ongletDispo(b.dataset.onglet);
    b.classList.toggle('actif', b.dataset.onglet === onglet.courant);
  });
}

function ongletMemorise() {
  try { return localStorage.getItem(CLE + ':onglet') || 'rent'; } catch (e) { return 'rent'; }
}

function ouvrirOnglet(nom) {
  // un onglet indisponible (role retire, service muet) retombe sur le defaut
  if (!ongletDispo(nom)) nom = commerce.dispo ? 'rent' : 'schema';
  onglet.courant = nom;
  // un mot de passe genere ne survit pas a la sortie de l'onglet Comptes
  if (nom !== 'comptes') comptes.mdp = null;
  document.body.classList.toggle('hors-schema', nom !== 'schema');
  document.body.classList.toggle('mode-jetons', nom === 'jetons');
  document.body.classList.toggle('mode-comptes', nom === 'comptes');
  if (nom === 'rent') {
    if (!vueRent.actif) basculerRent(true);
  } else if (vueRent.actif) {
    basculerRent(false);
  } else {
    fermerTiroir();
  }
  majOnglets();
  if (nom === 'jetons') dessinerJetons();
  if (nom === 'comptes') dessinerComptes();
  try { localStorage.setItem(CLE + ':onglet', nom); } catch (e) { /* navigation privee */ }
}

document.querySelectorAll('#onglets button').forEach(b => {
  b.onclick = () => ouvrirOnglet(b.dataset.onglet);
});
