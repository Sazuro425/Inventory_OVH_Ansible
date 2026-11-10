// -------------------------------------------------------- tableau de bord
// Le mode Graphiques est une liste de panneaux, propre a chaque vue : les
// quatre figures d'origine (panneaux « figure »), et des panneaux libres —
// barres, chiffre cle, tableau — qui prennent une mesure a un niveau,
// regroupee ou non. La vue d'ensemble garde les quatre figures.
//
// Comme ailleurs, rien n'est recalcule a partir d'autre chose que l'arbre
// de l'API. Un ratio (marge, RAM allouee %, vCPU par thread) s'agrege
// depuis ses termes — somme du ROI sur somme du CA, RAM allouee sur RAM
// physique — et jamais en moyennant des pourcentages.

const PANNEAUX_DEFAUT = [
  { type: 'figure', figure: 'roi', largeur: 1 },
  { type: 'figure', figure: 'capa', largeur: 1 },
  { type: 'figure', figure: 'clients', largeur: 1 },
  { type: 'figure', figure: 'ram', largeur: 2 },
];
const NIVEAUX_PANNEAU = ['cluster', 'serveur', 'vm', 'element'];
const TYPES_PANNEAU = [['barres', 'Barres'], ['chiffre', 'Chiffre clé'],
                       ['tableau', 'Tableau'], ['figure', 'Figure prédéfinie']];
let edition = false;

const panneauxCourants = () => vueRent.panneaux || PANNEAUX_DEFAUT;

const sommeDe = f => l => l.reduce((t, r) => t + Number(f(r) || 0), 0);
const capaDe = (l, cote, champ) => l.reduce((t, r) =>
  t + Number(((r.c.capacite || {})[cote] || {})[champ] || 0), 0);
const avecCapa = l => l.some(r => r.c.capacite);

// niv : les niveaux ou la mesure existe (tous si absent). ratio : non
// sommable, son total se recalcule depuis ses termes.
const MESURES = [
  { id: 'pv', titre: 'CA / mois', euro: true, agreger: sommeDe(r => r.c.pv) },
  { id: 'pa', titre: 'PA / mois', euro: true, agreger: sommeDe(r => r.c.pa) },
  { id: 'roi', titre: 'ROI / mois', euro: true, signe: true,
    agreger: sommeDe(r => r.c.roi) },
  { id: 'marge', titre: 'Marge', unite: ' %', dec: 1, ratio: true,
    agreger: l => { const pv = sommeDe(r => r.c.pv)(l);
                    return pv ? 100 * sommeDe(r => r.c.roi)(l) / pv : null; } },
  { id: 'pa_auto', titre: 'Coût OVH / mois', euro: true, niv: ['serveur'],
    agreger: sommeDe(r => r.c.pa_auto) },
  { id: 'nombre', titre: 'Nombre de cartes', dec: 0, agreger: l => l.length },
  { id: 'nb_vm', titre: 'VM', dec: 0, niv: ['cluster', 'serveur'],
    agreger: sommeDe(r => r.nbVm) },
  { id: 'vcpu', titre: 'vCPU alloués', dec: 0, niv: ['vm'],
    agreger: sommeDe(r => r.c.infos.cpus) },
  { id: 'ram', titre: 'RAM allouée (Go)', dec: 0, niv: ['vm'],
    agreger: sommeDe(r => (r.c.infos.ram_mb || 0) / 1024) },
  { id: 'ram_pct', titre: 'RAM allouée', unite: ' %', dec: 1, ratio: true,
    niv: ['cluster', 'serveur'],
    agreger: l => { const p = capaDe(l, 'physique', 'ram_mb');
                    return p ? 100 * capaDe(l, 'alloue', 'ram_mb') / p : null; } },
  { id: 'ram_libre', titre: 'RAM libre (Go)', dec: 0, niv: ['cluster', 'serveur'],
    agreger: l => avecCapa(l)
      ? sommeDe(r => ((r.c.capacite || {}).ram_libre_mb || 0) / 1024)(l) : null },
  { id: 'vcpu_ratio', titre: 'vCPU par thread', dec: 2, ratio: true,
    niv: ['cluster', 'serveur'],
    agreger: l => { const p = capaDe(l, 'physique', 'vcpu');
                    return p ? capaDe(l, 'alloue', 'vcpu') / p : null; } },
];

