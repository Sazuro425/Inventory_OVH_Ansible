// ------------------------------------------------------------ historique
// L'historique vit en base, un point par jour et par carte (migration
// 0005). Deux lectures :
//   - la projection de capacite : quand chaque cluster atteindra 85 % puis
//     100 % de RAM allouee, au rythme des 90 derniers jours (capacite.py) ;
//   - les courbes : le dernier point de chaque mois, sur deux ans.
// Rien n'est charge tant qu'aucun panneau n'en a besoin ; une page ouverte
// sans le service (fichier, hors ligne) n'en affiche simplement rien.

const histo = { projection: null, series: {}, enCours: new Set(), erreur: {} };

function chargerHisto(cle, url, ranger) {
  if (histo.enCours.has(cle)) return;
  histo.enCours.add(cle);
  fetch(url, { headers: { Accept: 'application/json' } })
    .then(r => r.ok ? r.json() : Promise.reject(new Error('http ' + r.status)))
    .then(ranger)
    .catch(e => { histo.erreur[cle] = e.message; })
    .finally(() => {
      histo.enCours.delete(cle);
      if (vueRent.actif && vueRent.mode === 'graphiques') dessinerGraphes();
    });
}

const chargerProjection = () => chargerHisto('projection', '/api/projection',
  d => { histo.projection = d; });
const chargerSeries = niveau => chargerHisto('series:' + niveau,
  '/api/historique?niveau=' + encodeURIComponent(niveau),
  d => { histo.series[niveau] = d.series || []; });

const NIVEAUX_COURBE = [['parc', 'Parc entier'], ['cluster', 'Clusters'],
                        ['serveur', 'Serveurs']];
const niveauTexte = n => (NIVEAUX_COURBE.find(x => x[0] === n) || [, LIBELLE[n] || n])[1];
const SERIES_MAX = 4;

// Ce qu'on suit dans le temps. Un ratio se recalcule depuis les termes du
// point, comme dans les panneaux.
const MESURES_HISTO = [
  { id: 'pv', titre: 'CA / mois', euro: true, val: p => p.pv },
  { id: 'pa', titre: 'PA / mois', euro: true, val: p => p.pa },
  { id: 'roi', titre: 'ROI / mois', euro: true, signe: true, val: p => p.roi },
  { id: 'marge', titre: 'Marge', unite: ' %', dec: 1,
    val: p => p.pv ? 100 * p.roi / p.pv : null },
  { id: 'ram_pct', titre: 'RAM allouée', unite: ' %', dec: 1,
    val: p => p.ram_physique_mb ? 100 * p.ram_alloue_mb / p.ram_physique_mb : null },
  { id: 'ram_alloue', titre: 'RAM allouée (Go)', dec: 0,
    val: p => p.ram_alloue_mb === null ? null : p.ram_alloue_mb / 1024 },
  { id: 'vcpu_ratio', titre: 'vCPU par thread', dec: 2,
    val: p => p.vcpu_physique ? p.vcpu_alloue / p.vcpu_physique : null },
  { id: 'nb_vm', titre: 'VM', dec: 0, val: p => p.nb_vm },
];
const mesureHisto = id => MESURES_HISTO.find(m => m.id === id);

const MOIS = ['janv.', 'févr.', 'mars', 'avr.', 'mai', 'juin', 'juil.', 'août',
              'sept.', 'oct.', 'nov.', 'déc.'];
const libelleMois = m => MOIS[Number(m.slice(5, 7)) - 1] + ' ' + m.slice(2, 4);
const dateLongue = iso => new Date(iso + 'T12:00:00').toLocaleDateString('fr-FR',
  { day: 'numeric', month: 'long', year: 'numeric' });

