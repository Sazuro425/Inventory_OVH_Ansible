// -------------------------------------------------------------- rentabilite
// Le cout vient du snapshot, les ventes et les saisies changent entre deux
// collectes : tout est lu a chaud par /api/rentabilite, qui rend l'arbre
// cluster > serveur > VM > elements deja calcule. La page n'additionne rien
// elle-meme : le chiffre affiche est celui de l'export CSV, a l'euro pres.
//
// Si l'API ne repond pas — page ouverte en fichier, service arrete — tout ce
// volet se tait. La topologie doit rester consultable sans lui.
const commerce = {
  dispo: false, peutEcrire: false, globale: null, arbre: [], orphelins: [],
  roles: [],             // le catalogue : [{ code, libelle }]
  index: new Map(),      // "type:cle" -> { carte, chemin }
  ouverts: new Set(),    // chemins depliés dans la vue
  premier: true,
};

const euro = (v, d) => (v === null || v === undefined || v === '') ? '—'
  : new Intl.NumberFormat('fr-FR', { style: 'currency',
      currency: d || 'EUR', maximumFractionDigits: 2 }).format(Number(v));
const pct = v => (v === null || v === undefined) ? '—'
  : new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 0 }).format(v) + ' %';
const go = mb => (mb === null || mb === undefined) ? '?'
  : new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 0 }).format(mb / 1024) + ' Go';

function classeMarge(v) {
  if (v === null || v === undefined) return 'neutre';
  return Number(v) > 0 ? 'positif' : (Number(v) < 0 ? 'negatif' : 'neutre');
}

// un signe explicite : la couleur seule ne suffit pas a dire perte ou benefice
function signe(v) {
  const n = Number(v || 0);
  return (n > 0 ? '+' : '') + euro(n);
}

// Petit constructeur de DOM. Jamais d'innerHTML : les noms viennent de
// saisies humaines, ils passent tous par textContent.
function el(tag, props, ...enfants) {
  const e = Object.assign(document.createElement(tag), props || {});
  for (const x of enfants) {
    if (x === null || x === undefined || x === false) continue;
    e.appendChild(typeof x === 'string' ? document.createTextNode(x) : x);
  }
  return e;
}

const LIBELLE = { cluster: 'Cluster', serveur: 'Serveur', vm: 'VM',
                  element: 'Élément' };
const cleIndex = c => c.type + ':' + c.cle;

// le libelle d'un role ; un code retire du catalogue reste lisible tel quel
function libelleRole(code) {
  const r = commerce.roles.find(x => x.code === code);
  return r ? r.libelle : code;
}

function indexer() {
  commerce.index = new Map();
  const visiter = (c, chemin) => {
    const ici = chemin.concat([cleIndex(c)]);
    // deux VM homonymes : la premiere rencontree est celle qui porte les
    // contrats, comme dans le calcul
    if (!commerce.index.has(cleIndex(c))) {
      commerce.index.set(cleIndex(c), { carte: c, chemin: ici });
    }
    c.enfants.forEach(e => visiter(e, ici));
  };
  commerce.arbre.forEach(c => visiter(c, []));
}

async function chargerCommerce() {
  try {
    const r = await fetch('/api/rentabilite',
                          { headers: { Accept: 'application/json' } });
    if (r.status === 401 && commerce.dispo) { sessionExpiree(); return; }
    if (!r.ok) throw new Error('http ' + r.status);
    const rent = await r.json();
    commerce.globale = rent.globale || null;
    commerce.arbre = rent.arbre || [];
    commerce.orphelins = rent.orphelins || [];
    commerce.roles = rent.roles || [];
    commerce.peutEcrire = !!rent.peut_ecrire;
    indexer();
    if (commerce.premier) {
      // a l'ouverture : les clusters ouverts, leurs serveurs visibles
      commerce.arbre.forEach(c => commerce.ouverts.add(cleIndex(c)));
      commerce.premier = false;
    }
    commerce.dispo = true;
  } catch (e) {
    commerce.dispo = false;
  }
  document.getElementById('btn-rent').hidden = !commerce.dispo;
  majStatCommerce();
  if (vueRent.actif) dessinerRent();
  if (etat.selection) {
    const n = parId.get(etat.selection);
    if (n) ficheCommerce(n);
  }
}