// cles(r) : les groupes d'une carte. Un client, un datacenter peuvent etre
// plusieurs : la carte compte alors dans chacun, et le panneau le dit.
const GROUPES = [
  { id: '', titre: 'Aucun : une ligne par carte' },
  { id: 'cluster', titre: 'Cluster', niv: ['serveur', 'vm', 'element'],
    cles: r => [r.groupe] },
  { id: 'serveur', titre: 'Serveur', niv: ['vm', 'element'],
    cles: r => (r.serveur ? [r.serveur] : []) },
  { id: 'datacenter', titre: 'Datacenter', cles: r => [...r.dcs] },
  { id: 'client', titre: 'Client', niv: ['vm', 'element'], cles: r => [...r.clients] },
  { id: 'role', titre: 'Rôle', cles: r => [r.c.infos.role_libelle || '(sans rôle)'] },
];

const dispoAu = (x, niveau) => !x.niv || x.niv.includes(niveau);
const mesure = id => MESURES.find(m => m.id === id);
const groupe = id => GROUPES.find(g => g.id === (id || ''));
const figure = cle => FIGURES.find(f => f.cle === cle);

function fmtMesure(m, v, court) {
  if (v === null || v === undefined) return '—';
  if (m.euro) {
    const t = court ? euroCourt(v) : euro(v);
    return m.signe && v > 0 ? '+' + t : t;
  }
  return Number(v).toLocaleString('fr-FR', { maximumFractionDigits: m.dec ?? 2 })
    + (m.unite || '');
}

// La forme exacte qu'ecrit le serveur (db_vues.verifier_panneau) : la
// comparer a la vue enregistree ne donne pas de faux « modifiee ».
function normPanneau(p) {
  const o = { type: p.type, largeur: Number(p.largeur) === 2 ? 2 : 1 };
  const titre = String(p.titre || '').trim().slice(0, 80);
  if (titre) o.titre = titre;
  if (p.type === 'figure') { o.figure = p.figure; return o; }
  o.niveau = p.niveau;
  o.mesure = p.mesure;
  if (p.groupe) o.groupe = p.groupe;
  if (p.type !== 'chiffre') o.top = Math.max(1, Math.min(100, parseInt(p.top, 10) || 10));
  return o;
}

function titrePanneau(p) {
  if (p.titre) return p.titre;
  if (p.type === 'figure') return (figure(p.figure) || {}).titre || 'Figure';
  const m = mesure(p.mesure), g = groupe(p.groupe);
  if (!m) return 'Panneau';
  const niveau = (LIBELLE[p.niveau] || '').toLowerCase();
  // un chiffre cle est un total : il ne se lit pas « par » quelque chose
  if (p.type === 'chiffre') {
    return m.titre + (m.ratio ? ', ensemble des ' : ', total des ') + niveau + 's';
  }
  return m.titre + ' par ' + (g && g.id ? g.titre.toLowerCase() : niveau);
}

// Les rangs d'un panneau libre, filtres de la vue appliques.
function donneesPanneau(p) {
  const m = mesure(p.mesure), g = groupe(p.groupe);
  if (!m || !g || !dispoAu(m, p.niveau) || !dispoAu(g, p.niveau)) {
    return { erreur: 'Ce panneau combine une mesure ou un regroupement indisponible '
                     + 'à ce niveau : modifie-le.' };
  }
  const lignes = lignesFiltrees(p.niveau);
  let rangs, multiple = false;
  if (!g.id) {
    rangs = lignes.map(r => ({ nom: r.c.nom, v: m.agreger([r]), n: 1, r }));
  } else {
    const par = new Map();
    for (const r of lignes) {
      let cles = g.cles(r).filter(Boolean);
      if (!cles.length) cles = ['(aucun)'];
      if (cles.length > 1) multiple = true;
      for (const k of cles) {
        if (!par.has(k)) par.set(k, []);
        par.get(k).push(r);
      }
    }
    rangs = [...par].map(([nom, l]) => ({ nom, v: m.agreger(l), n: l.length }));
  }
  rangs = rangs.filter(x => x.v !== null && x.v !== undefined && !Number.isNaN(x.v))
    .sort((a, b) => b.v - a.v || a.nom.localeCompare(b.nom, 'fr', { numeric: true }));
  return { m, g, lignes, rangs, multiple, total: m.agreger(lignes) };
}

