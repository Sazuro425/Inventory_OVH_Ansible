// ------------------------------------------------------ filtres communs
// Un seul jeu de filtres pour les trois presentations : l'arbre garde une
// carte si elle ou l'un de ses descendants passe, le tableau et les
// graphiques ne gardent que les cartes qui passent. Un filtre choisit des
// cartes ; il ne recalcule pas leurs montants, qui restent ceux de l'API.
const selection = { clusters: new Set(), datacenters: new Set(),
                    clients: new Set(), roles: new Set() };
const CRITERES = [['clusters', 'Clusters'], ['datacenters', 'Datacenters'],
                  ['clients', 'Clients'], ['roles', 'Rôles']];
// une valeur de filtre telle qu'on la lit : le libelle pour un role
const libelleValeur = (k, v) => k === 'roles' ? libelleRole(v) : v;
let cacheLignes = { arbre: null, lignes: [], parCarte: new Map() };

// L'arbre aplati, chaque carte avec son contexte : groupe, serveur, parent,
// datacenters, et les clients et roles qu'elle porte ou que portent ses
// descendants — c'est ce qui permet de garder un cluster parce qu'un site
// d'un client choisi y tourne, ou un serveur parce qu'il heberge une VM LAMP.
function aplatir() {
  if (cacheLignes.arbre === commerce.arbre) return cacheLignes.lignes;
  const out = [];
  let ordre = 0;
  const visiter = (c, ctx, chemin) => {
    const ici = chemin.concat([cleIndex(c)]);
    const r = { c, chemin: ici, ordre: ordre++, cluster: ctx.cluster,
                groupe: ctx.groupe, serveur: ctx.serveur, parent: ctx.parent,
                dcs: ctx.dcs, clients: new Set(), roles: new Set() };
    if (c.type === 'cluster') {
      r.cluster = c.infos.seul ? '' : c.nom;
      r.groupe = libelleGroupe(c);
      r.dcs = new Set(c.infos.datacenters || []);
    }
    if (c.type === 'serveur' && c.infos.datacenter) r.dcs = new Set([c.infos.datacenter]);
    if (c.type === 'vm') contrats(c).forEach(v => v.client && r.clients.add(v.client));
    if (c.type === 'element' && c.infos.client) r.clients.add(c.infos.client);
    if (c.infos.role) r.roles.add(c.infos.role);
    r.nbVm = compterVm(c);
    out.push(r);
    const sous = { cluster: r.cluster, groupe: r.groupe, dcs: r.dcs,
                   serveur: c.type === 'serveur' ? c.nom : ctx.serveur,
                   parent: c.nom };
    for (const e of c.enfants) {
      const re = visiter(e, sous, ici);
      re.clients.forEach(x => r.clients.add(x));
      re.roles.forEach(x => r.roles.add(x));
    }
    return r;
  };
  commerce.arbre.forEach(c => visiter(c, { cluster: '', groupe: '', serveur: '',
                                           parent: '', dcs: new Set() }, []));
  cacheLignes = { arbre: commerce.arbre, lignes: out,
                  parCarte: new Map(out.map(r => [r.c, r])) };
  return out;
}

function compterVm(c) {
  return c.enfants.reduce((n, e) => n + (e.type === 'vm' ? 1 : 0) + compterVm(e), 0);
}

const croise = (ens, choix) => [...ens].some(x => choix.has(x));

function correspondRent(r) {
  if (selection.clusters.size && !selection.clusters.has(r.groupe)) return false;
  if (selection.datacenters.size && !croise(r.dcs, selection.datacenters)) return false;
  if (selection.clients.size && !croise(r.clients, selection.clients)) return false;
  if (selection.roles.size && !croise(r.roles, selection.roles)) return false;
  const f = vueRent.filtre;
  if (f && ![r.c.nom, r.cluster, r.serveur, r.parent, r.c.infos.role_libelle,
             r.c.infos.client, r.c.infos.contrat,
             ...contrats(r.c).map(v => v.contrat + ' ' + (v.client || ''))]
      .some(x => String(x || '').toLowerCase().includes(f))) return false;
  if (vueRent.perte && !(r.c.roi < 0)) return false;
  if (vueRent.alertes && !r.c.alertes.length) return false;
  return true;
}