function chip(titre, valeur, classe) {
  const b = el('b', { textContent: valeur });
  if (classe) b.className = 'marge ' + classe;
  return el('div', { className: 'stat' }, b, el('span', { textContent: titre }));
}

function majStatCommerce() {
  const zone = document.getElementById('stats-commerce');
  zone.replaceChildren();
  if (!commerce.dispo || !commerce.globale) return;
  const g = commerce.globale;
  zone.append(chip('PA / mois', euro(g.pa)), chip('CA / mois', euro(g.pv)),
              chip('ROI / mois', signe(g.roi), classeMarge(g.roi)));
  if (Number(g.groupes_a_perte) > 0) {
    zone.appendChild(chip('clusters à perte', g.groupes_a_perte, 'negatif'));
  }
}

// ------------------------------------------------------- fiche topologie
// La fiche d'une carte du schema montre le bilan de la carte correspondante
// de l'arbre, et renvoie vers la vue Rentabilite pour tout le reste.
function carteDe(n) {
  if (n.kind === 'srv' && n.ovh) return commerce.index.get('serveur:' + n.ovh);
  if (n.kind === 'vm' && n.vm) return commerce.index.get('vm:' + n.vm);
  if (n.kind === 'node') {
    const srv = parId.get(n.parent);
    return srv && srv.ovh ? commerce.index.get('serveur:' + srv.ovh) : null;
  }
  return null;
}

// la fiche d'une carte du schema : son bilan commercial, puis, pour un
// admin, l'etat de sa recolte PVE
function ficheCommerce(n) {
  ficheVente(n);
  ficheRecolte(n);
}

function ficheVente(n) {
  const zone = document.getElementById('fiche-vente');
  zone.replaceChildren();
  if (!commerce.dispo) return;
  const trouve = carteDe(n);
  if (!trouve) return;
  const c = trouve.carte;
  const d = el('div', { className: 'vente' }, el('h4', { textContent: 'Rentabilité' }));
  bilanCarte(d, c);

  // le cluster du serveur : c'est lui le chiffre stable
  const cl = commerce.index.get(trouve.chemin[0]);
  if (c.type !== 'cluster' && cl && cl.carte !== c && !cl.carte.infos.seul) {
    d.appendChild(el('h5', { textContent: 'Cluster ' + cl.carte.nom }));
    const ligne = grille(d);
    ligne('ROI', signe(cl.carte.roi), classeMarge(cl.carte.roi));
  }
  if (c.type === 'vm') listeContrats(d, c);
  listeEnfants(d, c);
  const actions = el('div', { className: 'actions' });
  if (commerce.peutEcrire) {
    actions.append(
      el('button', { textContent: 'Prix et rôle…', title: 'Coût, prix de vente, rôle'
                       + (c.type === 'serveur' ? ', cluster' : ''),
                     onclick: () => formCorrection(c) }),
      el('button', { textContent: '+ Enfants…',
                     title: 'Déclarer des sites, vhosts, licences… sous ' + c.nom,
                     onclick: () => formEnfants(c) }));
  }
  actions.appendChild(el('button', { textContent: 'Voir dans la rentabilité',
                                     onclick: () => basculerMode('arbre', trouve.chemin) }));
  d.appendChild(actions);
  zone.appendChild(d);
}

