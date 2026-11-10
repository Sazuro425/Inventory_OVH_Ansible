// ------------------------------------------------------------- graphiques
// Quatre lectures du meme arbre, chacune repondant a une question :
//   - quel cluster gagne, lequel perd           barres divergentes (ROI)
//   - d'ou vient le resultat                    CA et PA cote a cote
//   - quels hyperviseurs peuvent encore prendre RAM allouee, seuils 85/100 %
//   - de qui depend le chiffre d'affaires       CA par client
// Les montants sont ceux de l'API, rien n'est recalcule. Le mode Tableau
// reste la vue chiffree complete : un graphique n'est jamais le seul acces a
// une valeur.
const VIZ = { barre: 16, ligne: 28, haut: 20, bas: 28, droite: 16 };

const euroCourt = v => {
  const a = Math.abs(v);
  const t = a >= 1000
    ? (a / 1000).toLocaleString('fr-FR', { maximumFractionDigits: 1 }) + ' k€'
    : a.toLocaleString('fr-FR', { maximumFractionDigits: 0 }) + ' €';
  return (v < 0 ? '−' : '') + t;
};

// graduations « rondes » : 1, 2 ou 5 fois une puissance de dix
function graduations(min, max, n) {
  const brut = (max - min) / Math.max(1, n);
  const p = Math.pow(10, Math.floor(Math.log10(brut || 1)));
  const pas = [1, 2, 5, 10].map(m => m * p).find(x => x >= brut) || brut;
  const out = [];
  for (let v = Math.ceil(min / pas) * pas; v <= max + 1e-9; v += pas) {
    out.push(Math.abs(v) < 1e-9 ? 0 : v);
  }
  return out;
}

// Barre horizontale : extremite de donnee arrondie (4 px), carree a la base.
function barreH(x0, x1, y, h) {
  const r = Math.min(4, Math.abs(x1 - x0), h / 2);
  if (x1 >= x0) {
    return 'M' + x0 + ',' + y + 'H' + (x1 - r) + 'A' + r + ',' + r + ' 0 0 1 '
      + x1 + ',' + (y + r) + 'V' + (y + h - r) + 'A' + r + ',' + r + ' 0 0 1 '
      + (x1 - r) + ',' + (y + h) + 'H' + x0 + 'Z';
  }
  return 'M' + x0 + ',' + y + 'H' + (x1 + r) + 'A' + r + ',' + r + ' 0 0 0 '
    + x1 + ',' + (y + r) + 'V' + (y + h - r) + 'A' + r + ',' + r + ' 0 0 0 '
    + (x1 + r) + ',' + (y + h) + 'H' + x0 + 'Z';
}

// --- bulle de survol, partagee par les quatre graphiques
const bulle = document.getElementById('viz-bulle');
function montrerBulle(ev, titre, lignes) {
  bulle.replaceChildren(el('b', { textContent: titre }),
    ...lignes.map(([k, v]) => el('div', null, el('span', { textContent: k }),
                                 el('span', { textContent: v }))));
  bulle.hidden = false;
  const m = 14, r = bulle.getBoundingClientRect();
  let x = ev.clientX + m, y = ev.clientY + m;
  if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - m;
  if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - m;
  bulle.style.left = Math.max(8, x) + 'px';
  bulle.style.top = Math.max(8, y) + 'px';
}
function cacherBulle() { bulle.hidden = true; }

// Ossature commune : un SVG a la largeur du conteneur, une colonne de noms a
// gauche dimensionnee sur le plus long, l'axe des valeurs en bas.
function cadreBarres(zone, rangs, opts) {
  const largeur = Math.max(320, zone.clientWidth);
  const mesure = svgEl('text', { class: 'viz-nom' });
  const sonde = svgEl('svg', { width: 0, height: 0 });
  sonde.appendChild(mesure);
  zone.appendChild(sonde);
  let gauche = 60;
  for (const r of rangs) {
    mesure.textContent = r.nom;
    gauche = Math.max(gauche, mesure.getComputedTextLength() + 14);
  }
  sonde.remove();
  gauche = Math.min(gauche, largeur * 0.38);
  const hauteur = VIZ.haut + rangs.length * VIZ.ligne + VIZ.bas;
  const svg = svgEl('svg', { width: largeur, height: hauteur, role: 'img',
                             'aria-label': opts.resume });
  const [min, max] = opts.domaine;
  const x0 = gauche + (opts.margeLabel || 0);
  const x1 = largeur - VIZ.droite - (opts.margeLabel || 0);
  const x = v => x0 + (v - min) / ((max - min) || 1) * (x1 - x0);
  // grille verticale, en retrait ; la ligne de base porte l'axe
  const g = svgEl('g', { class: 'viz-grille' });
  for (const t of graduations(min, max, Math.max(3, Math.floor((x1 - x0) / 70)))) {
    g.appendChild(svgEl('line', { x1: x(t), x2: x(t), y1: VIZ.haut - 4,
      y2: hauteur - VIZ.bas, class: t === 0 ? 'viz-base' : '' }));
    g.appendChild(svgEl('text', { x: x(t), y: hauteur - VIZ.bas + 16,
      class: 'viz-axe', 'text-anchor': 'middle' }, opts.format(t)));
  }
  svg.appendChild(g);
  zone.appendChild(svg);
  return { svg, x, gauche, largeur, hauteur,
           y: i => VIZ.haut + i * VIZ.ligne };
}