// Les series d'un panneau courbe : celles que les filtres de la vue gardent,
// les plus fortes au dernier point, SERIES_MAX au plus.
function donneesCourbe(p) {
  const m = mesureHisto(p.mesure);
  if (!m || !NIVEAUX_COURBE.some(n => n[0] === p.niveau)) {
    return { erreur: 'Mesure ou niveau indisponible pour une courbe : modifie le panneau.' };
  }
  const brutes = histo.series[p.niveau];
  if (!brutes) {
    if (!histo.erreur['series:' + p.niveau]) chargerSeries(p.niveau);
    return { attente: !histo.erreur['series:' + p.niveau],
             erreur: histo.erreur['series:' + p.niveau] ? 'historique illisible' : null };
  }
  let series = brutes;
  if (p.niveau !== 'parc' && filtreRentActif()) {
    const gardes = new Set(lignesFiltrees(p.niveau).map(r => r.c.cle));
    series = series.filter(s => gardes.has(s.cle));
  }
  const dernier = s => { const pt = s.points[s.points.length - 1]; return pt ? m.val(pt) : null; };
  series = series.filter(s => s.points.some(pt => m.val(pt) !== null))
    .sort((a, b) => (dernier(b) ?? -Infinity) - (dernier(a) ?? -Infinity))
    .slice(0, Math.min(SERIES_MAX, p.top || SERIES_MAX))
    .map(s => ({ ...s, nom: p.niveau === 'serveur' ? s.nom.split('.')[0] : s.nom }));
  const mois = [...new Set(series.flatMap(s => s.points.map(pt => pt.mois)))].sort();
  return { m, series, mois };
}

// Courbes : une ligne de 2 px par serie, un repere a chaque mois, la
// legende au-dessus, le nom en bout de ligne, un reticule au survol qui
// donne toutes les valeurs du mois.
function dessinCourbe(zone, p, d) {
  if (!d.mois.length) {
    return zone.appendChild(el('p', { className: 'lecture', textContent:
      'Pas encore d’historique : la collecte en ajoute un point par jour, '
      + 'le premier mois apparaît dès la prochaine collecte.' }));
  }
  const largeur = Math.max(320, zone.clientWidth), haut = 220;
  const g = 52, dr = 92, h0 = 14, b = 26;
  const valeurs = d.series.flatMap(s => s.points.map(pt => d.m.val(pt))).filter(v => v !== null);
  let min = Math.min(0, ...valeurs), max = Math.max(0, ...valeurs);
  if (min === max) max = min + 1;
  const x = i => g + (d.mois.length === 1 ? (largeur - g - dr) / 2
                                          : i * (largeur - g - dr) / (d.mois.length - 1));
  const y = v => h0 + (max - v) / (max - min) * (haut - h0 - b);
  const fmt = v => fmtMesure(d.m, v, true);

  zone.appendChild(el('div', { className: 'legende' }, ...d.series.map((s, i) =>
    el('span', null, el('i', { className: 'serie-' + (i + 1) }), s.nom))));
  const svg = svgEl('svg', { width: largeur, height: haut, role: 'img',
    'aria-label': titrePanneau(p) + ', ' + d.mois.length + ' mois' });
  const grille = svgEl('g', { class: 'viz-grille' });
  for (const t of graduations(min, max, 4)) {
    grille.appendChild(svgEl('line', { x1: g, x2: largeur - dr, y1: y(t), y2: y(t),
                                       class: t === 0 ? 'viz-base' : '' }));
    grille.appendChild(svgEl('text', { x: g - 6, y: y(t) + 4, class: 'viz-axe',
                                       'text-anchor': 'end' }, fmt(t)));
  }
  // une etiquette de mois sur deux au-dela de douze, pour qu'elles respirent
  const pas = d.mois.length > 12 ? 2 : 1;
  d.mois.forEach((mo, i) => {
    if (i % pas && i !== d.mois.length - 1) return;
    grille.appendChild(svgEl('text', { x: x(i), y: haut - 8, class: 'viz-axe',
                                       'text-anchor': 'middle' }, libelleMois(mo)));
  });
  svg.appendChild(grille);
  d.series.forEach((s, k) => {
    const pts = s.points.map(pt => [d.mois.indexOf(pt.mois), d.m.val(pt)])
      .filter(([, v]) => v !== null);
    if (!pts.length) return;
    svg.appendChild(svgEl('path', { class: 'courbe serie-' + (k + 1),
      d: pts.map(([i, v], j) => (j ? 'L' : 'M') + x(i) + ',' + y(v)).join('') }));
    for (const [i, v] of pts) {
      svg.appendChild(svgEl('circle', { class: 'repere serie-' + (k + 1),
                                        cx: x(i), cy: y(v), r: 4 }));
    }
    const [iz, vz] = pts[pts.length - 1];
    svg.appendChild(svgEl('text', { x: x(iz) + 8, y: y(vz) + 4, class: 'viz-nom' },
                          s.nom.length > 12 ? s.nom.slice(0, 11) + '…' : s.nom));
  });
  const reticule = svgEl('line', { class: 'reticule', y1: h0, y2: haut - b, x1: 0, x2: 0,
                                   visibility: 'hidden' });
  svg.appendChild(reticule);
  const cible = svgEl('rect', { x: g, y: 0, width: largeur - g - dr, height: haut,
                                class: 'viz-cible' });
  cible.addEventListener('mousemove', ev => {
    const r = svg.getBoundingClientRect();
    const px = ev.clientX - r.left;
    let i = 0;
    d.mois.forEach((_, j) => { if (Math.abs(x(j) - px) < Math.abs(x(i) - px)) i = j; });
    reticule.setAttribute('x1', x(i));
    reticule.setAttribute('x2', x(i));
    reticule.setAttribute('visibility', 'visible');
    montrerBulle(ev, libelleMois(d.mois[i]), d.series.map(s => {
      const pt = s.points.find(q => q.mois === d.mois[i]);
      return [s.nom, pt ? fmtMesure(d.m, d.m.val(pt)) : '—'];
    }));
  });
  cible.addEventListener('mouseleave', () => {
    reticule.setAttribute('visibility', 'hidden');
    cacherBulle();
  });
  svg.appendChild(cible);
  zone.appendChild(svg);
}