// Les enfants declares a la main sous la carte : sites d'une VM LAMP,
// licence d'un serveur. Les VM ont leur propre carte dans le schema.
function listeEnfants(d, c) {
  const enfants = c.enfants.filter(e => e.type === 'element');
  if (!enfants.length && !commerce.peutEcrire) return;
  d.appendChild(el('h5', { textContent: 'Enfants déclarés'
                                        + (enfants.length ? ' · ' + enfants.length : '') }));
  if (!enfants.length) {
    d.appendChild(el('p', { className: 'lecture', textContent:
      'Aucun. Sites, vhosts, licences… se déclarent avec « + Enfants ».' }));
    return;
  }
  const ul = el('ul');
  for (const e of enfants) {
    const li = el('li', null,
      el('span', { className: 'nom', textContent: e.nom,
                   title: [e.nom, e.infos.client, e.infos.contrat].filter(Boolean).join(' · ') }),
      e.infos.role_libelle ? el('span', { className: 'role', textContent: e.infos.role_libelle })
                           : null,
      el('span', { className: 'prix', textContent: euro(e.pv) }));
    if (commerce.peutEcrire) {
      li.appendChild(el('button', { textContent: 'Éditer', onclick: () => formElement(null, e) }));
    }
    ul.appendChild(li);
  }
  d.appendChild(ul);
  const pv = enfants.reduce((t, e) => t + e.pv, 0);
  const pa = enfants.reduce((t, e) => t + e.pa, 0);
  d.appendChild(el('p', { className: 'total-enfants', textContent:
    'Les enfants rapportent ' + euro(pv) + ' / mois' + (pa ? ', coûtent ' + euro(pa) : '') }));
}

function grille(d) {
  const bilan = el('div', { className: 'bilan' });
  d.appendChild(bilan);
  return (k, v, classe) => {
    const b = el('b', { textContent: v });
    if (classe) b.className = 'marge ' + classe;
    bilan.append(el('span', { textContent: k }), b);
  };
}

function bilanCarte(d, c) {
  const ligne = grille(d);
  if (c.infos.role_libelle) ligne('Rôle', c.infos.role_libelle);
  ligne('PA', euro(c.pa) + (c.corrections.prix_achat ? ' ✎' : ''));
  ligne('CA', euro(c.pv) + (c.corrections.prix_vente ? ' ✎' : ''));
  ligne('ROI', signe(c.roi), classeMarge(c.roi));
  if (c.type === 'serveur' && c.pa_auto !== null && c.corrections.prix_achat) {
    ligne('Coût OVH collecté', euro(c.pa_auto));
  }
  for (const [champ, k] of Object.entries(c.corrections)) {
    d.appendChild(el('p', { className: 'lecture',
      textContent: '✎ ' + champ.replace('_', ' ') + ' corrigé par ' + k.par
        + (k.motif ? ' : « ' + k.motif + ' »' : '') }));
  }
  for (const a of c.alertes) {
    d.appendChild(el('p', { className: 'lecture alerte', textContent: '⚠ ' + a }));
  }
}

function listeContrats(d, c) {
  const contrats = c.infos.contrats || [];
  d.appendChild(el('h5', { textContent: 'Contrats' }));
  if (!contrats.length) {
    d.appendChild(el('p', { className: 'lecture',
                            textContent: 'Aucun contrat sur cette VM.' }));
  }
  const ul = el('ul');
  for (const v of contrats) {
    const li = el('li', null,
      el('span', { className: 'nom',
                   textContent: v.contrat + (v.client ? ' · ' + v.client : '') }),
      el('span', { className: 'prix',
                   textContent: euro(v.prix_vente, v.devise) + ' / ' + v.periode }));
    if (commerce.peutEcrire) {
      li.appendChild(el('button', { textContent: 'Éditer',
        onclick: () => formVente(c, v) }));
    }
    ul.appendChild(li);
  }
  if (contrats.length) d.appendChild(ul);
  if (commerce.peutEcrire) {
    d.appendChild(el('button', { textContent: 'Ajouter un contrat',
                                 onclick: () => formVente(c, null) }));
  }
}

