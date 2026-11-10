// Le cookie OIDC a expire : Apache repond 401 a l'API (OIDCUnAuthAction).
// Recharger la page repasse par Authentik, qui rouvre la session.
function sessionExpiree() {
  document.getElementById('session').hidden = false;
}
document.getElementById('session-reprendre').onclick = () => location.reload();

// Le volet commercial se charge apres le dessin : la topologie s'affiche
// sans attendre la base, et s'enrichit quand la reponse arrive.
chargerMoi();
chargerCommerce().then(() => chargerVues(prefsRent.vue));