// Les memes series en tableau, pour le CSV : un mois par ligne.
function colonnesCourbe(d) {
  return [{ titre: 'Mois', val: r => r.mois },
          ...d.series.map(s => ({ titre: s.nom, num: true, euro: d.m.euro, dec: d.m.dec,
            val: r => { const pt = s.points.find(q => q.mois === r.mois);
                        return pt ? d.m.val(pt) : null; } }))];
}

// ---------------------------------------------------- projection (figure)
// Combien de mois avant 100 % de RAM allouee, cluster par cluster : la
// barre est d'autant plus courte, et d'autant plus rouge, que l'achat
// presse. Au-dela de deux ans, la barre plafonne.
const MOIS_MAX = 24;

function moisAvant(iso) {
  return Math.max(0, (new Date(iso + 'T12:00:00') - Date.now()) / (30.44 * 86400000));
}

function rangsProjection() {
  if (!histo.projection) {
    if (!histo.erreur.projection) chargerProjection();
    return [];
  }
  const gardes = filtreRentActif() ? new Set(lignesFiltrees('cluster').map(r => r.c.cle)) : null;
  return histo.projection.projections
    .filter(p => !gardes || gardes.has(p.cle))
    .filter(p => p.etat !== 'inconnu')
    .map(p => {
      const carte = commerce.index.get('cluster:' + p.cle);
      return { ...p, nom: carte ? libelleGroupe(carte.carte) : p.nom };
    });
}

const pctFr = v => Number(v).toLocaleString('fr-FR', { maximumFractionDigits: 1 }) + ' %';