// ------------------------------------------------------------- vue arbre
const vueRent = { actif: false, filtre: '', perte: false, alertes: false,
                  mode: 'graphiques', figures: {},
                  panneaux: null };   // null : les panneaux par defaut

function basculerRent(actif) {
  vueRent.actif = actif;
  document.body.classList.toggle('mode-rent', actif);
  document.getElementById('btn-rent').classList.toggle('actif', actif);
  if (actif) dessinerRent();
  else { fermerTiroir(); appliquerVue(); }
}

function ouvrirRent(chemin) {
  // deplie tout le chemin jusqu'a la carte, et la met en evidence
  chemin.slice(0, -1).forEach(k => commerce.ouverts.add(k));
  vueRent.cible = chemin[chemin.length - 1];
  // un filtre pourrait cacher la carte qu'on vient demander
  effacerFiltres();
  majBoutonFiltres();
  ouvrirOnglet('rent');
  const tr = document.querySelector('tr[data-cle="' + CSS.escape(vueRent.cible) + '"]');
  if (tr) tr.scrollIntoView({ block: 'center' });
}

function dessinerRent() {
  const g = commerce.globale;
  const kpis = document.getElementById('rent-kpis');
  kpis.replaceChildren();
  if (!commerce.dispo || !g) {
    kpis.appendChild(el('p', { className: 'lecture',
      textContent: 'Le service de rentabilité ne répond pas.' }));
    return;
  }
  const marge = g.pv ? Math.round(100 * g.roi / g.pv) : null;
  kpis.append(
    kpi('PA', euro(g.pa), 'achats par mois'),
    kpi('CA', euro(g.pv), 'ventes par mois'),
    kpi('ROI', signe(g.roi), marge === null ? '' : 'marge ' + marge + ' %',
        classeMarge(g.roi)),
    kpi('Parc', g.nb_serveurs + ' serveurs',
        g.nb_clusters + ' clusters · ' + g.nb_vm + ' VM'),
    kpi('À perte', String(g.groupes_a_perte),
        g.serveurs_a_perte + ' serveur(s) pris seuls',
        g.groupes_a_perte ? 'negatif' : ''),
  );
  if (g.capacite && g.capacite.ram_pct !== undefined) {
    kpis.appendChild(kpi('RAM allouée', pct(g.capacite.ram_pct),
      go(g.capacite.ram_libre_mb) + ' libres',
      g.capacite.ram_pct > 100 ? 'negatif' : ''));
  }

  if (filtreRentActif()) {
    kpis.appendChild(el('p', { className: 'lecture kpi-note', textContent:
      'Indicateurs du parc entier. Les filtres s’appliquent à l’arbre, au '
      + 'tableau et aux graphiques ci-dessous ; un montant affiché reste le '
      + 'total de sa carte.' }));
  }
  const avert = document.getElementById('rent-avert');
  avert.replaceChildren();
  const lignes = [...g.alertes];
  if (g.serveurs_sans_tarif) {
    lignes.push(g.serveurs_sans_tarif + ' serveur(s) sans tarif OVH : PA '
      + 'sous-estimé. Corrige-les à la main, ou donne le droit GET /services '
      + 'à la clef OVH.');
  }
  if (g.nb_orphelins) {
    lignes.push(g.nb_orphelins + ' saisie(s) hors parc, comptée(s) dans le '
      + 'total : ' + euro(g.pv_hors_parc) + ' de CA, ' + euro(g.pa_hors_parc)
      + ' de PA. Détail en bas de page.');
  }
  lignes.forEach(t => avert.appendChild(el('p', { textContent: '⚠ ' + t })));

  if (vueRent.mode === 'tableau') dessinerPlat();
  else if (vueRent.mode === 'graphiques') dessinerGraphes();
  else dessinerArbre();
  dessinerOrphelins();
  majModifiee();
}

