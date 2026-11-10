// --------------------------------------------------------- recolte PVE
// Les jetons PVE se deposent ici, hyperviseur par hyperviseur. Le service
// les scelle avec la clef publique de depot des reception : il ne peut plus
// les relire. La collecte suivante les ouvre, les teste sur l'hyperviseur,
// et les range chiffres ou les refuse avec leur motif — que cette vue
// affiche au passage suivant.
const droits = { admin: false };
const recolte = { data: null };

async function chargerRecolte() {
  try {
    const r = await fetch('/api/recolte', { headers: { Accept: 'application/json' } });
    if (!r.ok) throw new Error('http ' + r.status);
    recolte.data = await r.json();
  } catch (e) {
    recolte.data = null;
  }
  if (etat.selection) {
    const n = parId.get(etat.selection);
    if (n) ficheCommerce(n);
  }
}

function hoteDe(n) {
  const srv = n.kind === 'srv' ? n : (n.kind === 'node' ? parId.get(n.parent) : null);
  return srv && recolte.data
    ? recolte.data.hotes.find(h => h.ovh_name === srv.ovh) || null : null;
}

function etatRecolte(h) {
  if (h.ignore) return { texte: 'ignoré (skip)', classe: '' };
  if (!h.sonde) return { texte: 'non sondé', classe: '' };
  if (!h.joignable) return { texte: 'injoignable', classe: 'ko' };
  return { texte: 'joignable', classe: 'ok' };
}

function sourceJeton(h) {
  const d = h.depot;
  if (d && d.etat === 'en_attente') {
    return { texte: 'déposé le ' + d.depose_le + ' par ' + d.depose_par
                    + ', testé à la prochaine collecte', classe: 'attente' };
  }
  if (h.jeton_infra_yml) return { texte: 'nommé dans infra.yml', classe: '' };
  if (d && d.etat === 'accepte') {
    return { texte: 'déposé par ' + d.depose_par + ', accepté le ' + d.traite_le,
             classe: 'ok' };
  }
  if (d && d.etat === 'refuse') {
    return { texte: 'dépôt refusé le ' + d.traite_le + ' : ' + (d.motif || '?'),
             classe: 'ko' };
  }
  return { texte: 'jeton par défaut (default_token)', classe: '' };
}

function ficheRecolte(n) {
  if (!droits.admin || !recolte.data) return;
  const h = hoteDe(n);
  if (!h) return;
  const d = el('div', { className: 'vente' }, el('h4', { textContent: 'Récolte PVE' }));
  const ligne = grille(d);
  const e = etatRecolte(h), j = sourceJeton(h);
  ligne('État', e.texte + (h.cluster ? ' · cluster ' + h.cluster : ''));
  ligne('Jeton', j.texte);
  if (h.erreur) d.appendChild(el('p', { className: 'lecture alerte', textContent: '⚠ ' + h.erreur }));
  if (recolte.data.pret) {
    d.appendChild(el('div', { className: 'actions' },
      el('button', { textContent: 'Déposer un jeton…', onclick: () => formDepot([h.hote]) })));
  }
  document.getElementById('fiche-vente').appendChild(d);
}

const trierRecolte = hotes => [...hotes].sort((a, b) =>
  (a.joignable - b.joignable) || a.hote.localeCompare(b.hote, 'fr', { numeric: true }));

async function afficherRecolte() {
  const corps = ouvrirTiroir('Récolte PVE', 'jetons par hyperviseur');
  await chargerRecolte();
  const d = recolte.data;
  if (!d) {
    corps.appendChild(el('p', { className: 'msg ko', textContent: 'état de la récolte illisible' }));
    return;
  }
  const actifs = d.hotes.filter(h => !h.ignore);
  const ko = actifs.filter(h => !h.joignable).length;
  const attente = actifs.filter(h => h.depot && h.depot.etat === 'en_attente').length;
  corps.appendChild(el('p', { className: 'lecture', textContent:
    actifs.length + ' hyperviseur(s) : ' + (actifs.length - ko) + ' joignable(s), '
    + ko + ' injoignable(s)' + (attente ? ', ' + attente + ' dépôt(s) en attente' : '')
    + '. Photo de la collecte du ' + new Date(d.genere_le * 1000).toLocaleString('fr-FR') + '.' }));
  if (!d.pret) {
    corps.appendChild(el('p', { className: 'msg ko', textContent:
      'Boîte de dépôt pas encore initialisée : sur la VM, inventory_cli.py --init-depot.' }));
  } else if (!d.jetons_deposes) {
    corps.appendChild(el('p', { className: 'msg ko', textContent:
      'pve.jetons_deposes est désactivé dans infra.yml : un jeton accepté ne servira pas.' }));
  }
  const ul = el('ul', { className: 'liste-recolte' });
  for (const h of trierRecolte(actifs)) {
    const e = etatRecolte(h), j = sourceJeton(h);
    const li = el('li', null,
      el('span', { className: 'nom', textContent: h.hote,
                   title: [h.hote, h.cluster && 'cluster ' + h.cluster, h.erreur, j.texte]
                     .filter(Boolean).join('\n') }),
      el('span', { className: 'etat-recolte ' + e.classe, textContent: e.texte }),
      j.classe ? el('span', { className: 'etat-recolte ' + j.classe, textContent:
        { attente: 'en attente', ok: 'déposé', ko: 'refusé' }[j.classe] }) : null);
    if (d.pret) li.appendChild(el('button', { textContent: 'Déposer',
                                              onclick: () => formDepot([h.hote]) }));
    ul.appendChild(li);
  }
  corps.appendChild(ul);
  if (d.pret) {
    corps.appendChild(el('div', { className: 'actions' },
      el('button', { textContent: 'Déposer pour plusieurs…', onclick: () => formDepot([]) })));
  }
}