const filtreDeParc = () => selection.clusters.size || selection.datacenters.size
  || selection.roles.size || vueRent.filtre || vueRent.perte || vueRent.alertes;
const filtreRentActif = () => filtreDeParc() || selection.clients.size;

function lignesFiltrees(type) {
  return aplatir().filter(r => r.c.type === type && correspondRent(r));
}

// Arbre : une carte reste si elle passe, ou si un descendant passe — on ne
// cache jamais le chemin vers un resultat.
function garde(c) {
  aplatir();
  const memo = new Map();
  const test = x => {
    if (memo.has(x)) return memo.get(x);
    const r = correspondRent(cacheLignes.parCarte.get(x)) || x.enfants.some(test);
    memo.set(x, r);
    return r;
  };
  return test(c);
}

function valeursCritere(k) {
  const v = new Set();
  for (const r of aplatir()) {
    if (k === 'clusters' && r.c.type === 'cluster') v.add(r.groupe);
    if (k === 'datacenters') r.dcs.forEach(x => v.add(x));
    if (k === 'clients') r.clients.forEach(x => v.add(x));
    if (k === 'roles') r.roles.forEach(x => v.add(x));
  }
  // une valeur choisie reste affichee meme si elle a disparu du parc : sinon
  // on ne pourrait plus la decocher
  selection[k].forEach(x => v.add(x));
  return [...v].sort((a, b) => libelleValeur(k, a)
    .localeCompare(libelleValeur(k, b), 'fr', { numeric: true }));
}

function construireFiltresRent() {
  const zone = document.getElementById('rent-filtres');
  zone.replaceChildren();
  for (const [k, titre] of CRITERES) {
    const valeurs = valeursCritere(k);
    if (!valeurs.length) continue;
    const grp = el('div', { className: 'groupe' }, el('label', { textContent: titre }));
    for (const v of valeurs) {
      const p = el('button', { className: 'puce' + (selection[k].has(v) ? ' choisi' : ''),
        textContent: libelleValeur(k, v), ariaPressed: String(selection[k].has(v)),
        onclick: () => {
          if (selection[k].has(v)) selection[k].delete(v); else selection[k].add(v);
          apresFiltre();
        } });
      grp.appendChild(p);
    }
    zone.appendChild(grp);
  }
  zone.appendChild(el('button', { textContent: 'Tout effacer', className: 'effacer',
    onclick: () => { effacerFiltres(); apresFiltre(); } }));
}

function effacerFiltres() {
  for (const k in selection) selection[k].clear();
  vueRent.filtre = ''; vueRent.perte = false; vueRent.alertes = false;
  document.getElementById('rent-recherche').value = '';
  document.getElementById('rent-perte').checked = false;
  document.getElementById('rent-alertes').checked = false;
}

function majBoutonFiltres() {
  const n = Object.values(selection).reduce((t, s) => t + s.size, 0);
  const b = document.getElementById('rent-btn-filtres');
  b.textContent = n ? 'Filtres (' + n + ')' : 'Filtres';
  b.classList.toggle('actif', !document.getElementById('rent-filtres').hidden || n > 0);
}

function apresFiltre() {
  vueRent.cible = null;
  if (!document.getElementById('rent-filtres').hidden) construireFiltresRent();
  majBoutonFiltres();
  dessinerRent();
}

document.getElementById('rent-btn-filtres').onclick = () => {
  const zone = document.getElementById('rent-filtres');
  zone.hidden = !zone.hidden;
  if (!zone.hidden) construireFiltresRent();
  majBoutonFiltres();
};