// Une rangee : le nom, une zone de survol plus large que la barre, et ce que
// dessine l'appelant. Survoler eclaircit les autres rangees.
function rangee(cadre, i, nom, bulleFn, clic) {
  const g = svgEl('g', { class: 'viz-rang' + (clic ? ' cliquable' : '') });
  const y = cadre.y(i);
  g.appendChild(svgEl('rect', { x: 0, y, width: cadre.largeur,
    height: VIZ.ligne, class: 'viz-cible' }));
  g.appendChild(svgEl('text', { x: cadre.gauche - 10, y: y + VIZ.ligne / 2 + 4,
    class: 'viz-nom', 'text-anchor': 'end' }, nom));
  g.addEventListener('mousemove', ev => montrerBulle(ev, ...bulleFn()));
  g.addEventListener('mouseleave', cacherBulle);
  if (clic) g.addEventListener('click', () => { cacherBulle(); clic(); });
  cadre.svg.appendChild(g);
  return { g, y: y + (VIZ.ligne - VIZ.barre) / 2 };
}

function libelleGroupe(c) {
  return c.infos.seul ? c.nom.split('.')[0] + ' (seul)' : c.nom;
}

// Chaque figure a une seule source : rangs(), filtres appliques. Le dessin,
// le tableau et le CSV en sont trois lectures, ils ne peuvent donc pas se
// contredire. colonnes suit le format de COLONNES_PLAT (val, num, euro...).
const FIGURES = [
  {
    id: 'viz-roi', cle: 'roi', titre: 'Résultat par cluster',
    sous: 'ROI mensuel, CA − PA. Un clic ouvre le cluster dans l’arbre.',
    fichier: 'resultat-par-cluster',
    rangs: () => lignesFiltrees('cluster')
      .map(r => ({ c: r.c, nom: libelleGroupe(r.c) }))
      .sort((a, b) => b.c.roi - a.c.roi),
    colonnes: [
      { titre: 'Cluster', val: r => r.nom },
      { titre: 'Serveurs', num: true, dec: 0,
        val: r => r.c.enfants.filter(e => e.type === 'serveur').length },
      { titre: 'PA / mois', num: true, euro: true, somme: true, val: r => r.c.pa },
      { titre: 'CA / mois', num: true, euro: true, somme: true, val: r => r.c.pv },
      { titre: 'ROI / mois', num: true, euro: true, somme: true, signe: true,
        val: r => r.c.roi },
      { titre: 'Marge %', num: true, dec: 1, val: r => marge(r.c) },
    ],
    dessiner: dessinRoi,
  },
  {
    id: 'viz-capa', cle: 'capa', titre: 'Ce qui rentre, ce qui sort',
    sous: 'Chiffre d’affaires et prix d’achat mensuels, par cluster.',
    fichier: 'ca-et-pa-par-cluster',
    rangs: () => lignesFiltrees('cluster')
      .map(r => ({ c: r.c, nom: libelleGroupe(r.c) }))
      .sort((a, b) => b.c.pv - a.c.pv),
    colonnes: [
      { titre: 'Cluster', val: r => r.nom },
      { titre: 'CA / mois', num: true, euro: true, somme: true, val: r => r.c.pv },
      { titre: 'PA / mois', num: true, euro: true, somme: true, val: r => r.c.pa },
      { titre: 'ROI / mois', num: true, euro: true, somme: true, signe: true,
        val: r => r.c.roi },
    ],
    dessiner: dessinCaPa,
  },
  {
    id: 'viz-clients', cle: 'clients', titre: 'Chiffre d’affaires par client',
    sous: 'Contrats et éléments déclarés, ramenés au mois ; part du CA total.',
    fichier: 'ca-par-client',
    rangs: rangsClients,
    colonnes: [
      { titre: 'Client', val: r => r.nom },
      { titre: 'CA / mois', num: true, euro: true, somme: true, val: r => r.v },
      { titre: 'Part du CA %', num: true, dec: 1, val: r => r.part },
    ],
    dessiner: dessinClients,
  },
  {
    id: 'viz-ram', cle: 'ram', titre: 'RAM allouée par serveur',
    sous: 'En % de la RAM physique. ▲ au-delà de 85 % l’hôte est tendu, ⚠ au-delà de 100 % il est surréservé.',
    fichier: 'ram-par-serveur',
    rangs: () => lignesFiltrees('serveur')
      .filter(r => r.c.capacite && r.c.capacite.ram_pct !== undefined)
      .map(r => ({ c: r.c, groupe: r.groupe, nom: r.c.nom.split('.')[0]
        + (r.c.infos.noeud ? ' · ' + r.c.infos.noeud : '') }))
      .sort((a, b) => b.c.capacite.ram_pct - a.c.capacite.ram_pct),
    colonnes: [
      { titre: 'Serveur', val: r => r.c.nom },
      { titre: 'Nœud', val: r => r.c.infos.noeud },
      { titre: 'Cluster', val: r => r.groupe },
      { titre: 'RAM allouée (Go)', num: true, dec: 0,
        val: r => r.c.capacite.alloue.ram_mb / 1024 },
      { titre: 'RAM physique (Go)', num: true, dec: 0,
        val: r => (r.c.capacite.physique.ram_mb || 0) / 1024 },
      { titre: 'RAM allouée %', num: true, dec: 1, val: r => r.c.capacite.ram_pct },
      { titre: 'RAM libre (Go)', num: true, dec: 0,
        val: r => r.c.capacite.ram_libre_mb / 1024 },
      { titre: 'vCPU / thread', num: true, dec: 2, val: r => r.c.capacite.vcpu_ratio },
      { titre: 'État', val: r => r.c.capacite.ram_pct > 100 ? '⚠ surréservée'
          : r.c.capacite.ram_pct >= 85 ? '▲ tendue' : 'ok' },
    ],
    dessiner: dessinRam,
  },
];

