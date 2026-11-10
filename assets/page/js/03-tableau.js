// ----------------------------------------------------------- vue tableau
// Les memes cartes que l'arbre, a plat : une ligne par carte d'un niveau,
// des colonnes propres a ce niveau, un tri par colonne. Rien n'est recalcule
// ici, les montants sont ceux de l'API ; l'export reprend exactement ce qui
// est affiche — lignes filtrees, colonnes du niveau, ordre du tri.
const NIVEAUX = [
  ['cluster', 'Clusters'], ['serveur', 'Serveurs'], ['vm', 'VM'],
  ['element', 'Éléments'], ['tout', 'Tout'],
];

const n0 = v => (v === null || v === undefined || v === '') ? null : Number(v);
const marge = c => c.pv ? Math.round(1000 * c.roi / c.pv) / 10 : null;
const contrats = c => (c.infos.contrats || []);
const corrigees = c => Object.entries(c.corrections)
  .map(([k, v]) => k.replace('_', ' ') + ' = ' + v.valeur).join(', ');

// id, titre, valeur brute (tri et CSV), format a l'ecran, niveaux ou elle
// figure. num : nombre (aligne a droite, virgule decimale dans le CSV).
const COLONNES_PLAT = [
  { id: 'type', titre: 'Type', niv: ['tout'],
    val: r => LIBELLE[r.c.type] },
  { id: 'nom', titre: 'Nom', niv: ['cluster', 'serveur', 'vm', 'element', 'tout'],
    val: r => r.c.nom },
  { id: 'cluster', titre: 'Cluster', niv: ['serveur', 'vm', 'element', 'tout'],
    val: r => r.cluster },
  { id: 'serveur', titre: 'Serveur', niv: ['vm', 'element'],
    val: r => r.serveur },
  { id: 'parent', titre: 'Rattaché à', niv: ['element'],
    val: r => r.parent },
  { id: 'role', titre: 'Rôle', niv: ['cluster', 'serveur', 'vm', 'element', 'tout'],
    val: r => r.c.infos.role_libelle },
  { id: 'offre', titre: 'Offre', niv: ['serveur'], val: r => r.c.infos.offre },
  { id: 'dc', titre: 'DC', niv: ['cluster', 'serveur'],
    val: r => r.c.infos.datacenter || (r.c.infos.datacenters || []).join(' ') },
  { id: 'noeud', titre: 'Nœud', niv: ['serveur', 'vm'], val: r => r.c.infos.noeud },
  { id: 'nb_srv', titre: 'Serveurs', niv: ['cluster'], num: true, dec: 0,
    val: r => r.c.enfants.filter(e => e.type === 'serveur').length },
  { id: 'nb_vm', titre: 'VM', niv: ['cluster', 'serveur'], num: true, dec: 0,
    val: r => r.nbVm },
  { id: 'vmid', titre: 'VMID', niv: ['vm'], num: true, dec: 0,
    val: r => n0(r.c.infos.vmid) },
  { id: 'statut', titre: 'État', niv: ['vm'], val: r => r.c.infos.statut },
  { id: 'vcpu', titre: 'vCPU', niv: ['vm'], num: true, dec: 0,
    val: r => n0(r.c.infos.cpus) },
  { id: 'ram', titre: 'RAM (Go)', niv: ['vm'], num: true, dec: 1,
    val: r => r.c.infos.ram_mb ? r.c.infos.ram_mb / 1024 : null },
  { id: 'contrat', titre: 'Contrat', niv: ['vm', 'element'],
    val: r => r.c.type === 'vm' ? contrats(r.c).map(v => v.contrat).join(', ')
                                : r.c.infos.contrat },
  { id: 'client', titre: 'Client', niv: ['vm', 'element'],
    val: r => r.c.type === 'vm'
      ? [...new Set(contrats(r.c).map(v => v.client).filter(Boolean))].join(', ')
      : r.c.infos.client },
  { id: 'nb_el', titre: 'Éléments', niv: ['vm'], num: true, dec: 0,
    val: r => r.c.enfants.filter(e => e.type === 'element').length },
  { id: 'pa_auto', titre: 'Coût OVH', niv: ['serveur'], num: true, euro: true,
    val: r => n0(r.c.pa_auto) },
  { id: 'pa', titre: 'PA / mois', niv: ['cluster', 'serveur', 'vm', 'element', 'tout'],
    num: true, euro: true, somme: true, val: r => r.c.pa },
  { id: 'pv', titre: 'CA / mois', niv: ['cluster', 'serveur', 'vm', 'element', 'tout'],
    num: true, euro: true, somme: true, val: r => r.c.pv },
  { id: 'roi', titre: 'ROI / mois', niv: ['cluster', 'serveur', 'vm', 'element', 'tout'],
    num: true, euro: true, somme: true, signe: true, val: r => r.c.roi },
  { id: 'marge', titre: 'Marge %', niv: ['cluster', 'serveur', 'vm', 'element', 'tout'],
    num: true, dec: 1, val: r => marge(r.c) },
  { id: 'ram_pct', titre: 'RAM allouée %', niv: ['cluster', 'serveur'],
    num: true, dec: 1, val: r => n0((r.c.capacite || {}).ram_pct) },
  { id: 'ram_libre', titre: 'RAM libre (Go)', niv: ['cluster', 'serveur'],
    num: true, dec: 0, val: r => (r.c.capacite || {}).ram_libre_mb === undefined
      ? null : r.c.capacite.ram_libre_mb / 1024 },
  { id: 'vcpu_ratio', titre: 'vCPU / thread', niv: ['cluster', 'serveur'],
    num: true, dec: 2, val: r => n0((r.c.capacite || {}).vcpu_ratio) },
  { id: 'corr', titre: 'Corrections', niv: ['cluster', 'serveur', 'vm'],
    val: r => corrigees(r.c) },
  { id: 'alertes', titre: 'Alertes', niv: ['cluster', 'serveur', 'tout'],
    val: r => r.c.alertes.join(' ; ') },
];