function colonnesPanneau(p, d) {
  const cols = [
    { titre: d.g.id ? d.g.titre : LIBELLE[p.niveau], val: r => r.nom },
    { titre: d.m.titre + (d.m.unite ? ' %' : ''), num: true, euro: d.m.euro,
      signe: d.m.signe, dec: d.m.dec,
      somme: !d.m.ratio && !d.multiple, val: r => r.v },
  ];
  if (d.g.id) {
    cols.push({ titre: 'Cartes', num: true, dec: 0, somme: !d.multiple, val: r => r.n });
  }
  return cols;
}

function dessinBarresPanneau(zone, p, d) {
  const rangs = d.rangs.slice(0, p.top || 10);
  const min = Math.min(0, ...rangs.map(r => r.v));
  const max = Math.max(0, ...rangs.map(r => r.v));
  const cadre = cadreBarres(zone, rangs, {
    domaine: [min, max], format: v => fmtMesure(d.m, v, true), margeLabel: 60,
    resume: titrePanneau(p) + ' : ' + rangs.length + ' ligne(s)' });
  rangs.forEach((r, i) => {
    const infos = [[d.m.titre, fmtMesure(d.m, r.v)]];
    if (d.g.id) infos.push(['Cartes', String(r.n)]);
    const { g, y } = rangee(cadre, i, r.nom, () => [r.nom, infos],
      r.r ? () => basculerMode('arbre', r.r.chemin) : null);
    const xa = cadre.x(0), xb = cadre.x(r.v);
    g.appendChild(svgEl('path', { d: barreH(xa, xb, y, VIZ.barre),
      class: r.v < 0 ? 'viz-perte' : 'viz-ca' }));
    g.appendChild(svgEl('text', { x: xb + (r.v < 0 ? -6 : 6), y: y + VIZ.barre / 2 + 4,
      class: 'viz-valeur', 'text-anchor': r.v < 0 ? 'end' : 'start' },
      fmtMesure(d.m, r.v, true)));
  });
}

function dessinChiffre(zone, p, d) {
  const b = el('b', { textContent: fmtMesure(d.m, d.total) });
  if (d.m.signe) b.className = 'marge ' + classeMarge(d.total);
  zone.appendChild(el('div', { className: 'chiffre-cle' }, b,
    el('span', { textContent: 'sur ' + d.lignes.length + ' '
      + (LIBELLE[p.niveau] || '').toLowerCase() + (d.lignes.length > 1 ? 's' : '')
      + (filtreRentActif() ? ', filtres appliqués' : '') })));
}

// --- un panneau : son cadre, ses outils, son contenu
function outil(texte, titre, action, classe) {
  return el('button', { type: 'button', textContent: texte, title: titre,
                        className: classe || '', onclick: action });
}