// Le CA par client se lit sur les cartes qui le portent : contrats des VM,
// elements declares. Seules les cartes retenues par les filtres comptent ;
// les contrats hors parc, seulement quand aucun filtre de parc n'est pose.
function rangsClients() {
  const par = new Map();
  const ajouter = (client, v) => {
    const k = client || 'sans client';
    par.set(k, (par.get(k) || 0) + Number(v || 0));
  };
  for (const r of lignesFiltrees('vm')) {
    // une correction du PV remplace les contrats : on suit la valeur retenue
    if (r.c.corrections.prix_vente) ajouter('correction manuelle', r.c.pv_propre);
    else for (const v of contrats(r.c)) {
      if (!selection.clients.size || selection.clients.has(v.client)) {
        ajouter(v.client, v.vente_mois);
      }
    }
  }
  for (const r of lignesFiltrees('element')) {
    if (!selection.clients.size || selection.clients.has(r.c.infos.client)) {
      ajouter(r.c.infos.client, r.c.pv_propre);
    }
  }
  if (!filtreDeParc()) {
    for (const o of commerce.orphelins) {
      if (!selection.clients.size || selection.clients.has(o.client)) {
        ajouter(o.client, o.pv);
      }
    }
  }
  let rangs = [...par.entries()].map(([nom, v]) => ({ nom, v }))
    .filter(r => r.v > 0).sort((a, b) => b.v - a.v);
  const total = rangs.reduce((t, r) => t + r.v, 0);
  rangs.forEach(r => { r.part = total ? Math.round(1000 * r.v / total) / 10 : 0; });
  return rangs;
}

