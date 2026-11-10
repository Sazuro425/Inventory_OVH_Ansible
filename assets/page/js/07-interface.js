// ----------------------------------------------------------------- filtres
function construireFiltres() {
  const zone = document.getElementById('filtres');
  zone.replaceChildren();
  for (const f of DONNEES.filtres) {
    const grp = document.createElement('div');
    grp.className = 'groupe';
    grp.appendChild(Object.assign(document.createElement('label'),
                                  { textContent: f.libelle }));
    etat.filtres[f.cle] = etat.filtres[f.cle] || new Set();
    for (const v of f.valeurs) {
      const puce = document.createElement('span');
      puce.className = 'puce';
      if (etat.filtres[f.cle].has(String(v.val))) puce.classList.add('off');
      if (v.couleur) {
        const i = document.createElement('i');
        i.style.background = v.couleur;
        puce.appendChild(i);
      }
      puce.appendChild(document.createTextNode(v.libelle));
      const n = document.createElement('span');
      n.className = 'n'; n.textContent = v.n;
      puce.appendChild(n);
      puce.onclick = () => {
        const s = etat.filtres[f.cle];
        s.has(String(v.val)) ? s.delete(String(v.val)) : s.add(String(v.val));
        puce.classList.toggle('off');
        sauver(); dessiner();
      };
      grp.appendChild(puce);
    }
    zone.appendChild(grp);
  }
}

// ------------------------------------------------------------------ en-tete
function goDepuisMo(mo) {
  return (Math.round(mo / 1024 * 10) / 10).toLocaleString('fr-FR') + ' Go';
}

function totaux(filtre) {
  // Le pliage n'entre pas en compte : replier une branche est un confort
  // d'affichage, pas un filtre. Seuls les filtres et la recherche reduisent
  // le perimetre, sinon les totaux bougeraient en ouvrant une branche.
  const t = { dedies: 0, hyperviseurs: 0, invites: 0, marche: 0,
              vcpu: 0, ram: 0, sansAcces: 0 };
  for (const n of DONNEES.noeuds) {
    if (n.hors_parc) continue;
    if (filtre && !(passeFiltres(n) && correspond(n))) continue;
    const m = n.mesures || {};
    if (n.kind === 'srv') {
      t.dedies++;
      if (n.meta && n.meta.acces === 'manquant') t.sansAcces++;
    } else if (n.kind === 'node') {
      if (n.etat !== 'unreachable') t.hyperviseurs++;
    } else if (n.kind === 'vm') {
      t.invites++;
      if (n.etat === 'running') t.marche++;
      t.vcpu += m.cpus || 0;
      t.ram += m.ram_mb || 0;
      if (n.meta && n.meta.acces === 'manquant') t.sansAcces++;
    }
  }
  return t;
}

function filtreActif() {
  if (etat.recherche) return true;
  return Object.values(etat.filtres).some(s => s.size);
}

function construireStats() {
  const z = document.getElementById('stats');
  const actif = filtreActif();
  const vu = totaux(true);
  const tout = actif ? totaux(false) : vu;
  const lignes = [
    ['Dédiés', vu.dedies, tout.dedies],
    ['Hyperviseurs', vu.hyperviseurs, tout.hyperviseurs],
    ['Invités', vu.invites, tout.invites],
    ['En marche', vu.marche, tout.marche],
    ['vCPU alloués', vu.vcpu, tout.vcpu],
    ['RAM allouée', vu.ram, tout.ram, goDepuisMo],
  ];
  if (tout.sansAcces) lignes.push(['Sans accès', vu.sansAcces, tout.sansAcces]);

  z.replaceChildren();
  z.classList.toggle('filtre', actif);
  for (const [libelle, valeur, total, format] of lignes) {
    const d = document.createElement('div');
    d.className = 'stat';
    const b = document.createElement('b');
    b.textContent = format ? format(valeur) : valeur.toLocaleString('fr-FR');
    d.appendChild(b);
    // Le total d'origine reste affiche quand un filtre reduit le perimetre :
    // sans lui, on croirait regarder tout le parc.
    if (actif && valeur !== total) {
      const sur = document.createElement('i');
      sur.className = 'sur';
      sur.textContent = ' / ' + (format ? format(total)
                                        : total.toLocaleString('fr-FR'));
      b.appendChild(sur);
    }
    d.appendChild(Object.assign(document.createElement('span'),
                                { textContent: libelle }));
    z.appendChild(d);
  }
  document.getElementById('horodatage').textContent =
    'Snapshot du ' + DONNEES.horodatage + (actif ? ' · vue filtrée' : '');
}

