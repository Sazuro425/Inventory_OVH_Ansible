const DONNEES = __DATA__;

// ---------------------------------------------------------------- constantes
const TAILLE = {
  dc:   { w: 150, h: 54 },
  srv:  { w: 236, h: 80 },
  node: { w: 220, h: 80 },
  vm:   { w: 210, h: 60 },
};
const ECART_X = 86, ECART_Y = 16, MARGE = 40;
const CLE = 'topologie:' + (DONNEES.cle || 'defaut');

const COULEUR = {
  running: 'var(--vert)', stopped: 'var(--gris)',
  unreachable: 'var(--rouge)', dc: 'var(--accent)', srv: 'var(--accent)',
};

// ------------------------------------------------------------------- etat
const etat = {
  plies: new Set(),        // noeuds dont les enfants sont masques
  decales: {},             // id -> {dx, dy} deplacement manuel
  filtres: {},             // famille -> Set de valeurs exclues
  recherche: '',
  vue: { x: 0, y: 0, k: 1 },
  selection: null,
};

try {
  const sauve = JSON.parse(localStorage.getItem(CLE) || '{}');
  if (sauve.plies) etat.plies = new Set(sauve.plies);
  if (sauve.decales) etat.decales = sauve.decales;
  if (sauve.filtres) for (const [k, v] of Object.entries(sauve.filtres))
    etat.filtres[k] = new Set(v);
  if (sauve.theme) document.documentElement.dataset.theme = sauve.theme;
  if (sauve.vue) etat.vue = sauve.vue;
} catch (err) { /* stockage indisponible : on part d'un etat neuf */ }

function sauver() {
  try {
    localStorage.setItem(CLE, JSON.stringify({
      plies: [...etat.plies],
      decales: etat.decales,
      filtres: Object.fromEntries(
        Object.entries(etat.filtres).map(([k, v]) => [k, [...v]])),
      theme: document.documentElement.dataset.theme,
      vue: etat.vue,
    }));
  } catch (err) { /* mode prive, quota : on continue sans persistance */ }
}

// ------------------------------------------------------------------- arbre
const parId = new Map(DONNEES.noeuds.map(n => [n.id, n]));
for (const n of DONNEES.noeuds) n.enfants = [];
const racines = [];
for (const n of DONNEES.noeuds) {
  const p = n.parent ? parId.get(n.parent) : null;
  if (p) p.enfants.push(n); else racines.push(n);
}

function passeFiltres(n) {
  for (const [famille, exclus] of Object.entries(etat.filtres)) {
    if (!exclus.size) continue;
    const v = n.meta && n.meta[famille];
    if (v !== undefined && exclus.has(String(v))) return false;
  }
  return true;
}

const cacheFiltre = new Map();
function visible(n) {
  if (etat.recherche && !correspondBranche(n)) return false;
  // Un noeud qui ne passe pas les filtres reste affiche si un descendant les
  // passe : sinon masquer un hyperviseur emporterait les VM qu'on cherche
  // justement a voir, et l'arbre perdrait son chemin.
  if (cacheFiltre.has(n.id)) return cacheFiltre.get(n.id);
  const ok = passeFiltres(n) || n.enfants.some(c => visible(c));
  cacheFiltre.set(n.id, ok);
  return ok;
}

function correspond(n) {
  const q = etat.recherche;
  if (!q) return true;
  return (n.recherche || '').includes(q);
}

const cacheBranche = new Map();
function correspondBranche(n) {
  if (cacheBranche.has(n.id)) return cacheBranche.get(n.id);
  let ok = correspond(n) || n.enfants.some(c => correspondBranche(c));
  if (!ok) {  // ou bien un ancetre correspond
    let p = n.parent ? parId.get(n.parent) : null;
    while (p && !ok) { ok = correspond(p); p = p.parent ? parId.get(p.parent) : null; }
  }
  cacheBranche.set(n.id, ok);
  return ok;
}

function enfantsVisibles(n) {
  if (etat.plies.has(n.id)) return [];
  return n.enfants.filter(visible);
}

// ------------------------------------------------------------------ layout
function hauteur(n) {
  const kids = enfantsVisibles(n);
  if (!kids.length) return TAILLE[n.kind].h;
  const somme = kids.reduce((s, c) => s + hauteur(c), 0);
  return somme + ECART_Y * (kids.length - 1);
}

function placer(n, x, y) {
  const t = TAILLE[n.kind];
  const total = hauteur(n);
  n.x = x; n.y = y + (total - t.h) / 2;
  const d = etat.decales[n.id];
  if (d) { n.x += d.dx; n.y += d.dy; }
  let curseur = y;
  for (const c of enfantsVisibles(n)) {
    placer(c, x + t.w + ECART_X, curseur);
    curseur += hauteur(c) + ECART_Y;
  }
  return total;
}