const plat = {
  niveau: 'serveur', tri: null, sens: 1,   // tri null : ordre de l'arbre
};
// Ce navigateur se souvient de la vue choisie et de la presentation de
// chaque figure. Le reste — filtres, mode, niveau — appartient a la vue :
// rouvrir la page rouvre la vue telle qu'enregistree.
const prefsRent = { vue: '' };
try {
  const s = JSON.parse(localStorage.getItem(CLE + ':rent') || '{}');
  if (s.vue !== undefined) prefsRent.vue = String(s.vue);
  if (s.figures) vueRent.figures = s.figures;
} catch (e) { /* stockage indisponible : vue d'ensemble */ }
function sauverRent() {
  try {
    localStorage.setItem(CLE + ':rent', JSON.stringify({
      vue: vues.courante ? vues.courante.id : '', figures: vueRent.figures }));
  } catch (e) { /* sans persistance */ }
}

function lignesPlat() {
  const lignes = aplatir().filter(r =>
    (plat.niveau === 'tout' || r.c.type === plat.niveau) && correspondRent(r));
  const col = COLONNES_PLAT.find(k => k.id === plat.tri);
  if (col) {
    const cmp = col.num
      ? (a, b) => (col.val(a) ?? -Infinity) - (col.val(b) ?? -Infinity)
      : (a, b) => String(col.val(a) || '').localeCompare(
          String(col.val(b) || ''), 'fr', { numeric: true, sensitivity: 'base' });
    lignes.sort((a, b) => plat.sens * cmp(a, b) || a.ordre - b.ordre);
  }
  return lignes;
}

const colonnesNiveau = () => COLONNES_PLAT.filter(k => k.niv.includes(plat.niveau));

function formatCellule(k, v) {
  if (v === null || v === undefined || v === '') return '—';
  if (k.euro) return k.signe ? signe(v) : euro(v);
  if (k.num) return Number(v).toLocaleString('fr-FR',
    { maximumFractionDigits: k.dec ?? 2 });
  return String(v);
}

function dessinerPlat() {
  const cols = colonnesNiveau();
  const lignes = lignesPlat();
  document.querySelectorAll('#rent-niveaux button').forEach(b =>
    b.classList.toggle('actif', b.dataset.niveau === plat.niveau));

  const tete = document.getElementById('plat-tete');
  tete.replaceChildren(el('tr', null, ...cols.map(k => {
    const fleche = plat.tri === k.id ? (plat.sens > 0 ? ' ▲' : ' ▼') : '';
    const th = el('th', { className: k.num ? 'num' : '', scope: 'col' },
      el('button', { className: 'tri', textContent: k.titre + fleche,
        title: 'Trier par ' + k.titre.toLowerCase(),
        onclick: () => {
          // trois etats : croissant, decroissant, ordre de l'arbre
          if (plat.tri !== k.id) { plat.tri = k.id; plat.sens = k.num ? -1 : 1; }
          else if (plat.sens === (k.num ? -1 : 1)) plat.sens = -plat.sens;
          else plat.tri = null;
          dessinerPlat();
        } }));
    if (plat.tri === k.id) th.ariaSort = plat.sens > 0 ? 'ascending' : 'descending';
    return th;
  }), el('th')));

  const corps = document.getElementById('plat-corps');
  corps.replaceChildren();
  for (const r of lignes) {
    const tr = el('tr', { className: 'r-' + r.c.type, title: 'Voir dans l’arbre' });
    for (const k of cols) {
      const v = k.val(r);
      const td = el('td', { textContent: formatCellule(k, v) });
      if (k.num) td.className = 'num';
      if (k.signe) td.className += ' marge ' + classeMarge(v);
      if (k.id === 'pa' && r.c.corrections.prix_achat) td.classList.add('corrige');
      if (k.id === 'pv' && r.c.corrections.prix_vente) td.classList.add('corrige');
      if (k.id === 'ram_pct' && v !== null) {
        td.classList.add(v > 100 ? 'trop' : v >= 85 ? 'tendue' : 'ok');
      }
      if (k.id === 'nom') td.className = 'nom-plat';
      tr.appendChild(td);
    }
    tr.appendChild(actionsCarte(r.c));
    tr.onclick = ev => { if (ev.target.tagName !== 'BUTTON') basculerMode('arbre', r.chemin); };
    corps.appendChild(tr);
  }
  if (!lignes.length) {
    corps.appendChild(el('tr', null, el('td', { colSpan: cols.length + 1,
      className: 'lecture', textContent: 'Aucune ligne ne correspond.' })));
  }

  // un total n'a de sens que sur un seul niveau : sur « Tout », chaque
  // montant est deja compte dans celui de son parent
  const pied = document.getElementById('plat-pied');
  pied.replaceChildren();
  if (lignes.length && plat.niveau !== 'tout') {
    pied.appendChild(el('tr', null, ...cols.map((k, i) => {
      if (i === 0) return el('th', { scope: 'row',
        textContent: lignes.length + ' ligne' + (lignes.length > 1 ? 's' : '') });
      if (!k.somme) return el('td');
      const s = lignes.reduce((t, r) => t + Number(k.val(r) || 0), 0);
      return el('td', { className: 'num' + (k.signe ? ' marge ' + classeMarge(s) : ''),
                        textContent: formatCellule(k, s) });
    }), el('td')));
  }
  document.getElementById('plat-compte').textContent =
    lignes.length + ' ligne' + (lignes.length > 1 ? 's' : '');
  majModifiee();
}

