// ---------------------------------------------------------------- identite
// Qui est connecte, avec quels roles, tels que le service les recoit d'Apache
// apres l'OIDC. L'infobulle montre les groupes recus : c'est le premier
// endroit ou regarder quand un role manque.
async function chargerMoi() {
  try {
    const r = await fetch('/api/moi', { headers: { Accept: 'application/json' } });
    if (!r.ok) return;
    const m = await r.json();
    const zone = document.getElementById('moi');
    zone.replaceChildren(
      el('span', { className: 'moi-nom', textContent: m.utilisateur }),
      el('span', { className: 'moi-roles',
                   textContent: m.roles.length ? m.roles.join(' · ') : 'aucun rôle' }));
    zone.title = 'Groupes reçus : ' + (m.groupes.join(', ') || 'aucun')
      + (m.developpement ? '\nMode développement : pas d’OIDC devant le service' : '');
    if (m.deconnexion) {
      zone.appendChild(el('a', { className: 'bouton', textContent: 'Se déconnecter',
        href: m.deconnexion + encodeURIComponent(location.origin + '/') }));
    }
    if (m.locale) {
      // compte local : la session vit dans le service, c'est lui qui la ferme
      zone.append(
        el('a', { className: 'bouton', textContent: 'Mot de passe',
                  href: '/connexion?changer' }),
        el('button', { textContent: 'Se déconnecter', onclick: async () => {
          await fetch('/api/deconnexion', { method: 'POST',
            headers: { 'Content-Type': 'application/json' }, body: '{}' }).catch(() => {});
          location.replace('/connexion');
        } }));
    }
    zone.hidden = false;
    document.getElementById('btn-jeton').hidden = !m.peut_emettre_jeton;
    droits.admin = m.roles.includes('admin');
    droits.locale = !!m.locale;
    majOnglets();
    if (droits.admin) chargerRecolte();
  } catch (e) { /* page ouverte sans son service : rien a montrer */ }
}