function dessinProjection(zone, rangs) {
  // la place des libelles a droite : le domaine depasse les 24 mois, sans
  // graduation au-dela
  const cadre = cadreBarres(zone, rangs, {
    domaine: [0, MOIS_MAX * 1.7], format: v => (v <= MOIS_MAX ? v + ' mois' : ''),
    margeLabel: 0,
    resume: 'Mois avant 100 % de RAM allouée, par cluster' });
  rangs.forEach((p, i) => {
    const urgent = m => m < 3 ? 'viz-critique' : m < 6 ? 'viz-tendu' : 'viz-ca';
    let mois = null, texte, classe;
    if (p.ram_100 === 'atteint') {
      mois = 0; texte = '⚠ déjà pleine (' + pctFr(p.ram_pct) + ')'; classe = 'viz-critique';
    } else if (p.ram_100) {
      mois = moisAvant(p.ram_100);
      texte = (mois < 1 ? 'sous un mois' : 'dans ' + Math.round(mois) + ' mois')
        + ' · ' + dateLongue(p.ram_100).replace(/^\d+ /, '');
      classe = urgent(mois);
    } else if (p.tendance === 'insuffisante') {
      texte = 'pas assez de recul (' + pctFr(p.ram_pct) + ' aujourd’hui)';
    } else {
      texte = (p.tendance === 'hausse' ? 'pas avant cinq ans' : 'stable ou en baisse')
        + ' (' + pctFr(p.ram_pct) + ')';
    }
    // deja au-dessus de 85 % : le dire, quelle que soit la tendance
    if (p.etat === 'tendu' && p.ram_100 !== 'atteint') texte = '▲ tendue · ' + texte;
    const infos = [['RAM allouée', pctFr(p.ram_pct) + ' (' + p.ram_alloue_go + ' / '
                    + p.ram_physique_go + ' Go)']];
    if (p.ram_go_par_mois !== undefined) {
      infos.push(['Croissance', (p.ram_go_par_mois > 0 ? '+' : '') + p.ram_go_par_mois
                  + ' Go / mois']);
    }
    if (p.ram_85) infos.push(['85 %', p.ram_85 === 'atteint' ? 'atteint' : dateLongue(p.ram_85)]);
    if (p.ram_100) infos.push(['100 %', p.ram_100 === 'atteint' ? 'atteint' : dateLongue(p.ram_100)]);
    if (p.vcpu_ratio !== undefined) infos.push(['vCPU par thread', String(p.vcpu_ratio)]);
    const carte = commerce.index.get('cluster:' + p.cle);
    const { g, y } = rangee(cadre, i, p.nom, () => [p.nom, infos],
      carte ? () => basculerMode('arbre', carte.chemin) : null);
    if (mois !== null) {
      const xb = cadre.x(Math.min(MOIS_MAX, Math.max(mois, 0.15)));
      g.appendChild(svgEl('path', { d: barreH(cadre.x(0), xb, y, VIZ.barre), class: classe }));
      g.appendChild(svgEl('text', { x: xb + 6, y: y + VIZ.barre / 2 + 4,
                                    class: 'viz-valeur' }, texte));
    } else {
      g.appendChild(svgEl('text', { x: cadre.x(0) + 6, y: y + VIZ.barre / 2 + 4,
                                    class: 'viz-valeur' }, texte));
    }
  });
}

FIGURES.push({
  id: 'viz-projection', cle: 'projection', titre: 'Quand reprendre un serveur',
  sous: 'Mois avant 100 % de RAM allouée, au rythme des 90 derniers jours. '
    + 'Rouge : moins de 3 mois ; ambre : moins de 6.',
  fichier: 'projection-capacite',
  vide: 'Pas encore assez d’historique : la projection demande deux semaines '
    + 'de points quotidiens. La collecte en ajoute un à chaque passage.',
  pret: () => !!histo.projection || !!histo.erreur.projection,
  rangs: rangsProjection,
  colonnes: [
    { titre: 'Cluster', val: r => r.nom },
    { titre: 'RAM allouée %', num: true, dec: 1, val: r => r.ram_pct },
    { titre: 'Allouée (Go)', num: true, dec: 0, val: r => r.ram_alloue_go },
    { titre: 'Physique (Go)', num: true, dec: 0, val: r => r.ram_physique_go },
    { titre: 'Croissance (Go/mois)', num: true, dec: 1, val: r => r.ram_go_par_mois ?? null },
    { titre: '85 % le', val: r => r.ram_85 || (r.tendance === 'insuffisante' ? 'recul insuffisant' : '—') },
    { titre: '100 % le', val: r => r.ram_100 || (r.tendance === 'insuffisante' ? 'recul insuffisant' : '—') },
    { titre: 'vCPU / thread', num: true, dec: 2, val: r => r.vcpu_ratio ?? null },
    { titre: '4 vCPU / thread le', val: r => r.vcpu_max || '—' },
  ],
  dessiner: dessinProjection,
});