// ------------------------------------------------------------------ actions
document.getElementById('recherche').addEventListener('input', ev => {
  etat.recherche = ev.target.value.trim().toLowerCase();
  dessiner();
});
document.getElementById('btn-filtres').onclick = ev => {
  document.getElementById('filtres').classList.toggle('replie');
  ev.target.classList.toggle('actif');
};
document.getElementById('btn-plier').onclick = ev => {
  const pliables = DONNEES.noeuds.filter(n => n.kind === 'node' && n.enfants.length);
  const tout = pliables.every(n => etat.plies.has(n.id));
  pliables.forEach(n => tout ? etat.plies.delete(n.id) : etat.plies.add(n.id));
  ev.target.textContent = tout ? 'Replier' : 'Déplier';
  sauver(); dessiner();
};
document.getElementById('btn-ajuster').onclick = ajuster;
document.getElementById('btn-reinit').onclick = () => {
  etat.plies.clear(); etat.decales = {}; etat.recherche = '';
  for (const k of Object.keys(etat.filtres)) etat.filtres[k].clear();
  document.getElementById('recherche').value = '';
  construireFiltres(); sauver(); dessiner(); ajuster();
};
// ----------------------------------------------------------------- jeton
const boiteJeton = document.getElementById('boite-jeton');
document.getElementById('jeton-fermer').onclick =
  () => boiteJeton.classList.remove('visible');

document.getElementById('btn-jeton').onclick = async () => {
  boiteJeton.classList.add('visible');
  const etat = document.getElementById('jeton-etat');
  const cmd = document.getElementById('jeton-cmd');
  const note = document.getElementById('jeton-note');
  etat.textContent = 'demande en cours…';
  cmd.textContent = ''; note.textContent = '';
  try {
    // meme origine : le cookie de session OIDC part avec la requete
    const r = await fetch('api/token', { method: 'POST', credentials: 'same-origin' });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      etat.textContent = 'refus (' + r.status + ')';
      note.textContent = d.erreur || (r.status === 401
        ? "Session expiree : recharge la page pour te reauthentifier."
        : "Le service d'inventaire n'a pas repondu comme attendu.");
      return;
    }
    const d = await r.json();
    etat.textContent = 'valable ' + d.expire_dans_min + ' min, une seule fois';
    cmd.textContent = 'infra update --token ' + d.token;
    note.textContent = 'Emis pour ' + d.emis_par
      + '. Un jeton deja utilise est refuse : si le client echoue, reprends-en un.';
  } catch (err) {
    // page ouverte sans le service derriere (fichier local, par exemple)
    etat.textContent = 'service injoignable';
    note.textContent = "Cette page est servie sans son service d'API : "
      + "le jeton ne peut pas etre emis d'ici.";
  }
};

document.getElementById('jeton-copier').onclick = async () => {
  const cmd = document.getElementById('jeton-cmd').textContent;
  if (!cmd) return;
  const bouton = document.getElementById('jeton-copier');
  try {
    await navigator.clipboard.writeText(cmd);
    bouton.textContent = 'copie';
  } catch (err) {
    // presse-papiers refuse hors contexte securise : on selectionne a la place
    const plage = document.createRange();
    plage.selectNodeContents(document.getElementById('jeton-cmd'));
    getSelection().removeAllRanges(); getSelection().addRange(plage);
    bouton.textContent = 'selectionne, Ctrl+C';
  }
  setTimeout(() => { bouton.textContent = 'Copier la commande'; }, 2500);
};

document.getElementById('btn-theme').onclick = () => {
  const r = document.documentElement;
  r.dataset.theme = r.dataset.theme === 'light' ? 'dark' : 'light';
  sauver();
};
addEventListener('keydown', ev => {
  if (ev.key === 'Escape') {
    if (tiroir.classList.contains('visible')) { fermerTiroir(); return; }
    fiche.classList.remove('visible'); etat.selection = null; dessiner();
  }
  const saisie = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName);
  if (ev.key === '/' && !saisie) {
    ev.preventDefault();
    document.getElementById(vueRent.actif ? 'rent-recherche' : 'recherche').focus();
  }
});
addEventListener('resize', () => appliquerVue());

// ------------------------------------------------------------------ demarrage
construireStats();
construireFiltres();

// Beaucoup de valeurs de filtres : on les replie au premier chargement pour
// laisser la place au schema. Le bouton Filtres les rouvre.
const nbValeurs = DONNEES.filtres.reduce((n, f) => n + f.valeurs.length, 0);
if (!localStorage.getItem(CLE) && nbValeurs > 18) {
  document.getElementById('filtres').classList.add('replie');
} else {
  document.getElementById('btn-filtres').classList.add('actif');
}

// Premiere visite sur un parc important : tout deplier donne une page de
// plusieurs milliers de pixels, illisible. On part replie, l'utilisateur
// ouvre ce qui l'interesse.
const SEUIL_REPLI = 60;
if (!localStorage.getItem(CLE)) {
  const invites = DONNEES.noeuds.filter(n => n.kind === 'vm').length;
  if (invites > SEUIL_REPLI) {
    DONNEES.noeuds.filter(n => n.kind === 'node' && n.enfants.length)
      .forEach(n => etat.plies.add(n.id));
    document.getElementById('btn-plier').textContent = 'Déplier';
  }
  dessiner();
  ajuster();
} else {
  dessiner();
}