function celluleMontant(c, quoi) {
  const corr = c.corrections[quoi === 'pa' ? 'prix_achat' : 'prix_vente'];
  const t = el('td', { className: 'num', textContent: euro(c[quoi]) });
  if (corr) {
    t.classList.add('corrige');
    t.title = (quoi === 'pa' ? 'PA' : 'PV') + ' propre corrigé à ' + euro(corr.valeur)
      + (quoi === 'pa' && corr.auto !== null ? ' (collecté : ' + euro(corr.auto) + ')' : '')
      + ' par ' + corr.par + ' — ' + corr.motif;
  }
  return t;
}

function enteteArbre() {
  const tr = el('tr', null, el('th', { textContent: 'Carte' }),
    ...colonnesNiveau('arbre').map(k => el('th', { className: k.num ? 'num' : '',
                                                   textContent: k.titre })),
    el('th'));
  document.getElementById('arbre-tete').replaceChildren(tr);
}

function dessinerArbre() {
  const corps = document.getElementById('rent-corps');
  corps.replaceChildren();
  enteteArbre();
  const filtre = vueRent.filtre || vueRent.perte || vueRent.alertes;
  const ajouter = (c, prof, chemin) => {
    if (filtre && !garde(c)) return;
    const k = chemin.concat([cleIndex(c)]);
    const cle = k[k.length - 1];
    const ouvert = filtre || commerce.ouverts.has(k.join('>'))
                   || commerce.ouverts.has(cle);
    corps.appendChild(ligneRent(c, prof, k, ouvert));
    if (ouvert) c.enfants.forEach(e => ajouter(e, prof + 1, k));
  };
  commerce.arbre.forEach(c => ajouter(c, 0, []));
  if (!corps.children.length) {
    corps.appendChild(el('tr', null, el('td', { colSpan: colonnesNiveau('arbre').length + 2,
      className: 'lecture', textContent: 'Aucune carte ne correspond.' })));
  }
}

function dessinerOrphelins() {
  const orph = document.getElementById('rent-orphelins');
  orph.replaceChildren();
  if (commerce.orphelins.length) {
    orph.appendChild(el('h4', { textContent: 'Hors parc' }));
    orph.appendChild(el('p', { className: 'lecture', textContent:
      'Ces saisies ne se rattachent à rien de l’inventaire : VM détruite ou '
      + 'renommée, parent retiré. Elles restent comptées dans le total.' }));
    const ul = el('ul');
    for (const o of commerce.orphelins) {
      ul.appendChild(el('li', null,
        el('span', { className: 'nom', textContent: o.genre + ' · ' + o.nom
          + (o.contrat ? ' · ' + o.contrat : '') }),
        el('span', { className: 'lecture', textContent: o.motif }),
        el('span', { className: 'prix', textContent: euro(o.pv) })));
    }
    orph.appendChild(ul);
  }
}

function kpi(titre, valeur, sous, classe) {
  const b = el('b', { textContent: valeur });
  if (classe) b.className = 'marge ' + classe;
  return el('div', { className: 'kpi' }, el('span', { textContent: titre }), b,
            el('small', { textContent: sous || '' }));
}

function jauge(cap) {
  if (!cap || cap.ram_pct === undefined) return el('span', { className: 'lecture', textContent: '—' });
  const p = cap.ram_pct;
  const barre = el('i');
  barre.style.width = Math.min(100, p) + '%';
  const cadre = el('span', { className: 'jauge' + (p > 100 ? ' trop'
                                    : p >= 85 ? ' tendue' : '') }, barre);
  const txt = 'RAM ' + pct(p)
    + (cap.vcpu_ratio !== undefined ? ' · vCPU ' + cap.vcpu_ratio.toLocaleString('fr-FR') + '×' : '');
  return el('span', { className: 'capa', title: go(cap.alloue.ram_mb) + ' alloués sur '
    + go(cap.physique.ram_mb) + ' · ' + cap.alloue.vcpu + ' vCPU sur '
    + (cap.physique.vcpu || '?') + ' threads' }, cadre, el('small', { textContent: txt }));
}