function dessinRoi(zone, rangs) {
  const min = Math.min(0, ...rangs.map(r => r.c.roi));
  const max = Math.max(0, ...rangs.map(r => r.c.roi));
  const cadre = cadreBarres(zone, rangs, {
    domaine: [min, max], format: euroCourt, margeLabel: 64,
    resume: 'ROI mensuel par cluster, de ' + euroCourt(max) + ' à ' + euroCourt(min),
  });
  rangs.forEach((r, i) => {
    const c = r.c;
    const { g, y } = rangee(cadre, i, r.nom, () => [r.nom, [
      ['PA', euro(c.pa)], ['CA', euro(c.pv)], ['ROI', signe(c.roi)],
      ['Marge', c.pv ? Math.round(100 * c.roi / c.pv) + ' %' : '—'],
      ['Serveurs', String(c.enfants.filter(e => e.type === 'serveur').length)],
    ]], () => basculerMode('arbre', [cleIndex(c)]));
    const xa = cadre.x(0), xb = cadre.x(c.roi);
    g.appendChild(svgEl('path', { d: barreH(xa, xb, y, VIZ.barre),
      class: c.roi < 0 ? 'viz-perte' : 'viz-gain' }));
    // la valeur, signee, en encre de texte : le signe dit le sens sans
    // dependre de la couleur
    g.appendChild(svgEl('text', { x: xb + (c.roi < 0 ? -6 : 6),
      y: y + VIZ.barre / 2 + 4, class: 'viz-valeur',
      'text-anchor': c.roi < 0 ? 'end' : 'start' },
      (c.roi > 0 ? '+' : '') + euroCourt(c.roi)));
  });
}

function dessinCaPa(zone, rangs) {
  const max = Math.max(1, ...rangs.flatMap(r => [r.c.pv, r.c.pa]));
  zone.appendChild(el('div', { className: 'viz-legende' },
    el('span', null, el('i', { className: 'viz-ca' }), 'CA / mois'),
    el('span', null, el('i', { className: 'viz-pa' }), 'PA / mois')));
  const cadre = cadreBarres(zone, rangs, {
    domaine: [0, max], format: euroCourt,
    resume: 'Chiffre d’affaires et prix d’achat mensuels par cluster',
  });
  const h = (VIZ.barre - 2) / 2;   // deux barres et l'interstice de 2 px
  rangs.forEach((r, i) => {
    const c = r.c;
    const { g, y } = rangee(cadre, i, r.nom, () => [r.nom, [
      ['CA', euro(c.pv)], ['PA', euro(c.pa)], ['ROI', signe(c.roi)],
    ]], () => basculerMode('arbre', [cleIndex(c)]));
    g.appendChild(svgEl('path', { d: barreH(cadre.x(0), cadre.x(c.pv), y, h),
                                  class: 'viz-ca' }));
    g.appendChild(svgEl('path', { d: barreH(cadre.x(0), cadre.x(c.pa), y + h + 2, h),
                                  class: 'viz-pa' }));
  });
}

function dessinRam(zone, rangs) {
  const max = Math.max(110, ...rangs.map(r => r.c.capacite.ram_pct));
  const cadre = cadreBarres(zone, rangs, {
    domaine: [0, max], format: v => v + ' %', margeLabel: 0,
    resume: 'RAM allouée par serveur, en pourcentage de la RAM physique',
  });
  // seuils nommes, pas seulement colores ; les deux etiquettes de part et
  // d'autre de leur ligne, sinon elles se chevauchent au-dela de 150 %
  for (const [v, t, ancre, dx] of [[85, '▲ 85 %', 'end', -4],
                                   [100, '⚠ 100 %', 'start', 4]]) {
    cadre.svg.appendChild(svgEl('line', { x1: cadre.x(v), x2: cadre.x(v),
      y1: VIZ.haut - 4, y2: cadre.hauteur - VIZ.bas, class: 'viz-seuil' }));
    cadre.svg.appendChild(svgEl('text', { x: cadre.x(v) + dx, y: VIZ.haut - 8,
      'text-anchor': ancre, class: 'viz-axe' }, t));
  }
  rangs.forEach((r, i) => {
    const cap = r.c.capacite, p = cap.ram_pct;
    const trop = p > 100, tendu = p >= 85;
    const { g, y } = rangee(cadre, i, r.nom, () => [r.c.nom, [
      ['Cluster', r.groupe],
      ['RAM allouée', go(cap.alloue.ram_mb) + ' sur ' + go(cap.physique.ram_mb)],
      ['Taux', pct(p) + (trop ? ' — surréservée' : tendu ? ' — tendue' : '')],
      ['RAM libre', go(cap.ram_libre_mb)],
      ['vCPU', cap.alloue.vcpu + ' sur ' + (cap.physique.vcpu || '?') + ' threads'],
    ]], () => basculerMode('arbre', commerce.index.get(cleIndex(r.c)).chemin));
    g.appendChild(svgEl('path', { d: barreH(cadre.x(0), cadre.x(p), y, VIZ.barre),
      class: trop ? 'viz-critique' : tendu ? 'viz-tendu' : 'viz-ram' }));
    const fin = cadre.x(p);
    const dedans = fin - cadre.x(0) > 70;
    // l'etat porte un pictogramme en plus de sa couleur : ▲ tendu, ⚠ surreserve
    g.appendChild(svgEl('text', { x: dedans ? fin - 6 : fin + 6,
      y: y + VIZ.barre / 2 + 4, 'text-anchor': dedans ? 'end' : 'start',
      class: 'viz-valeur' + (dedans ? (trop || !tendu ? ' dedans' : ' dedans-sombre') : '') },
      (trop ? '⚠ ' : tendu ? '▲ ' : '') + Math.round(p) + ' %'));
  });
}