// ------------------------------------------------- export de la vue en CSV
// Memes regles que l'export serveur (rentabilite.py) : UTF-8 avec BOM pour
// qu'Excel reconnaisse les accents, point-virgule, virgule decimale sans
// separateur de milliers pour que les montants restent des nombres, et une
// apostrophe devant tout texte qui commencerait par = + - @ : un nom de site
// ne doit pas s'executer comme une formule chez la personne qui ouvre le
// fichier. Un montant negatif, lui, reste un nombre.
function celluleCsv(k, v) {
  if (v === null || v === undefined || v === '') return '';
  let t;
  if (k.num) {
    t = Number(v).toFixed(k.euro ? 2 : (k.dec ?? 2)).replace('.', ',');
  } else {
    t = String(v).replace(/[\r\n]+/g, ' ');
    if (/^[=+\-@\t]/.test(t)) t = "'" + t;
  }
  return /[;"\n]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t;
}

function texteCsv(colonnes, lignes) {
  const entete = colonnes.map(k => celluleCsv({}, k.titre + (k.euro ? ' (€)' : '')));
  const corps = lignes.map(r => colonnes.map(k => celluleCsv(k, k.val(r))).join(';'));
  return '\ufeff' + [entete.join(';'), ...corps].join('\r\n') + '\r\n';
}

const csvVue = () => texteCsv(colonnesNiveau(), lignesPlat());

function telechargerCsv(base, colonnes, lignes) {
  const nom = base + '-' + new Date().toISOString().slice(0, 10) + '.csv';
  const url = URL.createObjectURL(new Blob([texteCsv(colonnes, lignes)],
                                           { type: 'text/csv;charset=utf-8' }));
  const a = el('a', { href: url, download: nom });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function exporterVue() {
  const lignes = lignesPlat();
  telechargerCsv('rentabilite-' + plat.niveau, colonnesNiveau(), lignes);
  flash(lignes.length + ' lignes exportées.');
}

function basculerMode(mode, chemin) {
  vueRent.mode = mode;
  sauverRent();
  document.body.classList.toggle('mode-plat', mode === 'tableau');
  document.body.classList.toggle('mode-graph', mode === 'graphiques');
  cacherBulle();
  document.querySelectorAll('#rent-modes button').forEach(b =>
    b.classList.toggle('actif', b.dataset.mode === mode));
  if (chemin) { ouvrirRent(chemin); return; }
  dessinerRent();
}

document.querySelectorAll('#rent-modes button').forEach(b => {
  b.onclick = () => basculerMode(b.dataset.mode);
});
document.querySelectorAll('#rent-niveaux button').forEach(b => {
  b.onclick = () => {
    plat.niveau = b.dataset.niveau;
    // une colonne absente du nouveau niveau ne peut plus trier
    if (!colonnesNiveau().some(k => k.id === plat.tri)) plat.tri = null;
    sauverRent();
    dessinerPlat();
  };
});
document.getElementById('rent-export-vue').onclick = exporterVue;
document.body.classList.toggle('mode-plat', vueRent.mode === 'tableau');
document.body.classList.toggle('mode-graph', vueRent.mode === 'graphiques');
document.querySelectorAll('#rent-modes button').forEach(b =>
  b.classList.toggle('actif', b.dataset.mode === (vueRent.mode || 'arbre')));