function panneau(mur, p, i, n) {
  const fig = el('figure', { className: 'viz' + (p.largeur === 2 ? ' large' : '') });
  const cap = el('figcaption');
  const outils = el('div', { className: 'fig-outils' });
  const zone = el('div', { className: 'viz-zone' });
  let sous = '';
  let dessin = null;

  if (p.type === 'figure') {
    const f = figure(p.figure);
    if (!f) return null;
    sous = f.sous;
    const enTable = vueRent.figures[f.id] === 'tableau';
    const modes = el('div', { className: 'segment fig-modes', role: 'group' });
    for (const [vue, libelle] of [['graphique', 'Graphique'], ['tableau', 'Tableau']]) {
      const b = outil(libelle, libelle === 'Tableau' ? 'Afficher les valeurs en tableau'
                                                     : 'Afficher en graphique', () => {
        vueRent.figures[f.id] = vue;
        sauverRent();
        cacherBulle();
        dessinerGraphes();
      });
      b.classList.toggle('actif', (vue === 'tableau') === enTable);
      modes.appendChild(b);
    }
    outils.append(modes, outil('CSV', 'Exporter cette figure en CSV', () => {
      const rangs = f.rangs();
      telechargerCsv('rentabilite-' + f.fichier, f.colonnes, rangs);
      flash(rangs.length + ' ligne(s) exportée(s).');
    }));
    dessin = () => {
      const rangs = f.rangs();
      if (!rangs.length) return graphVide(zone);
      if (enTable) tableFigure(zone, f, rangs);
      else f.dessiner(zone, rangs);
    };
  } else {
    const d = donneesPanneau(p);
    if (d.erreur) {
      dessin = () => zone.appendChild(el('p', { className: 'lecture alerte', textContent: d.erreur }));
    } else {
      sous = (p.type === 'chiffre' ? (d.m.ratio ? 'Recalculé sur l’ensemble' : 'Total')
                                   : 'Les ' + (p.top || 10) + ' premiers')
        + ', niveau ' + (LIBELLE[p.niveau] || '').toLowerCase()
        + (d.multiple ? ' · une carte à plusieurs ' + d.g.titre.toLowerCase()
                        + 's compte dans chacun' : '');
      if (p.type !== 'chiffre') {
        outils.appendChild(outil('CSV', 'Exporter ce panneau en CSV', () => {
          const rangs = d.rangs.slice(0, p.top || 10);
          telechargerCsv('panneau-' + titrePanneau(p).toLowerCase()
            .normalize('NFD').replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, ''),
            colonnesPanneau(p, d), rangs);
          flash(rangs.length + ' ligne(s) exportée(s).');
        }));
      }
      dessin = () => {
        if (p.type === 'chiffre') return dessinChiffre(zone, p, d);
        if (!d.rangs.length) return graphVide(zone);
        if (p.type === 'tableau') {
          tableFigure(zone, { colonnes: colonnesPanneau(p, d) },
                      d.rangs.slice(0, p.top || 10));
        } else {
          dessinBarresPanneau(zone, p, d);
        }
      };
    }
  }

  if (edition) {
    const ed = el('div', { className: 'pan-edition', role: 'group',
                           ariaLabel: 'Disposer ce panneau' },
      outil('←', 'Avancer ce panneau', () => modifierPanneaux(l => {
        if (i > 0) [l[i - 1], l[i]] = [l[i], l[i - 1]]; })),
      outil('→', 'Reculer ce panneau', () => modifierPanneaux(l => {
        if (i < n - 1) [l[i + 1], l[i]] = [l[i], l[i + 1]]; })),
      outil(p.largeur === 2 ? '½' : '↔', p.largeur === 2 ? 'Demi-largeur' : 'Pleine largeur',
            () => modifierPanneaux(l => { l[i].largeur = l[i].largeur === 2 ? 1 : 2; })),
      outil('Modifier', 'Régler ce panneau', () => formPanneau(i)),
      outil('Retirer', 'Retirer ce panneau', () => modifierPanneaux(l => { l.splice(i, 1); }),
            'danger'));
    outils.prepend(ed);
  }
  cap.append(outils, el('b', { textContent: titrePanneau(p) }),
             el('small', { textContent: sous }));
  fig.append(cap, zone);
  mur.appendChild(fig);
  return dessin;
}

function dessinerGraphes() {
  const mur = document.getElementById('viz-mur');
  mur.replaceChildren();
  mur.classList.toggle('edition', edition);
  const liste = panneauxCourants();
  // Tous les cadres d'abord, les dessins ensuite : un graphique prend la
  // largeur de son cadre, qui n'est connue qu'une fois la grille complete.
  // Dessine seul dans la grille, le premier panneau prendrait toute la
  // largeur, puis deborderait de sa colonne.
  const dessins = liste.map((p, i) => panneau(mur, p, i, liste.length));
  if (edition) {
    mur.appendChild(el('button', { type: 'button', className: 'viz-ajout',
      textContent: '+ Ajouter un panneau', onclick: () => formPanneau(null) }));
  } else if (!liste.length) {
    mur.appendChild(el('p', { className: 'lecture', textContent:
      'Aucun panneau dans cette vue. « Personnaliser » pour en ajouter.' }));
  }
  dessins.forEach(d => d && d());
  const b = document.getElementById('rent-personnaliser');
  b.textContent = edition ? 'Terminer' : 'Personnaliser';
  b.classList.toggle('actif', edition);
  document.getElementById('rent-defaut-panneaux').hidden = !edition || !vueRent.panneaux;
}

// Toute modification passe ici : la liste revient a null (les panneaux par
// defaut) si elle leur est identique, et la vue se dit « modifiee ».
function modifierPanneaux(fn) {
  const l = panneauxCourants().map(p => ({ ...p }));
  fn(l);
  const norm = l.map(normPanneau);
  vueRent.panneaux = JSON.stringify(norm) === JSON.stringify(PANNEAUX_DEFAUT.map(normPanneau))
    ? null : norm;
  cacherBulle();
  dessinerGraphes();
  majModifiee();
}

