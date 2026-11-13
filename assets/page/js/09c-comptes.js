// ------------------------------------------------------------------ comptes
// L'onglet Comptes : les comptes locaux, pour un admin. Le service genere
// les mots de passe ; un mot de passe rendu ne s'affiche qu'ici, une fois,
// et disparait des qu'on le masque ou qu'on quitte l'onglet.
const ROLES_COMPTE = [
  ['lecture', 'Lecture', 'voit tout, ne saisit rien'],
  ['commercial', 'Commercial', 'saisit ventes, enfants, corrections'],
  ['admin', 'Admin', 'jetons PVE, comptes, journal'],
];
const comptes = { liste: [], moi: null, mdp: null };

function etatCompte(c) {
  if (!c.actif) return 'désactivé';
  if (c.bloque_jusqu_a) return 'bloqué jusqu’à ' + c.bloque_jusqu_a.slice(11);
  if (c.doit_changer) return 'mot de passe à changer';
  return 'actif';
}

function messageComptes(texte, ok) {
  const p = document.getElementById('comptes-msg');
  if (!p) return;
  p.className = 'msg ' + (ok ? 'ok' : 'ko');
  p.textContent = texte;
  p.hidden = !texte;
}

function blocMotDePasse(m) {
  const code = el('code', { textContent: m.mot_de_passe });
  return el('div', { className: 'mdp-genere', role: 'status' },
    el('div', null, 'Mot de passe de ', el('b', { textContent: m.login }), ' : ', code),
    el('p', { className: 'lecture', textContent:
      'Affiché une seule fois. Transmets-le à la personne (de vive voix, ou par '
      + 'un gestionnaire de mots de passe) : elle devra le changer à sa première '
      + 'connexion. Jusque-là, son compte n’a accès à rien.' }),
    el('div', { className: 'actions' },
      el('button', { textContent: 'Copier', onclick: () => navigator.clipboard
        .writeText(m.mot_de_passe).then(() => flash('Mot de passe copié.'),
                                        () => flash('Copie refusée par le navigateur.')) }),
      el('button', { textContent: 'C’est noté, masquer', onclick: () => {
        comptes.mdp = null;
        dessinerComptes();
      } })));
}

async function actionCompte(url, charge, succes) {
  messageComptes('', true);
  try {
    const rep = await poster(url, charge);
    if (rep.mot_de_passe) comptes.mdp = rep;
    await dessinerComptes();
    if (succes) flash(succes);
  } catch (e) {
    messageComptes(e.message === 'Failed to fetch' ? 'le service n’a pas répondu'
                                                   : e.message, false);
  }
}

function casesRoles(coches) {
  return ROLES_COMPTE.map(([v, l, aide]) => {
    const c = el('input', { type: 'checkbox', value: v, checked: coches.includes(v) });
    return el('label', { className: 'case', title: aide }, c, l);
  });
}
const rolesCoches = zone => [...zone.querySelectorAll('input[type=checkbox]')]
  .filter(c => c.checked).map(c => c.value);

function formRoles(c) {
  const corps = ouvrirTiroir('Rôles', c.login);
  const f = el('form', { className: 'saisie' });
  const zone = el('div', { className: 'hotes-depot' }, ...casesRoles(c.roles));
  f.append(el('p', { className: 'lecture', textContent:
    'Commercial et Admin emportent la Lecture. Ses sessions ouvertes se ferment : '
    + 'les nouveaux droits valent dès sa prochaine connexion.' }), zone);
  f.appendChild(el('div', { className: 'actions' },
    el('button', { type: 'submit', textContent: 'Enregistrer' }),
    el('button', { type: 'button', textContent: 'Annuler', onclick: fermerTiroir })));
  f.onsubmit = ev => {
    ev.preventDefault();
    const roles = rolesCoches(zone);
    if (!roles.length) { message(f, 'coche au moins un rôle', false); return; }
    soumettre(f, async () => {
      await poster('/api/comptes/modifier', { login: c.login, roles });
      await dessinerComptes();
    }, 'Rôles de ' + c.login + ' enregistrés.');
  };
  corps.appendChild(f);
}

