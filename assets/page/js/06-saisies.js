// --------------------------------------------------------------- saisies
const tiroir = document.getElementById('tiroir');
function fermerTiroir() { tiroir.classList.remove('visible'); }
document.getElementById('tiroir-fermer').onclick = fermerTiroir;

function ouvrirTiroir(titre, sous) {
  document.getElementById('tiroir-titre').textContent = titre;
  document.getElementById('tiroir-type').textContent = sous || '';
  const corps = document.getElementById('tiroir-corps');
  corps.replaceChildren();
  tiroir.classList.add('visible');
  return corps;
}

function champ(f, etiquette, attrs) {
  const id = 'c-' + Math.random().toString(36).slice(2, 8);
  const i = el(attrs.tag || 'input', Object.assign({ id }, attrs));
  delete i.tag;
  f.append(el('label', { textContent: etiquette, htmlFor: id }), i);
  return i;
}

function periode(f, valeur) {
  const s = el('select');
  for (const p of ['mois', 'an']) {
    s.appendChild(el('option', { value: p, textContent: 'par ' + p,
                                 selected: (valeur || 'mois') === p }));
  }
  f.append(el('label', { textContent: 'Période des prix' }), s);
  return s;
}

function message(f, texte, ok) {
  const p = f.querySelector('.msg') || f.appendChild(el('p'));
  p.className = 'msg ' + (ok ? 'ok' : 'ko');
  p.textContent = texte;
}

async function poster(url, charge) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(charge),
  });
  if (r.status === 401) {
    sessionExpiree();
    throw new Error('session expirée : reconnecte-toi, la saisie n’a pas été enregistrée');
  }
  const rep = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(rep.erreur || ('refus (' + r.status + ')'));
  return rep;
}

// envoie, recharge l'arbre, ferme le tiroir ; garde le formulaire ouvert et
// dit pourquoi en cas de refus
async function soumettre(f, travail, succes) {
  const boutons = f.querySelectorAll('button');
  boutons.forEach(b => { b.disabled = true; });
  try {
    await travail();
    await chargerCommerce();
    fermerTiroir();
    if (succes) flash(succes);
  } catch (e) {
    message(f, e.message === 'Failed to fetch'
      ? 'le service n’a pas répondu' : e.message, false);
  } finally {
    boutons.forEach(b => { b.disabled = false; });
  }
}

function flash(texte) {
  const z = document.getElementById('flash');
  z.textContent = texte;
  z.classList.add('visible');
  clearTimeout(flash.t);
  flash.t = setTimeout(() => z.classList.remove('visible'), 2600);
}

// Le choix d'un role : le catalogue, et de quoi l'etendre sans quitter le
// formulaire. code() rend le code retenu, en creant le role au besoin ; un
// role cree reste cree meme si la saisie qui suit est refusee, et le
// formulaire le selectionne pour qu'un nouvel envoi ne le recree pas.
function choixRole(f, actuel, etiquette) {
  const id = 'c-' + Math.random().toString(36).slice(2, 8);
  const s = el('select', { id });
  s.appendChild(el('option', { value: '', textContent: '— aucun —' }));
  for (const r of commerce.roles) {
    s.appendChild(el('option', { value: r.code, textContent: r.libelle,
                                 selected: r.code === actuel }));
  }
  if (actuel && !commerce.roles.some(r => r.code === actuel)) {
    s.appendChild(el('option', { value: actuel, selected: true,
                                 textContent: actuel + ' (retiré du catalogue)' }));
  }
  const plus = el('option', { value: '+', textContent: '＋ Nouveau rôle…' });
  s.appendChild(plus);
  const nouveau = el('input', { type: 'text', maxLength: 80, hidden: true,
                                placeholder: 'nom du rôle, ex. Serveur Node.js' });
  s.onchange = () => {
    nouveau.hidden = s.value !== '+';
    if (!nouveau.hidden) nouveau.focus();
  };
  f.append(el('label', { textContent: etiquette || 'Rôle', htmlFor: id }), s, nouveau);
  return {
    change: () => s.value !== (actuel || ''),
    async code() {
      if (s.value !== '+') return s.value;
      if (!nouveau.value.trim()) throw new Error('donne un nom au nouveau rôle');
      const r = (await poster('/api/roles', { libelle: nouveau.value })).role;
      commerce.roles.push(r);
      s.insertBefore(el('option', { value: r.code, textContent: r.libelle }), plus);
      s.value = r.code;
      nouveau.hidden = true;
      return r.code;
    },
  };
}