// --- le formulaire d'un panneau, nouveau (index null) ou existant
function formPanneau(index) {
  const existant = index === null ? null : panneauxCourants()[index];
  const corps = ouvrirTiroir(existant ? 'Modifier le panneau' : 'Ajouter un panneau',
                             'tableau de bord de la vue');
  const f = el('form', { autocomplete: 'off', className: 'saisie' });
  const choix = (etiquette, options, valeur) => {
    const s = champ(f, etiquette, { tag: 'select' });
    s.replaceChildren(...options.map(([v, t]) =>
      el('option', { value: v, textContent: t, selected: v === valeur })));
    return s;
  };
  const p0 = existant || { type: 'barres', niveau: 'serveur', mesure: 'pv', top: 10, largeur: 1 };
  const type = choix('Type', TYPES_PANNEAU, p0.type);
  const fig = choix('Figure', FIGURES.map(x => [x.cle, x.titre]), p0.figure || 'roi');
  const titre = champ(f, 'Titre (facultatif)', { type: 'text', maxLength: 80,
    value: p0.titre || '', placeholder: 'titre automatique' });
  const niveau = choix('Niveau', NIVEAUX_PANNEAU.map(n => [n, LIBELLE[n]]), p0.niveau || 'serveur');
  const mes = champ(f, 'Mesure', { tag: 'select' });
  const grp = champ(f, 'Regroupement', { tag: 'select' });
  const top = champ(f, 'Lignes au plus', { type: 'number', min: 1, max: 100,
                                           value: p0.top || 10 });
  const larg = choix('Largeur', [['1', 'Demi-largeur'], ['2', 'Pleine largeur']],
                     String(p0.largeur || 1));
  const aide = el('p', { className: 'lecture' });
  f.appendChild(aide);
  const etiquette = x => x.previousElementSibling;   // le label qui le precede
  let premier = true;

  const maj = () => {
    const n = niveau.value, t = type.value, estFig = t === 'figure';
    // au premier passage, les listes sont vides : on part du panneau
    const m0 = premier ? p0.mesure : mes.value;
    const g0 = premier ? (p0.groupe || '') : grp.value;
    premier = false;
    mes.replaceChildren(...MESURES.filter(m => dispoAu(m, n)).map(m =>
      el('option', { value: m.id, textContent: m.titre, selected: m.id === m0 })));
    grp.replaceChildren(...GROUPES.filter(g => dispoAu(g, n)).map(g =>
      el('option', { value: g.id, textContent: g.titre, selected: g.id === g0 })));
    fig.hidden = !estFig; etiquette(fig).hidden = !estFig;
    for (const x of [niveau, mes, grp]) { x.hidden = estFig; etiquette(x).hidden = estFig; }
    top.hidden = estFig || t === 'chiffre'; etiquette(top).hidden = top.hidden;
    const m = mesure(mes.value);
    aide.textContent = estFig ? (figure(fig.value) || {}).sous || ''
      : m && m.ratio ? m.titre + ' est un ratio : un groupe le recalcule depuis ses '
                       + 'termes, sans moyenner des pourcentages.' : '';
  };
  [type, niveau, mes, fig].forEach(x => x.addEventListener('change', maj));
  maj();

  f.appendChild(el('div', { className: 'actions' },
    el('button', { type: 'submit', textContent: existant ? 'Appliquer' : 'Ajouter' }),
    el('button', { type: 'button', textContent: 'Annuler', onclick: fermerTiroir })));
  f.onsubmit = ev => {
    ev.preventDefault();
    const p = normPanneau({ type: type.value, figure: fig.value, titre: titre.value,
      niveau: niveau.value, mesure: mes.value, groupe: grp.value, top: top.value,
      largeur: larg.value });
    modifierPanneaux(l => { if (existant) l[index] = p; else l.push(p); });
    fermerTiroir();
    flash(existant ? 'Panneau modifié.' : 'Panneau ajouté. Enregistre la vue pour le garder.');
  };
  corps.appendChild(f);
  type.focus();
}

document.getElementById('rent-personnaliser').onclick = () => {
  edition = !edition;
  dessinerGraphes();
};
document.getElementById('rent-defaut-panneaux').onclick = () => {
  vueRent.panneaux = null;
  dessinerGraphes();
  majModifiee();
};