// ------------------------------------------------------ vues enregistrees
// Une vue, c'est un nom et l'etat qui la rouvre : filtres, presentation,
// niveau et tri du tableau. Elles sont en base, partagees par defaut : la
// vue « Commercial » de l'un est celle de toute l'equipe. La vue d'ensemble,
// elle, n'est pas enregistree : c'est l'etat neutre, en graphiques.
const VUE_BASE = { id: '', nom: 'Vue d’ensemble', definition: { mode: 'graphiques' },
                   modifiable: false };
const vues = { liste: [], courante: VUE_BASE, dispo: false };

// L'etat courant, sous la forme exacte que le serveur normalise : memes
// cles, listes triees. C'est ce qui permet de dire « modifiee » sans faux
// positif.
function etatCourant() {
  const d = { mode: vueRent.mode };
  if (vueRent.mode === 'tableau') {
    d.niveau = plat.niveau;
    if (plat.tri) { d.tri = plat.tri; d.sens = plat.sens; }
  }
  const texte = document.getElementById('rent-recherche').value.trim();
  if (texte) d.texte = texte;
  if (vueRent.perte) d.perte = true;
  if (vueRent.alertes) d.alertes = true;
  for (const [k] of CRITERES) {
    if (selection[k].size) d[k] = [...selection[k]].sort();
  }
  return d;
}

function canon(d) {
  const o = {};
  for (const k of Object.keys(d || {}).sort()) {
    o[k] = Array.isArray(d[k]) ? [...d[k]].sort() : d[k];
  }
  // un niveau ou un tri hors du mode tableau n'a pas d'effet
  if (o.mode !== 'tableau') { delete o.niveau; delete o.tri; delete o.sens; }
  return JSON.stringify(o);
}

function appliquerDefinition(d) {
  d = d || {};
  for (const [k] of CRITERES) selection[k] = new Set(d[k] || []);
  document.getElementById('rent-recherche').value = d.texte || '';
  vueRent.filtre = (d.texte || '').toLowerCase();
  vueRent.perte = !!d.perte;
  vueRent.alertes = !!d.alertes;
  document.getElementById('rent-perte').checked = vueRent.perte;
  document.getElementById('rent-alertes').checked = vueRent.alertes;
  plat.niveau = d.niveau || 'serveur';
  plat.tri = d.tri || null;
  plat.sens = d.sens === -1 ? -1 : 1;
  vueRent.cible = null;
  if (!document.getElementById('rent-filtres').hidden) construireFiltresRent();
  majBoutonFiltres();
  basculerMode(d.mode || 'graphiques');
}

function majModifiee() {
  const modifiee = canon(etatCourant()) !== canon(vues.courante.definition);
  document.getElementById('vue-modifiee').hidden = !modifiee;
}

function remplirSelectVues() {
  const s = document.getElementById('rent-vue');
  s.replaceChildren(el('option', { value: '', textContent: VUE_BASE.nom }));
  const miennes = vues.liste.filter(v => v.modifiable);
  const autres = vues.liste.filter(v => !v.modifiable);
  const groupe = (titre, lot) => {
    if (!lot.length) return;
    const g = el('optgroup', { label: titre });
    for (const v of lot) {
      g.appendChild(el('option', { value: String(v.id),
        textContent: v.nom + (v.partagee ? '' : ' (privée)')
          + (v.modifiable ? '' : ' — ' + v.cree_par) }));
    }
    s.appendChild(g);
  };
  groupe('Mes vues', miennes);
  groupe('Vues de l’équipe', autres);
  s.value = String(vues.courante.id);
  const c = vues.courante;
  document.getElementById('vue-supprimer').hidden = !c.modifiable;
  document.getElementById('vue-auteur').textContent = c.id === ''
    ? '' : c.modifiable ? (c.partagee ? 'partagée avec l’équipe' : 'visible de toi seul')
                        : 'par ' + c.cree_par + ', lecture seule';
}