// Plusieurs enfants d'un coup sous la meme carte : un nom par ligne, le reste
// en commun. « La VM A heberge 4 sites a 40 € » : quatre lignes, 40 €, et le
// formulaire annonce ce que la carte rapportera avant qu'on enregistre.
function formEnfants(parent) {
  const corps = ouvrirTiroir('Ajouter des enfants',
    'sous ' + LIBELLE[parent.type].toLowerCase() + ' ' + parent.nom);
  const f = el('form', { autocomplete: 'off', className: 'saisie' });
  const noms = champ(f, 'Noms — un par ligne', { tag: 'textarea', rows: 5,
    required: true, placeholder: 'boutique.exemple.fr\nblog.exemple.fr' });
  // sous une VM, on declare surtout des sites
  const role = choixRole(f, parent.type === 'vm'
    && commerce.roles.some(r => r.code === 'site') ? 'site' : null);
  const paire = el('div', { className: 'paire' });
  const pv = el('input', { type: 'number', step: '0.01', min: '0',
                           placeholder: 'PV de chacun', ariaLabel: 'Prix de vente de chacun' });
  const pa = el('input', { type: 'number', step: '0.01', min: '0',
                           placeholder: 'PA de chacun', ariaLabel: 'Prix d’achat de chacun' });
  paire.append(pv, pa);
  f.append(el('label', { textContent: 'Prix de vente · prix d’achat, de chacun' }), paire);
  const per = periode(f);
  const client = champ(f, 'Client (facultatif, le même pour tous)',
                       { type: 'text', maxLength: 190 });
  const total = el('p', { className: 'total-lot', ariaLive: 'polite' });
  f.appendChild(total);

  const lister = () => noms.value.split('\n').map(x => x.trim()).filter(Boolean);
  const maj = () => {
    const n = lister().length;
    if (!n) { total.textContent = 'Aucun nom saisi.'; return; }
    const div = per.value === 'an' ? 12 : 1;
    const v = Number(pv.value || 0) / div, a = Number(pa.value || 0) / div;
    total.replaceChildren(
      el('b', { textContent: n + ' enfant' + (n > 1 ? 's' : '') + ' × ' + euro(v)
                             + ' = ' + euro(n * v) + ' / mois' }),
      a ? ' (PA ' + euro(n * a) + ')' : '',
      el('br'),
      LIBELLE[parent.type] + ' ' + parent.nom + ' : CA ' + euro(parent.pv) + ' → ',
      el('b', { textContent: euro(parent.pv + n * v) }),
      ', ROI ', el('b', { className: 'marge ' + classeMarge(parent.roi + n * (v - a)),
                          textContent: signe(parent.roi + n * (v - a)) }));
  };
  [noms, pv, pa, per].forEach(x => x.addEventListener('input', maj));
  maj();

  f.appendChild(el('div', { className: 'actions' },
    el('button', { type: 'submit', textContent: 'Enregistrer' }),
    el('button', { type: 'button', textContent: 'Annuler', onclick: fermerTiroir })));
  f.onsubmit = ev => {
    ev.preventDefault();
    const liste = lister();
    soumettre(f, async () => {
      await poster('/api/elements', {
        parent_type: parent.type, parent_ref: parent.cle, noms: liste,
        role: await role.code(), prix_vente: pv.value, prix_achat: pa.value,
        periode: per.value, client: client.value,
      });
    }, liste.length > 1 ? liste.length + ' enfants déclarés.' : 'Enfant déclaré.');
  };
  corps.appendChild(f);
  noms.focus();
}

