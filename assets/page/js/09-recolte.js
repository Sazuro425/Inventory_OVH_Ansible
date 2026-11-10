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

// Jours restants avant l'echeance declaree d'un depot, ou null si inconnue.
function joursRestants(h) {
  const d = h.depot;
  if (!d || !d.expire_le || d.etat === 'refuse') return null;
  const fin = new Date(d.expire_le + 'T23:59:59');
  return Math.floor((fin - Date.now()) / 86400000);
}

const SOUS_PEU = 30;

function sourceJeton(h) {
  const d = h.depot;
  const j = joursRestants(h);
  const fin = d && d.expire_le ? ' · expire le ' + d.expire_le
    + (j !== null && j >= 0 ? ' (dans ' + j + ' j)' : '') : '';
  // une echeance passee ou proche l'emporte : c'est elle qu'il faut voir
  if (j !== null && j < 0) {
    return { texte: 'jeton expiré le ' + d.expire_le + ' : à renouveler',
             classe: 'ko', badge: 'expiré' };
  }
  if (d && d.etat === 'en_attente') {
    return { texte: 'déposé le ' + d.depose_le + ' par ' + d.depose_par
                    + ', testé à la prochaine collecte' + fin,
             classe: 'attente', badge: 'en attente' };
  }
  if (h.jeton_infra_yml) return { texte: 'nommé dans infra.yml', classe: '' };
  if (d && d.etat === 'accepte') {
    const proche = j !== null && j <= SOUS_PEU;
    return { texte: 'déposé par ' + d.depose_par + ', accepté le ' + d.traite_le + fin,
             classe: proche ? 'attente' : 'ok',
             badge: proche ? 'expire sous ' + Math.max(j, 0) + ' j' : 'déposé' };
  }
  if (d && d.etat === 'refuse') {
    return { texte: 'dépôt refusé le ' + d.traite_le + ' : ' + (d.motif || '?'),
             classe: 'ko', badge: 'refusé' };
  }
  return { texte: 'jeton par défaut (default_token), échéance inconnue', classe: '' };
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

// injoignables d'abord, puis l'echeance la plus proche, puis le nom
const trierRecolte = hotes => [...hotes].sort((a, b) =>
  (a.joignable - b.joignable)
  || ((joursRestants(a) ?? Infinity) - (joursRestants(b) ?? Infinity))
  || a.hote.localeCompare(b.hote, 'fr', { numeric: true }));

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
  const echeances = actifs.filter(h => { const j = joursRestants(h); return j !== null && j <= SOUS_PEU; }).length;
  corps.appendChild(el('p', { className: 'lecture', textContent:
    actifs.length + ' hyperviseur(s) : ' + (actifs.length - ko) + ' joignable(s), '
    + ko + ' injoignable(s)' + (attente ? ', ' + attente + ' dépôt(s) en attente' : '')
    + (echeances ? ', ' + echeances + ' jeton(s) expiré(s) ou à renouveler sous '
                   + SOUS_PEU + ' jours' : '')
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
      j.badge ? el('span', { className: 'etat-recolte ' + j.classe, textContent: j.badge })
              : null);
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

// Les commandes de creation du compte de recolte, avec l'echeance choisie :
// la date que PVE appliquera est celle que la page gardera.
const commandesPve = echeance => [
  'pveum user add inventory@pve --comment "inventaire, lecture seule"',
  'pveum aclmod / --user inventory@pve --role PVEAuditor',
  'pveum user token add inventory@pve readonly --privsep 0'
    + (echeance ? " --expire $(date -d '" + echeance + " 23:59:59' +%s)" : ''),
].join('\n');

function dansUnAn() {
  const d = new Date();
  d.setFullYear(d.getFullYear() + 1);
  return d.toISOString().slice(0, 10);
}

function formDepot(choisis) {
  const d = recolte.data;
  const corps = ouvrirTiroir('Déposer un jeton PVE',
    choisis.length === 1 ? choisis[0] : 'plusieurs hyperviseurs');
  corps.appendChild(el('p', { className: 'lecture', textContent:
    'Scellé dès réception : ce service ne peut plus le relire. La prochaine '
    + 'collecte le teste sur chaque hyperviseur coché, puis le range chiffré '
    + 'ou le refuse avec son motif.' }));
  const pre = el('pre');
  corps.appendChild(el('details', null,
    el('summary', { textContent: 'Créer le compte de récolte sur l’hyperviseur' }),
    pre,
    el('button', { type: 'button', textContent: 'Copier les commandes',
      onclick: () => navigator.clipboard.writeText(pre.textContent)
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
  const echeance = champ(f, 'Échéance du jeton (pveum --expire)', { type: 'date',
    value: dansUnAn(), min: new Date(Date.now() + 86400000).toISOString().slice(0, 10) });
  const majCommandes = () => { pre.textContent = commandesPve(echeance.value); };
  echeance.addEventListener('input', majCommandes);
  majCommandes();

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
      await poster('/api/recolte/jeton', { hotes, jeton, expire_le: echeance.value });
      secret.value = '';
      await chargerRecolte();
    }, 'Jeton scellé pour ' + hotes.length + ' hyperviseur(s) : testé à la prochaine collecte.');
  };
  corps.appendChild(f);
  secret.focus();
}

document.getElementById('btn-recolte').onclick = afficherRecolte;