const COMMANDES_PVE = [
  'pveum user add inventory@pve --comment "inventaire, lecture seule"',
  'pveum aclmod / --user inventory@pve --role PVEAuditor',
  'pveum user token add inventory@pve readonly --privsep 0',
].join('\n');

function formDepot(choisis) {
  const d = recolte.data;
  const corps = ouvrirTiroir('Déposer un jeton PVE',
    choisis.length === 1 ? choisis[0] : 'plusieurs hyperviseurs');
  corps.appendChild(el('p', { className: 'lecture', textContent:
    'Scellé dès réception : ce service ne peut plus le relire. La prochaine '
    + 'collecte le teste sur chaque hyperviseur coché, puis le range chiffré '
    + 'ou le refuse avec son motif.' }));
  corps.appendChild(el('details', null,
    el('summary', { textContent: 'Créer le compte de récolte sur l’hyperviseur' }),
    el('pre', { textContent: COMMANDES_PVE }),
    el('button', { type: 'button', textContent: 'Copier les commandes',
      onclick: () => navigator.clipboard.writeText(COMMANDES_PVE)
        .then(() => flash('Commandes copiées.'), () => flash('Copie refusée par le navigateur.')) }),
    el('p', { className: 'lecture', textContent:
      'Sur un seul nœud par cluster : compte et jeton valent pour tout le cluster. '
      + 'Lecture seule (PVEAuditor). pveum n’affiche la valeur qu’une fois.' })));

  const f = el('form', { autocomplete: 'off', className: 'saisie' });
  const ident = champ(f, 'Identifiant du jeton (full-tokenid)', { type: 'text',
    value: 'inventory@pve!readonly', maxLength: 160, required: true, spellcheck: false });
  const secret = champ(f, 'Valeur du jeton (value)', { type: 'password',
    autocomplete: 'off', required: true, maxLength: 36, spellcheck: false,
    placeholder: 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx' });

  // les noeuds d'un meme cluster partagent compte et jeton : coches d'office
  const clusters = new Set(d.hotes.filter(h => choisis.includes(h.hote) && h.cluster)
                                  .map(h => h.cluster));
  const coches = new Set(choisis);
  d.hotes.forEach(h => { if (h.cluster && clusters.has(h.cluster)) coches.add(h.hote); });
  const liste = el('div', { className: 'hotes-depot' });
  const cases = [];
  for (const h of trierRecolte(d.hotes.filter(x => !x.ignore))) {
    const c = el('input', { type: 'checkbox', value: h.hote, checked: coches.has(h.hote) });
    cases.push(c);
    liste.appendChild(el('label', { className: 'case' }, c,
      h.hote + (h.cluster ? ' · ' + h.cluster : '') + (h.joignable ? '' : ' · injoignable')));
  }
  f.append(el('label', { textContent: 'Hyperviseurs' }), liste);
  f.appendChild(el('div', { className: 'actions' },
    el('button', { type: 'submit', textContent: 'Sceller et déposer' }),
    el('button', { type: 'button', textContent: 'Annuler', onclick: fermerTiroir })));
  f.onsubmit = ev => {
    ev.preventDefault();
    const hotes = cases.filter(c => c.checked).map(c => c.value);
    if (!hotes.length) { message(f, 'coche au moins un hyperviseur', false); return; }
    const jeton = ident.value.trim() + '=' + secret.value.trim();
    soumettre(f, async () => {
      await poster('/api/recolte/jeton', { hotes, jeton });
      secret.value = '';
      await chargerRecolte();
    }, 'Jeton scellé pour ' + hotes.length + ' hyperviseur(s) : testé à la prochaine collecte.');
  };
  corps.appendChild(f);
  secret.focus();
}

document.getElementById('btn-recolte').onclick = afficherRecolte;