// parent : la carte sous laquelle on declare ; existant : l'element a editer
function formElement(parent, existant) {
  const info = existant ? existant.infos : null;
  const corps = ouvrirTiroir(existant ? 'Élément #' + existant.cle : 'Nouvel élément',
    existant ? 'sous ' + info.parent_type + ' ' + info.parent_ref
             : 'sous ' + LIBELLE[parent.type].toLowerCase() + ' ' + parent.nom);
  const f = el('form', { autocomplete: 'off', className: 'saisie' });
  const nom = champ(f, 'Nom', { type: 'text', required: true, maxLength: 190,
    value: existant ? existant.nom : '', placeholder: 'ex. boutique.exemple.fr' });
  const role = choixRole(f, info ? info.role : null);
  const paire = el('div', { className: 'paire' });
  const pv = el('input', { type: 'number', step: '0.01', min: '0',
    placeholder: 'PV', value: info ? (info.prix_vente || '') : '' });
  const pa = el('input', { type: 'number', step: '0.01', min: '0',
    placeholder: 'PA', value: info ? (info.prix_achat || '') : '' });
  paire.append(pv, pa);
  f.append(el('label', { textContent: 'Prix de vente · prix d’achat' }), paire);
  const per = periode(f, info && info.periode);
  const contrat = champ(f, 'Contrat (facultatif)', { type: 'text', maxLength: 100,
    value: info ? (info.contrat || '') : '' });
  const client = champ(f, 'Client (facultatif)', { type: 'text', maxLength: 190,
    value: info ? (info.client || '') : '' });
  const noteTxt = champ(f, 'Note (facultatif)', { tag: 'textarea', maxLength: 500,
    rows: 2, value: info ? (info.note || '') : '' });

  const actions = el('div', { className: 'actions' },
    el('button', { type: 'submit', textContent: 'Enregistrer' }),
    el('button', { type: 'button', textContent: 'Annuler', onclick: fermerTiroir }));
  if (existant) {
    actions.appendChild(el('button', { type: 'button', className: 'danger',
      textContent: 'Retirer',
      onclick: () => {
        if (!confirm('Retirer « ' + existant.nom + ' » ? Ses enfants passeront '
                     + 'hors parc.')) return;
        soumettre(f, () => poster('/api/element/retirer', { id: existant.cle }),
                  'Élément retiré.');
      } }));
  }
  f.appendChild(actions);
  f.onsubmit = ev => {
    ev.preventDefault();
    const charge = {
      nom: nom.value, prix_vente: pv.value,
      prix_achat: pa.value, periode: per.value, contrat: contrat.value,
      client: client.value, note: noteTxt.value,
    };
    if (existant) {
      Object.assign(charge, { id: existant.cle, parent_type: info.parent_type,
                              parent_ref: info.parent_ref });
    } else {
      Object.assign(charge, { parent_type: parent.type, parent_ref: parent.cle });
    }
    soumettre(f, async () => {
      charge.role = await role.code();
      await poster('/api/element', charge);
    }, existant ? 'Élément corrigé.' : 'Élément déclaré.');
  };
  corps.appendChild(f);
  nom.focus();
}