function disposer() {
  cacheBranche.clear();
  cacheFiltre.clear();
  let y = MARGE;
  const vues = racines.filter(visible);
  for (const r of vues) { placer(r, MARGE, y); y += hauteur(r) + 48; }
  return vues;
}

// ------------------------------------------------------------------- rendu
const elLiens = document.getElementById('liens');
const elNoeuds = document.getElementById('noeuds');
const elMonde = document.getElementById('monde');
const svg = document.getElementById('svg');

function svgEl(nom, attrs, texte) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', nom);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  if (texte !== undefined) el.textContent = texte;
  return el;
}

function dessiner() {
  const vues = disposer();
  elLiens.replaceChildren();
  elNoeuds.replaceChildren();

  const pile = [...vues];
  let compte = 0;
  while (pile.length) {
    const n = pile.pop();
    compte++;
    const t = TAILLE[n.kind];
    const kids = enfantsVisibles(n);

    for (const c of kids) {
      const x1 = n.x + t.w, y1 = n.y + t.h / 2;
      const x2 = c.x, y2 = c.y + TAILLE[c.kind].h / 2;
      const m = (x1 + x2) / 2;
      elLiens.appendChild(svgEl('path', {
        class: 'lien', d: `M${x1},${y1} C${m},${y1} ${m},${y2} ${x2},${y2}`,
      }));
      pile.push(c);
    }

    const g = svgEl('g', { class: 'noeud', 'data-id': n.id,
                           transform: `translate(${n.x},${n.y})` });
    g.appendChild(svgEl('rect', { class: 'fond', width: t.w, height: t.h, rx: 10 }));
    g.appendChild(svgEl('rect', {
      class: 'bande', width: 4, height: t.h, rx: 2,
      fill: COULEUR[n.etat] || COULEUR[n.kind] || 'var(--doux)' }));
    // le titre ne doit jamais passer sous le badge : on le tronque sur la
    // place reellement disponible (~6.4 px par caractere a 12.5 px)
    const placeBadge = n.badge ? n.badge.length * 5.6 + 12 : 0;
    const maxCar = Math.max(6, Math.floor((t.w - 26 - placeBadge) / 6.4));
    const titre = n.titre.length > maxCar
      ? n.titre.slice(0, maxCar - 1) + '…' : n.titre;
    const elTitre = svgEl('text', { class: 'titre', x: 14, y: 21 }, titre);
    if (titre !== n.titre) elTitre.appendChild(svgEl('title', {}, n.titre));
    g.appendChild(elTitre);
    (n.lignes || []).forEach((l, i) =>
      g.appendChild(svgEl('text', { class: 'detail', x: 14, y: 38 + i * 15 }, l)));
    if (n.badge)
      g.appendChild(svgEl('text', { class: 'badge', x: t.w - 12, y: 21 }, n.badge));

    if (n.enfants.length) {
      const plie = etat.plies.has(n.id);
      const bx = t.w - 13, by = t.h - 13;
      const btn = svgEl('g', { class: 'plier-grp' });
      btn.appendChild(svgEl('circle', { class: 'plier-fond', cx: bx, cy: by, r: 9 }));
      btn.appendChild(svgEl('text', { class: 'plier', x: bx, y: by + 4 },
                            plie ? '+' + n.enfants.length : '–'));
      btn.addEventListener('pointerdown', ev => {
        ev.stopPropagation();
        plie ? etat.plies.delete(n.id) : etat.plies.add(n.id);
        sauver(); dessiner();
      });
      g.appendChild(btn);
    }
    // conserve pour sa descendance, mais hors du filtre : on l'attenue pour
    // qu'il ne soit pas lu comme un resultat
    if (!passeFiltres(n)) g.classList.add('attenue');
    if (n.meta && n.meta.acces === 'manquant') {
      g.classList.add('sans-acces');
      g.appendChild(svgEl('text',
        { class: 'marque-acces', x: t.w - 12, y: t.h - 8 }, 'sans accès'));
    }
    if (etat.decales[n.id]) g.classList.add('deplace-main');
    if (etat.selection === n.id) g.classList.add('cible');
    elNoeuds.appendChild(g);
  }

  document.getElementById('vide').classList.toggle('visible', compte === 0);
  construireStats();
  appliquerVue();
}

// -------------------------------------------------------------- vue (pan/zoom)
function appliquerVue() {
  const { x, y, k } = etat.vue;
  elMonde.setAttribute('transform', `translate(${x},${y}) scale(${k})`);
}