async function chargerVues(idVoulu) {
  try {
    const r = await fetch('/api/vues', { headers: { Accept: 'application/json' } });
    if (!r.ok) throw new Error('http ' + r.status);
    vues.liste = (await r.json()).vues || [];
    vues.dispo = true;
  } catch (e) {
    vues.liste = [];
    vues.dispo = false;
  }
  document.getElementById('rent-vues').hidden = !vues.dispo;
  const voulue = vues.liste.find(v => String(v.id) === String(idVoulu));
  vues.courante = voulue || VUE_BASE;
  remplirSelectVues();
  appliquerDefinition(vues.courante.definition);
}

document.getElementById('rent-vue').onchange = ev => {
  vues.courante = vues.liste.find(v => String(v.id) === ev.target.value) || VUE_BASE;
  sauverRent();
  remplirSelectVues();
  appliquerDefinition(vues.courante.definition);
};

document.getElementById('vue-enregistrer').onclick = () => {
  const c = vues.courante;
  const corps = ouvrirTiroir('Enregistrer la vue',
    'filtres, présentation, niveau et tri du tableau');
  const f = el('form', { autocomplete: 'off', className: 'saisie' });
  const nom = champ(f, 'Nom', { type: 'text', required: true, maxLength: 80,
    value: c.modifiable ? c.nom : '', placeholder: 'ex. Commercial, Compta mensuelle' });
  const part = el('input', { type: 'checkbox', checked: c.modifiable ? c.partagee : true });
  f.appendChild(el('label', { className: 'case' }, part,
    ' partagée avec l’équipe (sinon visible de toi seul)'));
  const resume = el('ul', { className: 'resume-vue' });
  const d = etatCourant();
  resume.appendChild(el('li', { textContent: 'Présentation : ' + d.mode
    + (d.niveau ? ', niveau ' + d.niveau : '') + (d.tri ? ', tri ' + d.tri : '') }));
  for (const [k, titre] of CRITERES) {
    if (d[k]) resume.appendChild(el('li', { textContent: titre + ' : '
      + d[k].map(x => libelleValeur(k, x)).join(', ') }));
  }
  if (d.texte) resume.appendChild(el('li', { textContent: 'Texte : ' + d.texte }));
  if (d.perte) resume.appendChild(el('li', { textContent: 'À perte seulement' }));
  if (d.alertes) resume.appendChild(el('li', { textContent: 'Avec alerte seulement' }));
  f.appendChild(resume);
  const envoyer = (id) => soumettre(f, async () => {
    const rep = await poster('/api/vues', { id, nom: nom.value, partagee: part.checked,
                                            definition: etatCourant() });
    await chargerVues(rep.vue.id);
  }, id ? 'Vue mise à jour.' : 'Vue enregistrée.');
  const actions = el('div', { className: 'actions' });
  if (c.modifiable) {
    actions.appendChild(el('button', { type: 'submit',
      textContent: 'Mettre à jour « ' + c.nom + ' »' }));
    actions.appendChild(el('button', { type: 'button', textContent: 'Nouvelle vue',
      onclick: () => { if (f.reportValidity()) envoyer(null); } }));
    f.onsubmit = ev => { ev.preventDefault(); envoyer(c.id); };
  } else {
    actions.appendChild(el('button', { type: 'submit', textContent: 'Enregistrer' }));
    f.onsubmit = ev => { ev.preventDefault(); envoyer(null); };
  }
  actions.appendChild(el('button', { type: 'button', textContent: 'Annuler',
                                     onclick: fermerTiroir }));
  f.appendChild(actions);
  corps.appendChild(f);
  nom.focus();
};

document.getElementById('vue-supprimer').onclick = async () => {
  const c = vues.courante;
  if (!c.modifiable || !confirm('Supprimer la vue « ' + c.nom + ' » ?')) return;
  try {
    await poster('/api/vues/retirer', { id: c.id });
    flash('Vue supprimée.');
    await chargerVues('');
  } catch (e) {
    flash(e.message);
  }
};

// a la largeur du conteneur : on redessine quand la fenetre change
let attenteViz = null;
addEventListener('resize', () => {
  if (!vueRent.actif || vueRent.mode !== 'graphiques') return;
  clearTimeout(attenteViz);
  attenteViz = setTimeout(dessinerGraphes, 120);
});