// Modifier : le role de la carte, et ses prix. Un prix remplace la valeur
// PROPRE de la carte ; les enfants continuent de s'ajouter par-dessus, le
// tiroir le rappelle.
function formCorrection(c) {
  const corps = ouvrirTiroir('Modifier ' + c.nom, LIBELLE[c.type]);
  corps.appendChild(el('p', { className: 'lecture', textContent:
    'Un prix remplace la valeur propre de la carte ; ses enfants '
    + 'continuent de s’y ajouter. Un prix vidé lève la correction.' }));
  const f = el('form', { autocomplete: 'off', className: 'saisie' });
  const role = choixRole(f, c.infos.role);
  const champs = [];
  const auto = {
    prix_achat: c.type === 'serveur' ? c.pa_auto : null,
    prix_vente: c.type === 'vm' ? c.pv_auto : null,
  };
  const libelle = { prix_achat: 'PA propre', prix_vente: 'PV propre',
                    cluster: 'Cluster' };
  const noms = ['prix_achat', 'prix_vente'].concat(c.type === 'serveur' ? ['cluster'] : []);
  for (const k of noms) {
    const corr = c.corrections[k];
    const actuel = corr ? corr.valeur : '';
    let aide = '';
    if (k === 'prix_achat' && c.type === 'serveur') {
      aide = auto.prix_achat === null ? 'aucun tarif collecté'
                                      : 'collecté : ' + euro(auto.prix_achat);
    } else if (k === 'prix_vente' && c.type === 'vm') {
      aide = 'somme des contrats : ' + euro(auto.prix_vente || 0);
    } else if (k === 'cluster') {
      aide = 'Proxmox : ' + (c.infos.cluster_pve || 'nœud seul');
    } else {
      aide = 'aucune valeur collectée (0)';
    }
    const i = champ(f, libelle[k] + ' — ' + aide, k === 'cluster'
      ? { type: 'text', maxLength: 190, value: actuel || '',
          placeholder: 'nom du regroupement' }
      : { type: 'number', step: '0.01', min: '0', value: actuel === '' ? '' : actuel,
          placeholder: 'valeur collectée' });
    if (k === 'cluster') {
      i.setAttribute('list', 'clusters-connus');
      f.appendChild(el('datalist', { id: 'clusters-connus' },
        ...commerce.arbre.filter(x => !x.infos.seul)
          .map(x => el('option', { value: x.nom }))));
    }
    champs.push({ k, i, avant: actuel === null || actuel === undefined ? '' : String(actuel) });
  }
  const motif = champ(f, 'Motif (obligatoire pour un prix)', { type: 'text',
    maxLength: 300, placeholder: 'ex. tarif négocié, licence Windows…' });
  f.appendChild(el('div', { className: 'actions' },
    el('button', { type: 'submit', textContent: 'Enregistrer' }),
    el('button', { type: 'button', textContent: 'Annuler', onclick: fermerTiroir })));
  f.onsubmit = ev => {
    ev.preventDefault();
    const changes = champs.filter(x => x.i.value.trim() !== x.avant);
    if (!changes.length && !role.change()) { message(f, 'rien n’a changé', false); return; }
    if (changes.length && !motif.value.trim()) {
      message(f, 'un prix corrigé demande un motif', false);
      motif.focus();
      return;
    }
    soumettre(f, async () => {
      if (role.change()) {
        await poster('/api/role', { cible_type: c.type, cible_ref: c.cle,
                                    role: await role.code() });
      }
      for (const x of changes) {
        await poster('/api/surcharge', { cible_type: c.type, cible_ref: c.cle,
          champ: x.k, valeur: x.i.value.trim(), motif: motif.value });
      }
    }, 'Modifications enregistrées.');
  };
  corps.appendChild(f);
}

// vm : la carte de la VM ; la vente porte sa reference cluster/vmid, et son
// nom comme libelle
function formVente(vm, existant) {
  const corps = ouvrirTiroir(existant ? 'Contrat ' + existant.contrat : 'Nouveau contrat',
                             'VM ' + vm.nom);
  const f = el('form', { autocomplete: 'off', className: 'saisie' });
  const contrat = champ(f, 'Numéro de contrat (Dolibarr)', { type: 'text',
    required: true, maxLength: 100, value: existant ? existant.contrat : '',
    readOnly: !!existant });
  const prix = champ(f, 'Prix de vente', { type: 'number', step: '0.01',
    min: '0', required: true, value: existant ? existant.prix_vente : '' });
  const per = periode(f, existant && existant.periode);
  const client = champ(f, 'Client (facultatif)', { type: 'text', maxLength: 190,
    value: existant && existant.client ? existant.client : '' });
  const actions = el('div', { className: 'actions' },
    el('button', { type: 'submit', textContent: 'Enregistrer' }),
    el('button', { type: 'button', textContent: 'Annuler', onclick: fermerTiroir }));
  if (existant) {
    actions.appendChild(el('button', { type: 'button', className: 'danger',
      textContent: 'Résilier',
      onclick: () => {
        if (!confirm('Résilier le contrat ' + existant.contrat + ' ?')) return;
        soumettre(f, () => poster('/api/commercial/resilier',
                                  { contrat: existant.contrat }),
                  'Contrat résilié.');
      } }));
  }
  f.appendChild(actions);
  f.onsubmit = ev => {
    ev.preventDefault();
    soumettre(f, () => poster('/api/commercial', {
      vm_ref: vm.cle, vm_name: vm.nom, contrat: contrat.value, prix_vente: prix.value,
      periode: per.value, client: client.value,
    }), existant ? 'Contrat corrigé.' : 'Vente enregistrée.');
  };
  corps.appendChild(f);
  (existant ? prix : contrat).focus();
}