function ajuster() {
  const boite = elNoeuds.getBBox();
  if (!boite.width) return;
  const r = svg.getBoundingClientRect();
  let k = Math.min((r.width - 60) / boite.width, (r.height - 60) / boite.height, 1.4);
  // en dessous de ce seuil le texte est illisible : on cadre en haut a gauche
  // plutot que de tout faire tenir a l'ecran
  const LISIBLE = 0.45;
  if (k < LISIBLE) {
    k = LISIBLE;
    etat.vue = { k, x: 30 - boite.x * k, y: 30 - boite.y * k };
  } else {
    etat.vue = {
      k,
      x: (r.width - boite.width * k) / 2 - boite.x * k,
      y: (r.height - boite.height * k) / 2 - boite.y * k,
    };
  }
  appliquerVue(); sauver();
}

const scene = document.getElementById('scene');
// La fiche et la boite du jeton sont posees sur la scene, mais ne sont pas
// la carte : un clic, une molette, une selection de texte y restent a elles.
// Sans ce garde, le deplacement de la carte capturait le pointeur, et ni la
// croix ni les boutons de la fiche ne recevaient leur clic.
const surFiche = ev => !!ev.target.closest('.fiche');

scene.addEventListener('wheel', ev => {
  if (surFiche(ev)) return;          // la fiche defile, la carte ne zoome pas
  ev.preventDefault();
  const r = svg.getBoundingClientRect();
  const sx = ev.clientX - r.left, sy = ev.clientY - r.top;
  const f = ev.deltaY < 0 ? 1.12 : 1 / 1.12;
  const k = Math.min(3, Math.max(0.15, etat.vue.k * f));
  // zoom centre sur le pointeur
  etat.vue.x = sx - (sx - etat.vue.x) * (k / etat.vue.k);
  etat.vue.y = sy - (sy - etat.vue.y) * (k / etat.vue.k);
  etat.vue.k = k;
  appliquerVue(); sauver();
}, { passive: false });

// --------------------------------------------------------- glisser (pan + noeud)
let glisse = null;

scene.addEventListener('pointerdown', ev => {
  if (surFiche(ev)) return;
  const g = ev.target.closest('.noeud');
  if (ev.target.closest('.plier-grp')) return;
  if (g) {
    const n = parId.get(g.dataset.id);
    glisse = { type: 'noeud', n, g, x0: ev.clientX, y0: ev.clientY,
               d0: { ...(etat.decales[n.id] || { dx: 0, dy: 0 }) }, bouge: false };
    g.classList.add('saisi');
  } else {
    glisse = { type: 'vue', x0: ev.clientX, y0: ev.clientY,
               v0: { ...etat.vue }, bouge: false };
    scene.classList.add('deplace');
  }
  scene.setPointerCapture(ev.pointerId);
});

scene.addEventListener('pointermove', ev => {
  if (!glisse) return;
  const dx = ev.clientX - glisse.x0, dy = ev.clientY - glisse.y0;
  if (Math.abs(dx) + Math.abs(dy) > 3) glisse.bouge = true;
  if (glisse.type === 'vue') {
    etat.vue.x = glisse.v0.x + dx; etat.vue.y = glisse.v0.y + dy;
    appliquerVue();
  } else {
    const k = etat.vue.k;
    etat.decales[glisse.n.id] = {
      dx: glisse.d0.dx + dx / k, dy: glisse.d0.dy + dy / k };
    const n = glisse.n;
    glisse.g.setAttribute('transform',
      `translate(${n.x - glisse.d0.dx + etat.decales[n.id].dx},` +
      `${n.y - glisse.d0.dy + etat.decales[n.id].dy})`);
  }
});

scene.addEventListener('pointerup', ev => {
  if (!glisse) return;
  scene.classList.remove('deplace');
  if (glisse.type === 'noeud') {
    glisse.g.classList.remove('saisi');
    if (glisse.bouge) { sauver(); dessiner(); }
    else { selectionner(glisse.n); }
  } else if (glisse.bouge) sauver();
  glisse = null;
});

// ------------------------------------------------------------------- fiche
const fiche = document.getElementById('fiche');
function selectionner(n) {
  etat.selection = n.id;
  document.getElementById('fiche-titre').textContent = n.titre;
  document.getElementById('fiche-type').textContent = n.libelle || n.kind;
  const dl = document.getElementById('fiche-corps');
  dl.replaceChildren();
  for (const [k, v] of Object.entries(n.fiche || {})) {
    if (v === null || v === '' || v === undefined) continue;
    dl.appendChild(Object.assign(document.createElement('dt'), { textContent: k }));
    dl.appendChild(Object.assign(document.createElement('dd'), { textContent: v }));
  }
  ficheCommerce(n);
  fiche.classList.add('visible');
  dessiner();
}
document.getElementById('fiche-fermer').onclick = () => {
  fiche.classList.remove('visible'); etat.selection = null; dessiner();
};