function ligneCompte(c) {
  const moi = c.login === comptes.moi;
  const actions = el('td', { className: 'actions-compte' },
    el('button', { textContent: 'Rôles…', onclick: () => formRoles(c) }),
    el('button', { textContent: 'Nouveau mot de passe', onclick: () => {
      if (!confirm('Générer un nouveau mot de passe pour ' + c.login + ' ?\n'
                   + 'L’ancien cesse de fonctionner et ses sessions se ferment.')) return;
      actionCompte('/api/comptes/reinitialiser', { login: c.login });
    } }),
    c.actif
      ? el('button', { className: 'danger', textContent: 'Désactiver', disabled: moi,
          title: moi ? 'Tu ne peux pas te désactiver toi-même' : '',
          onclick: () => {
            if (!confirm('Désactiver ' + c.login + ' ? Il ne pourra plus se connecter, '
                         + 'ses sessions se ferment.')) return;
            actionCompte('/api/comptes/modifier', { login: c.login, actif: false },
                         c.login + ' désactivé.');
          } })
      : el('button', { textContent: 'Réactiver', onclick: () =>
          actionCompte('/api/comptes/modifier', { login: c.login, actif: true },
                       c.login + ' réactivé.') }));
  return el('tr', { className: c.actif ? '' : 'inactif' },
    el('td', null, el('b', { textContent: c.login }),
       moi ? el('span', { className: 'lecture', textContent: ' (toi)' }) : null),
    el('td', null, ...c.roles.map(r => el('span', { className: 'puce-role', textContent: r }))),
    el('td', { textContent: etatCompte(c) }),
    el('td', { className: 'doux', textContent: c.derniere_connexion || 'jamais' }),
    el('td', { className: 'doux', textContent: c.cree_le + ' par ' + c.cree_par }),
    el('td', { className: 'num', textContent: String(c.sessions) }),
    actions);
}

function formNouveauCompte() {
  const login = el('input', { type: 'text', required: true, maxLength: 64,
    autocomplete: 'off', spellcheck: false, placeholder: 'prenom.nom' });
  const zone = el('div', { className: 'hotes-depot' }, ...casesRoles(['lecture']));
  const f = el('form', { className: 'nouveau-compte' },
    el('label', { className: 'champ' }, 'Nouveau compte', login),
    zone,
    el('button', { type: 'submit', className: 'primaire', textContent: 'Créer le compte' }));
  f.onsubmit = ev => {
    ev.preventDefault();
    const roles = rolesCoches(zone);
    if (!roles.length) { messageComptes('coche au moins un rôle', false); return; }
    actionCompte('/api/comptes', { login: login.value.trim().toLowerCase(), roles },
                 'Compte créé.');
  };
  return f;
}

async function dessinerComptes() {
  const corps = document.getElementById('comptes-corps');
  let rep;
  try {
    const r = await fetch('/api/comptes', { headers: { Accept: 'application/json' } });
    if (r.status === 401) { sessionExpiree(); return; }
    rep = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(rep.erreur || 'refus (' + r.status + ')');
  } catch (e) {
    corps.replaceChildren(el('p', { className: 'msg ko', textContent:
      'Comptes illisibles : ' + (e.message === 'Failed to fetch'
        ? 'le service n’a pas répondu' : e.message) }));
    return;
  }
  comptes.liste = rep.comptes || [];
  comptes.moi = rep.moi;
  const tete = el('tr', null, ...['Identifiant', 'Rôles', 'État', 'Dernière connexion',
    'Créé', 'Sessions', ''].map(t => el('th', { textContent: t })));
  corps.replaceChildren(
    ...(comptes.mdp ? [blocMotDePasse(comptes.mdp)] : []),
    el('p', { id: 'comptes-msg', className: 'msg', role: 'alert', hidden: true }),
    el('table', { className: 'comptes' }, el('thead', null, tete),
       el('tbody', null, ...comptes.liste.map(ligneCompte))),
    formNouveauCompte());
}