function dessinClients(zone, rangs) {
  // au-dela de dix, la longue traine se replie en « Autres » — sur le dessin
  // seulement : le tableau et le CSV gardent chaque client
  if (rangs.length > 10) {
    const reste = rangs.slice(9);
    rangs = rangs.slice(0, 9).concat([{ nom: 'Autres (' + reste.length + ')',
      v: reste.reduce((t, r) => t + r.v, 0),
      part: Math.round(10 * reste.reduce((t, r) => t + r.part, 0)) / 10 }]);
  }
  const max = Math.max(...rangs.map(r => r.v));
  const cadre = cadreBarres(zone, rangs, {
    domaine: [0, max], format: euroCourt, margeLabel: 0,
    resume: 'Chiffre d’affaires mensuel par client',
  });
  rangs.forEach((r, i) => {
    const { g, y } = rangee(cadre, i, r.nom, () => [r.nom, [
      ['CA / mois', euro(r.v)], ['Part du CA', r.part.toLocaleString('fr-FR') + ' %'],
    ]]);
    const fin = cadre.x(r.v);
    g.appendChild(svgEl('path', { d: barreH(cadre.x(0), fin, y, VIZ.barre),
                                  class: 'viz-ca' }));
    const dedans = fin - cadre.x(0) > 90;
    g.appendChild(svgEl('text', { x: dedans ? fin - 6 : fin + 6,
      y: y + VIZ.barre / 2 + 4, 'text-anchor': dedans ? 'end' : 'start',
      class: dedans ? 'viz-valeur dedans' : 'viz-valeur' },
      r.part.toLocaleString('fr-FR') + ' %'));
  });
}

function graphVide(zone) {
  zone.appendChild(el('p', { className: 'lecture',
    textContent: filtreRentActif() ? 'Rien ne correspond aux filtres.'
                               : 'Rien à représenter.' }));
}

// Une figure en tableau : memes rangs, memes colonnes que le CSV, et un
// total pour les montants.
function tableFigure(zone, f, rangs) {
  const t = el('table', { className: 'arbre plat mini' });
  t.appendChild(el('thead', null, el('tr', null, ...f.colonnes.map(k =>
    el('th', { className: k.num ? 'num' : '', scope: 'col', textContent: k.titre })))));
  const corps = el('tbody');
  for (const r of rangs) {
    corps.appendChild(el('tr', null, ...f.colonnes.map(k => {
      const v = k.val(r);
      return el('td', { textContent: formatCellule(k, v),
        className: (k.num ? 'num' : '') + (k.signe ? ' marge ' + classeMarge(v) : '') });
    })));
  }
  t.appendChild(corps);
  if (rangs.length > 1 && f.colonnes.some(k => k.somme)) {
    t.appendChild(el('tfoot', null, el('tr', null, ...f.colonnes.map((k, i) => {
      if (i === 0) return el('th', { scope: 'row', textContent: 'Total' });
      if (!k.somme) return el('td');
      const s = rangs.reduce((x, r) => x + Number(k.val(r) || 0), 0);
      return el('td', { textContent: formatCellule(k, s),
        className: 'num' + (k.signe ? ' marge ' + classeMarge(s) : '') });
    }))));
  }
  zone.appendChild(el('div', { className: 'table-defile mini' }, t));
}

// Le dessin des figures, et leurs outils, passent par les panneaux du
// tableau de bord : voir 04b-panneaux.js.