// Ce qui a change, champ par champ : "prix_vente : 12.5 → 25". Le JSON
// complet reste en base pour un litige ; a l'ecran, seul l'ecart se lit.
function ecarts(avant, apres) {
  const lire = t => { try { return t ? JSON.parse(t) : {}; } catch (e) { return {}; } };
  const a = lire(avant), b = lire(apres), out = [];
  // la cible est deja dans le titre de la ligne
  const ignore = new Set(['id', 'cree_le', 'cree_par', 'maj_le', 'maj_par',
                          'champ', 'cible_ref', 'cible_type']);
  for (const k of Object.keys(b)) {
    if (ignore.has(k)) continue;
    const va = a[k] === undefined ? null : a[k], vb = b[k];
    if (String(va) === String(vb)) continue;
    if (!avant && (vb === null || vb === '')) continue;
    out.push(k + ' : ' + (avant ? (va === null ? '∅' : va) + ' → ' : '')
             + (vb === null ? '∅' : vb));
  }
  return out;
}

async function afficherJournal() {
  const corps = ouvrirTiroir('Journal des saisies', '200 dernières');
  if (droits.admin) {
    // la chaine : une ligne modifiee ou effacee, meme en base, se voit ici
    const etat = el('p', { className: 'lecture', textContent: 'Vérification de la chaîne…' });
    corps.appendChild(etat);
    fetch('/api/journal/integrite', { headers: { Accept: 'application/json' } })
      .then(r => r.ok ? r.json() : Promise.reject(new Error('refus (' + r.status + ')')))
      .then(v => {
        etat.className = 'msg ' + (v.ok ? 'ok' : 'ko');
        etat.textContent = v.ok
          ? '✓ Chaîne intacte : ' + v.chainees + ' ligne(s) chaînée(s)'
            + (v.hors_chaine ? ', ' + v.hors_chaine + ' antérieure(s) hors chaîne' : '')
          : '⚠ Chaîne rompue à la ligne ' + v.rupture.n + ' : ' + v.rupture.motif;
      })
      .catch(e => { etat.textContent = 'Chaîne non vérifiée : ' + e.message; });
  }
  try {
    const r = await fetch('/api/journal', { headers: { Accept: 'application/json' } });
    if (!r.ok) throw new Error('refus (' + r.status + ')');
    const lignes = (await r.json()).journal || [];
    if (!lignes.length) {
      corps.appendChild(el('p', { className: 'lecture', textContent: 'Aucune saisie.' }));
    }
    const ul = el('ul', { className: 'journal' });
    for (const l of lignes) {
      ul.appendChild(el('li', null,
        el('b', { textContent: l.action + ' · ' + l.cible }),
        el('small', { textContent: l.le.replace('T', ' ').slice(0, 16) + ' · ' + l.par }),
        ...ecarts(l.avant, l.apres).map(t => el('code', { textContent: t }))));
    }
    corps.appendChild(ul);
  } catch (e) {
    corps.appendChild(el('p', { className: 'msg ko', textContent: e.message }));
  }
}

document.getElementById('btn-rent').onclick = () => basculerRent(!vueRent.actif);
document.getElementById('rent-recherche').oninput = ev => {
  vueRent.filtre = ev.target.value.trim().toLowerCase();
  apresFiltre();
};
document.getElementById('rent-perte').onchange = ev => {
  vueRent.perte = ev.target.checked; apresFiltre();
};
document.getElementById('rent-alertes').onchange = ev => {
  vueRent.alertes = ev.target.checked; apresFiltre();
};
document.getElementById('rent-deplier').onclick = () => {
  const tout = commerce.ouverts.size < commerce.index.size / 2;
  commerce.ouverts.clear();
  if (tout) {
    for (const { chemin } of commerce.index.values()) commerce.ouverts.add(chemin.join('>'));
  } else {
    commerce.arbre.forEach(c => commerce.ouverts.add(cleIndex(c)));
  }
  document.getElementById('rent-deplier').textContent = tout ? 'Replier' : 'Tout déplier';
  dessinerRent();
};
document.getElementById('rent-journal').onclick = afficherJournal;