function ligneRent(c, prof, chemin, ouvert) {
  const cle = chemin.join('>');
  const tr = el('tr', { className: 'r-' + c.type
    + (vueRent.cible === chemin[chemin.length - 1] ? ' cible' : '') });
  tr.dataset.cle = chemin[chemin.length - 1];

  // nom, indente, avec le bouton de pliage
  const td = el('td', { className: 'nom' });
  td.style.paddingLeft = (8 + prof * 18) + 'px';
  if (c.enfants.length) {
    td.appendChild(el('button', { className: 'pli', textContent: ouvert ? '▾' : '▸',
      title: ouvert ? 'Replier' : 'Déplier', ariaExpanded: String(ouvert),
      onclick: () => {
        if (ouvert) { commerce.ouverts.delete(cle); commerce.ouverts.delete(chemin[chemin.length - 1]); }
        else commerce.ouverts.add(cle);
        vueRent.cible = null;
        dessinerRent();
      } }));
  } else {
    td.appendChild(el('span', { className: 'pli vide' }));
  }
  td.appendChild(el('span', { className: 'genre', textContent:
    c.type === 'element' ? (c.infos.role_libelle || 'élément')
    : c.type === 'cluster' && c.infos.seul ? 'nœud seul'
    : LIBELLE[c.type] }));
  if (c.type !== 'element' && c.infos.role_libelle) {
    td.appendChild(el('span', { className: 'genre role', textContent: c.infos.role_libelle }));
  }
  td.appendChild(el('span', { className: 'titre', textContent: c.nom }));
  const sous = [];
  if (c.type === 'cluster' && c.infos.manuel) sous.push('regroupement manuel');
  if (c.type === 'cluster' && c.infos.datacenters) sous.push(c.infos.datacenters.join(' '));
  if (c.type === 'serveur') sous.push([c.infos.offre, c.infos.datacenter, c.infos.noeud].filter(Boolean).join(' · '));
  if (c.type === 'vm') sous.push([c.infos.vmid, c.infos.statut, (c.infos.cpus || '?') + ' vCPU', go(c.infos.ram_mb)].filter(Boolean).join(' · '));
  if (c.type === 'element') sous.push([c.infos.client, c.infos.contrat].filter(Boolean).join(' · '));
  if (c.type === 'vm' && (c.infos.contrats || []).length) {
    sous.push(c.infos.contrats.length + ' contrat(s)');
  }
  if (sous.filter(Boolean).length) td.appendChild(el('small', { textContent: sous.filter(Boolean).join(' · ') }));
  if (c.alertes.length) {
    td.appendChild(el('span', { className: 'alerte', textContent: '⚠',
                                title: c.alertes.join('\n') }));
  }
  tr.appendChild(td);

  for (const k of colonnesNiveau('arbre')) tr.appendChild(k.cellule(c));

  tr.appendChild(actionsCarte(c));
  return tr;
}

// les memes boutons dans l'arbre et dans le tableau
function actionsCarte(c) {
  const act = el('td', { className: 'act' });
  if (commerce.peutEcrire) {
    act.appendChild(el('button', { textContent: '+ enfants',
      title: 'Déclarer un ou plusieurs éléments sous ' + c.nom,
      onclick: () => formEnfants(c) }));
    if (c.type === 'element') {
      act.appendChild(el('button', { textContent: 'Éditer',
                                     onclick: () => formElement(null, c) }));
    } else {
      act.appendChild(el('button', { textContent: 'Modifier',
                                     title: 'Prix et rôle',
                                     onclick: () => formCorrection(c) }));
    }
    if (c.type === 'vm') {
      act.appendChild(el('button', { textContent: 'Contrat',
                                     onclick: () => formVente(c, null) }));
    }
  }
  return act;
}

