#!/usr/bin/env python3
"""
Service web de l'inventaire : emet les jetons et sert l'inventaire aux postes.

    python serveur_web.py                      ecoute sur 127.0.0.1:8080
    python serveur_web.py --dev-user evan      sans Apache devant, pour tester
    python serveur_web.py --dev-user evan --dev-groupes inventaire-lecture
                                               idem, en eprouvant les roles

Il n'ecoute que sur la boucle locale et se place derriere Apache, qui porte
l'authentification OIDC. Apache transmet l'identite dans un en-tete :

    RequestHeader set X-Remote-User %{REMOTE_USER}s
    ProxyPass /api/ http://127.0.0.1:8080/api/

Cet en-tete n'est digne de confiance que parce que rien d'autre qu'Apache ne
peut joindre le service : si on l'exposait sur 0.0.0.0, n'importe qui pourrait
le forger et s'emettre des jetons.

Routes :
    POST /api/token       emet un jeton a usage unique (identite OIDC requise)
    GET  /api/inventaire  rend l'inventaire Ansible (jeton requis, consomme)
    GET  /api/sante       etat du service, sans authentification
    GET  /api/rentabilite l'arbre cluster > serveur > VM > elements
    GET  /api/export.csv  le meme arbre, pour Excel ou Calc
    GET  /api/journal     les dernieres saisies, qui et quoi
    GET  /api/commercial  les ventes, ?vm=nom pour une seule
    POST /api/commercial  cree ou corrige une vente (role commercial)
    POST /api/commercial/resilier   passe un contrat a inactif
    POST /api/element     declare ou corrige un element (site, vhost...)
    POST /api/element/retirer       passe un element a inactif
    POST /api/surcharge   corrige une valeur collectee, ou leve la correction
    GET  /api/vues        les vues enregistrees (partagees, et les siennes)
    POST /api/vues        cree ou modifie une vue (lecteur ; auteur ou admin)
    POST /api/vues/retirer          retire une vue

Toutes les routes autres que sante et inventaire exigent l'identite.
Toutes les ecritures exigent le role commercial, un corps JSON, et une
requete qui vienne de la page elle-meme (voir meme_origine()).

Trois roles, declares dans infra.yml sous web:, chacun par un groupe OIDC
qu'Apache transmet dans X-Remote-Groups, ou par une liste de comptes :

    lecture      groupe_lecture / lecteurs       page, rentabilite, export
    commercial   groupe_commercial / commerciaux ventes, elements, corrections
    admin        groupe_admin / admins           jetons pour le client Ansible

Commercial et admin emportent la lecture. Sans lecture declaree, toute
identite authentifiee lit. Sans commercial declare, personne n'ecrit. Sans
admin declare, un lecteur peut emettre un jeton : il voit deja tout ce que
l'inventaire Ansible contient.

GET /api/moi rend l'identite, les groupes et les roles tels que le service
les recoit : c'est la verification de la chaine Authentik > Apache > service.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import infra_inventory as orch  # noqa: E402
import inventory_db  # noqa: E402
import rentabilite as calcul  # noqa: E402

DUREE_JETON_MIN = 10

# En-tetes poses sur toutes les reponses. L'API ne rend que du JSON et du
# CSV : rien a encadrer, rien a interpreter autrement que ce qui est annonce.
ENTETES_SURS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cache-Control": "no-store",
}

# La page, servie en developpement : meme politique qu'Apache en production.
# connect-src 'self' est ce qui permet a la page d'appeler l'API.
CSP_PAGE = ("default-src 'none'; script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; img-src data:; connect-src 'self'; "
            "form-action 'none'; frame-ancestors 'self'; base-uri 'none'")

_CONTROLE = re.compile(r"[\x00-\x1f\x7f]")


def pour_journal(v):
    """Une saisie, inoffensive dans un journal.

    Un nom de site contenant un retour a la ligne fabriquerait une fausse
    ligne de journal ; on remplace les caracteres de controle et on borne.
    """
    return _CONTROLE.sub("?", str(v))[:120]


def empreinte(jeton):
    return hashlib.sha256(jeton.encode("utf-8")).hexdigest()


def emettre_jeton(conn, utilisateur, duree_min=DUREE_JETON_MIN):
    jeton = secrets.token_urlsafe(32)
    maintenant = dt.datetime.now()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO client_token (empreinte, emis_par, emis_le, expire_le) "
            "VALUES (%s, %s, %s, %s)",
            (empreinte(jeton), utilisateur, maintenant,
             maintenant + dt.timedelta(minutes=duree_min)))
        # menage au passage : les jetons perimes depuis un jour n'ont plus
        # d'interet, meme pour la tracabilite
        cur.execute("DELETE FROM client_token WHERE expire_le < %s",
                    (maintenant - dt.timedelta(days=1),))
    conn.commit()
    return jeton, duree_min


def consommer_jeton(conn, jeton, origine):
    """Valide et marque le jeton. Renvoie l'emetteur, ou None si refuse."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, emis_par, expire_le, consomme_le FROM client_token "
            "WHERE empreinte = %s", (empreinte(jeton),))
        ligne = cur.fetchone()
        if not ligne:
            return None, "jeton inconnu"
        if ligne["consomme_le"]:
            # Un jeton rejoue signale soit une erreur du client, soit une
            # interception : on refuse et on garde la trace du premier usage.
            return None, "jeton deja utilise le {}".format(ligne["consomme_le"])
        if ligne["expire_le"] < dt.datetime.now():
            return None, "jeton expire"
        cur.execute(
            "UPDATE client_token SET consomme_le = %s, consomme_de = %s "
            "WHERE id = %s AND consomme_le IS NULL",
            (dt.datetime.now(), origine[:45], ligne["id"]))
        if cur.rowcount != 1:
            # course entre deux requetes simultanees : une seule doit gagner
            return None, "jeton deja utilise"
    conn.commit()
    return ligne["emis_par"], None


class Handler(BaseHTTPRequestHandler):
    server_version = "inventaire/1.0"
    conf = None
    dev_user = None
    dev_groupes = None
    page = None

    def log_message(self, format, *args):
        print("{} {} {}".format(
            self.address_string(), self.log_date_time_string(), format % args),
            file=sys.stderr)

    # -- utilitaires -------------------------------------------------------
    def envoyer(self, code, corps, type_, entetes=None):
        self.send_response(code)
        self.send_header("Content-Type", type_)
        self.send_header("Content-Length", str(len(corps)))
        for k, v in dict(ENTETES_SURS, **(entetes or {})).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(corps)

    def repondre(self, code, charge):
        self.envoyer(code, json.dumps(charge, ensure_ascii=False).encode("utf-8"),
                     "application/json; charset=utf-8")

    def meme_origine(self):
        """La requete vient-elle de la page, et pas d'un autre site ?

        L'identite vient du cookie de session OIDC, que le navigateur joint a
        toute requete vers ce domaine — y compris celle qu'un autre site
        declenche par un formulaire cache. Sans ce controle, une page piegee
        ouverte par un commercial pourrait corriger un prix a sa place.

        Sec-Fetch-Site est pose par le navigateur et ne se forge pas depuis
        une page. A defaut (vieux navigateur), Origin doit designer cet hote.
        Un client hors navigateur n'envoie ni l'un ni l'autre : il passe, il
        n'a pas de cookie a se faire voler.
        """
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None:
            return site in ("same-origin", "none")
        origine = self.headers.get("Origin")
        if origine is None:
            return True
        hote = (self.headers.get("Host") or "").lower()
        return urlparse(origine).netloc.lower() == hote

    def identite(self):
        """Identite OIDC transmise par Apache, ou compte de developpement."""
        return self.headers.get("X-Remote-User") or self.dev_user

    def en_dev(self):
        """Requete servie sans Apache, sous l'identite de --dev-user."""
        return bool(self.dev_user) and not self.headers.get("X-Remote-User")

    def groupes(self):
        """Groupes OIDC, tels qu'Apache les transmet (claim "groups") :

            RequestHeader set X-Remote-Groups "%{OIDC_CLAIM_groups}e"

        mod_auth_openidc joint les valeurs d'une claim tableau par des
        virgules. Un en-tete absent ou non resolu ("(null)") ne donne rien.
        """
        if self.en_dev():
            return list(self.dev_groupes or [])
        brut = self.headers.get("X-Remote-Groups") or ""
        return [g.strip() for g in brut.replace(";", ",").split(",")
                if g.strip() and g.strip() != "(null)"]

    def roles(self):
        """Les roles de la requete : lecture, commercial, admin.

        En --dev-user sans --dev-groupes, tous : l'option annonce deja
        qu'elle tient n'importe quelle requete pour authentifiee. Avec
        --dev-groupes, les regles normales, pour les eprouver.
        """
        utilisateur = self.identite()
        if not utilisateur:
            return set()
        if self.en_dev() and self.dev_groupes is None:
            return {"lecture", "commercial", "admin"}

        web = self.conf.get("web") or {}
        groupes = {g.lower() for g in self.groupes()}
        moi = utilisateur.lower()

        def declare(cle_groupe, cle_comptes):
            return bool(web.get(cle_groupe) or web.get(cle_comptes))

        def membre(cle_groupe, cle_comptes):
            groupe = str(web.get(cle_groupe) or "").lower()
            comptes = {str(c).lower() for c in (web.get(cle_comptes) or [])}
            return (groupe and groupe in groupes) or moi in comptes

        r = set()
        if membre("groupe_commercial", "commerciaux"):
            r.add("commercial")
        if membre("groupe_admin", "admins"):
            r.add("admin")
        if (r or not declare("groupe_lecture", "lecteurs")
                or membre("groupe_lecture", "lecteurs")):
            r.add("lecture")
        return r

    def exiger(self, role):
        """Vrai si la requete a le role ; sinon repond 401 ou 403."""
        if not self.identite():
            self.repondre(401, {"erreur": "identite absente : ce service doit "
                                          "etre place derriere une "
                                          "authentification"})
            return False
        if role not in self.roles():
            self.repondre(403, {"erreur": "{} n'a pas le role {}".format(
                self.identite(), role)})
            return False
        return True

    def peut_vendre(self):
        """Qui a le droit d'ecrire une vente, un element, une correction.

        Refus par defaut : tant que rien n'est declare dans infra.yml,
        personne n'ecrit. Une interface de saisie qui s'ouvre d'elle-meme
        parce que la configuration est vide, c'est l'inverse de ce qu'on veut.
        """
        utilisateur = self.identite()
        if not utilisateur:
            return False, "identite absente"
        if "commercial" in self.roles():
            return True, None
        web = self.conf.get("web") or {}
        if not (web.get("commerciaux") or web.get("groupe_commercial")):
            return False, ("aucun commercial declare : renseigne "
                           "web.commerciaux ou web.groupe_commercial "
                           "dans infra.yml")
        return False, "{} n'a pas le droit d'enregistrer une vente".format(
            utilisateur)

    def peut_emettre(self):
        """Un jeton Ansible : admin si un admin est declare, lecture sinon."""
        web = self.conf.get("web") or {}
        if web.get("groupe_admin") or web.get("admins"):
            return "admin"
        return "lecture"

    def corps_json(self, limite=16384):
        """Lit le corps de la requete.

        La limite evite qu'un Content-Length fantaisiste fasse grossir le
        processus : on n'alloue jamais plus que ce qu'une vente pese.

        Le type application/json est exige : un formulaire HTML ne sait pas
        l'envoyer, et un fetch d'un autre site qui le pose declenche une
        requete preliminaire CORS que ce service ne valide jamais.
        """
        type_ = (self.headers.get("Content-Type") or "").split(";")[0]
        if type_.strip().lower() != "application/json":
            return None, "Content-Type application/json attendu"
        try:
            taille = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None, "Content-Length illisible"
        if taille <= 0:
            return None, "corps vide"
        if taille > limite:
            return None, "corps trop volumineux"
        try:
            charge = json.loads(self.rfile.read(taille).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, "JSON invalide : {}".format(exc)
        if not isinstance(charge, dict):
            return None, "un objet JSON est attendu"
        return charge, None

    def base(self):
        return inventory_db.connect(self.conf)

    def lire(self, fn):
        """Ouvre la base, appelle fn(conn), ferme quoi qu'il arrive.

        Renvoie (resultat, None), ou (None, True) si une reponse d'erreur a
        deja ete envoyee.
        """
        try:
            conn = self.base()
        except inventory_db.DbError as exc:
            self.repondre(503, {"erreur": str(exc)})
            return None, True
        try:
            return fn(conn), None
        except inventory_db.SaisieInterdite as exc:
            self.repondre(403, {"erreur": str(exc)})
        except inventory_db.VenteInvalide as exc:
            self.repondre(422, {"erreur": str(exc)})
        except inventory_db.DbError as exc:
            self.repondre(503, {"erreur": str(exc)})
        finally:
            conn.close()
        return None, True

    def ecriture_permise(self):
        """Role, origine et corps : tout ce qu'une ecriture doit passer.

        Renvoie le corps JSON, ou None si une reponse d'erreur est partie.
        """
        if not self.meme_origine():
            self.log_message("ecriture refusee : requete d'un autre site")
            self.repondre(403, {"erreur": "requete d'une autre origine"})
            return None
        peut, motif = self.peut_vendre()
        if not peut:
            code = 401 if motif == "identite absente" else 403
            self.repondre(code, {"erreur": motif})
            return None
        charge, erreur = self.corps_json()
        if erreur:
            self.repondre(400, {"erreur": erreur})
            return None
        return charge

    # -- routes ------------------------------------------------------------
    def do_POST(self):
        route = self.path.split("?")[0].rstrip("/")
        ecritures = {
            "/api/commercial": self.enregistrer_vente,
            "/api/commercial/resilier": self.resilier_vente,
            "/api/element": self.enregistrer_element,
            "/api/element/retirer": self.retirer_element,
            "/api/surcharge": self.poser_surcharge,
            "/api/vues": self.enregistrer_vue,
            "/api/vues/retirer": self.retirer_vue,
        }
        if route in ecritures:
            return ecritures[route]()
        if route != "/api/token":
            return self.repondre(404, {"erreur": "route inconnue"})
        if not self.meme_origine():
            return self.repondre(403, {"erreur": "requete d'une autre origine"})
        if not self.exiger(self.peut_emettre()):
            return
        utilisateur = self.identite()
        res, echec = self.lire(lambda c: emettre_jeton(c, utilisateur))
        if echec:
            return
        jeton, duree = res
        self.log_message("jeton emis pour %s", pour_journal(utilisateur))
        self.repondre(200, {
            "token": jeton, "expire_dans_min": duree, "emis_par": utilisateur,
            "usage": "une seule fois",
        })

    def do_GET(self):
        route = self.path.split("?")[0].rstrip("/")
        if route == "/api/sante":
            return self.sante()
        if route == "/api/inventaire":
            return self.inventaire()
        if route == "/api/rentabilite":
            return self.rentabilite()
        if route == "/api/export.csv":
            return self.exporter_csv()
        if route == "/api/journal":
            return self.journal()
        if route == "/api/moi":
            return self.moi()
        if route == "/api/vues":
            return self.lister_vues()
        if route == "/api/commercial":
            return self.lister_ventes()
        if route in ("", "/index.html") and self.page:
            return self.servir_page()
        return self.repondre(404, {"erreur": "route inconnue"})

    def servir_page(self):
        """Sert le schema. En production c'est Apache qui le fait ; utile ici
        pour que la page et l'API partagent la meme origine."""
        try:
            corps = Path(self.page).read_bytes()
        except OSError as exc:
            return self.repondre(503, {"erreur": "page illisible : {}".format(exc)})
        self.envoyer(200, corps, "text/html; charset=utf-8",
                     {"Content-Security-Policy": CSP_PAGE,
                      "X-Frame-Options": "SAMEORIGIN"})

    # -- volet commercial --------------------------------------------------
    def requete(self):
        return parse_qs(urlparse(self.path).query)

    def lister_ventes(self):
        """Les ventes, toutes ou celles d'une VM."""
        if not self.exiger("lecture"):
            return
        q = self.requete()
        vm = (q.get("vm") or [None])[0]
        toutes = (q.get("toutes") or ["0"])[0] in ("1", "true", "oui")
        ventes, echec = self.lire(lambda c: inventory_db.lister_ventes(
            c, vm=vm, actives=not toutes))
        if echec:
            return
        peut, _ = self.peut_vendre()
        self.repondre(200, {"ventes": ventes, "peut_ecrire": peut})

    def enregistrer_vente(self):
        """Cree ou corrige une vente. Reserve aux comptes declares."""
        charge = self.ecriture_permise()
        if charge is None:
            return

        # On controle la saisie avant d'ouvrir la base : une ligne refusee ne
        # doit pas couter une connexion, et le commercial doit lire "prix
        # negatif" plutot qu'une erreur de connexion qui ne le concerne pas.
        try:
            inventory_db.verifier_vente(charge)
        except inventory_db.VenteInvalide as exc:
            return self.repondre(422, {"erreur": str(exc)})

        utilisateur = self.identite()
        vente, echec = self.lire(
            lambda c: inventory_db.enregistrer_vente(c, charge, utilisateur))
        if echec:
            return

        self.log_message("vente %s par %s : %s -> %.2f %s/%s",
                         "creee" if vente["cree"] else "corrigee",
                         pour_journal(utilisateur),
                         pour_journal(vente["vm_name"]), vente["prix_vente"],
                         vente["devise"], vente["periode"])
        self.repondre(201 if vente["cree"] else 200,
                      {"enregistre": True, "cree": vente["cree"],
                       "vente": {k: v for k, v in vente.items()
                                 if k != "cree"},
                       "par": utilisateur})

    def resilier_vente(self):
        charge = self.ecriture_permise()
        if charge is None:
            return
        utilisateur = self.identite()
        touche, echec = self.lire(lambda c: inventory_db.desactiver_vente(
            c, charge.get("contrat"), utilisateur))
        if echec:
            return
        if not touche:
            return self.repondre(404, {"erreur": "aucun contrat actif sous ce "
                                                 "numero"})
        self.log_message("contrat resilie par %s : %s", pour_journal(utilisateur),
                         pour_journal(charge.get("contrat")))
        self.repondre(200, {"resilie": True, "par": utilisateur})

    # -- elements et corrections ---------------------------------------------
    def enregistrer_element(self):
        charge = self.ecriture_permise()
        if charge is None:
            return
        utilisateur = self.identite()
        e, echec = self.lire(
            lambda c: inventory_db.enregistrer_element(c, charge, utilisateur))
        if echec:
            return
        self.log_message("element %s par %s : #%s %s sous %s %s",
                         "cree" if e["cree"] else "corrige",
                         pour_journal(utilisateur), e["id"],
                         pour_journal(e["nom"]), e["parent_type"],
                         pour_journal(e["parent_ref"]))
        self.repondre(201 if e["cree"] else 200,
                      {"enregistre": True, "element": e, "par": utilisateur})

    def retirer_element(self):
        charge = self.ecriture_permise()
        if charge is None:
            return
        utilisateur = self.identite()
        touche, echec = self.lire(lambda c: inventory_db.retirer_element(
            c, charge.get("id"), utilisateur))
        if echec:
            return
        if not touche:
            return self.repondre(404, {"erreur": "aucun element actif sous ce "
                                                 "numero"})
        self.log_message("element retire par %s : #%s",
                         pour_journal(utilisateur), pour_journal(charge.get("id")))
        self.repondre(200, {"retire": True, "par": utilisateur})

    def poser_surcharge(self):
        charge = self.ecriture_permise()
        if charge is None:
            return
        utilisateur = self.identite()
        s, echec = self.lire(
            lambda c: inventory_db.poser_surcharge(c, charge, utilisateur))
        if echec:
            return
        self.log_message("correction par %s : %s %s %s = %s",
                         pour_journal(utilisateur), s["cible_type"],
                         pour_journal(s["cible_ref"]), s["champ"],
                         pour_journal(s["valeur"]))
        self.repondre(200, {"enregistre": True, "surcharge": s,
                            "par": utilisateur})

    # -- lectures ------------------------------------------------------------
    def rentabilite(self):
        """L'arbre de rentabilite : cluster > serveur > VM > elements."""
        if not self.exiger("lecture"):
            return
        res, echec = self.lire(inventory_db.rentabilite)
        if echec:
            return
        peut, _ = self.peut_vendre()
        res["peut_ecrire"] = peut
        self.repondre(200, res)

    def exporter_csv(self):
        """Le meme arbre, a plat. Telecharge, jamais affiche dans l'onglet."""
        if not self.exiger("lecture"):
            return
        utilisateur = self.identite()
        res, echec = self.lire(inventory_db.rentabilite)
        if echec:
            return
        nom = "rentabilite-{}.csv".format(dt.date.today().isoformat())
        self.log_message("export CSV pour %s", pour_journal(utilisateur))
        self.envoyer(200, calcul.vers_csv(res), "text/csv; charset=utf-8",
                     {"Content-Disposition":
                      'attachment; filename="{}"'.format(nom)})

    # -- vues enregistrees ----------------------------------------------------
    # Une vue n'est qu'un filtre nomme : tout lecteur en cree. La modifier ou
    # la retirer revient a son auteur, ou a un admin.
    def ecriture_vue(self):
        """Comme ecriture_permise(), mais le role lecture suffit."""
        if not self.meme_origine():
            self.repondre(403, {"erreur": "requete d'une autre origine"})
            return None
        if not self.exiger("lecture"):
            return None
        charge, erreur = self.corps_json()
        if erreur:
            self.repondre(400, {"erreur": erreur})
            return None
        return charge

    def lister_vues(self):
        if not self.exiger("lecture"):
            return
        moi = self.identite()
        vues, echec = self.lire(lambda c: inventory_db.lister_vues(c, moi))
        if echec:
            return
        admin = "admin" in self.roles()
        for v in vues:
            v["modifiable"] = admin or v["cree_par"] == moi
        self.repondre(200, {"vues": vues})

    def enregistrer_vue(self):
        charge = self.ecriture_vue()
        if charge is None:
            return
        moi, admin = self.identite(), "admin" in self.roles()
        v, echec = self.lire(
            lambda c: inventory_db.enregistrer_vue(c, charge, moi, admin))
        if echec:
            return
        self.log_message("vue %s par %s : #%s %s",
                         "creee" if v["cree"] else "modifiee",
                         pour_journal(moi), v["id"], pour_journal(v["nom"]))
        v["modifiable"] = True
        self.repondre(201 if v["cree"] else 200, {"vue": v})

    def retirer_vue(self):
        charge = self.ecriture_vue()
        if charge is None:
            return
        moi, admin = self.identite(), "admin" in self.roles()
        _, echec = self.lire(lambda c: inventory_db.retirer_vue(
            c, charge.get("id"), moi, admin))
        if echec:
            return
        self.log_message("vue retiree par %s : #%s", pour_journal(moi),
                         pour_journal(charge.get("id")))
        self.repondre(200, {"retiree": True})

    def moi(self):
        """Qui suis-je, pour le service. Sert a la page (nom, roles,
        deconnexion) et a verifier la chaine OIDC de bout en bout."""
        utilisateur = self.identite()
        if not utilisateur:
            return self.repondre(401, {"erreur": "identite absente"})
        web = self.conf.get("web") or {}
        # La deconnexion passe par l'URL interne de mod_auth_openidc : elle
        # ferme la session Apache, puis celle d'Authentik (end_session).
        retour = web.get("oidc_redirect_uri") or "/oidc-callback"
        self.repondre(200, {
            "utilisateur": utilisateur,
            "groupes": self.groupes(),
            "roles": sorted(self.roles()),
            "peut_emettre_jeton": self.peut_emettre() in self.roles(),
            "developpement": self.en_dev(),
            "deconnexion": None if self.en_dev() else retour + "?logout=",
        })

    def journal(self):
        if not self.exiger("lecture"):
            return
        lignes, echec = self.lire(inventory_db.lire_journal)
        if echec:
            return
        self.repondre(200, {"journal": lignes})

    def sante(self):
        """Sans authentification : l'etat, jamais le detail.

        Le message d'une erreur de base nomme l'hote, le compte, parfois le
        chemin du schema. Il va au journal du service ; le visiteur anonyme
        n'apprend que "degrade".
        """
        try:
            conn = self.base()
            try:
                data = inventory_db.load_latest(conn)
            finally:
                conn.close()
        except inventory_db.DbError as exc:
            self.log_message("sante degradee : %s", pour_journal(exc))
            return self.repondre(503, {"etat": "degrade",
                                       "erreur": "base indisponible"})
        if data is None:
            return self.repondre(503, {"etat": "degrade",
                                       "erreur": "aucun snapshot en base"})
        age = int(dt.datetime.now().timestamp() - data["generated_at"])
        self.repondre(200, {
            "etat": "ok", "snapshot": data["run_id"], "age_s": age,
            "serveurs": len(data["servers"]), "invites": len(data["guests"]),
        })

    def inventaire(self):
        entete = self.headers.get("Authorization") or ""
        jeton = entete[7:].strip() if entete.lower().startswith("bearer ") else ""
        if not jeton:
            return self.repondre(401, {"erreur": "jeton absent"})
        try:
            conn = self.base()
            try:
                emetteur, motif = consommer_jeton(
                    conn, jeton, self.client_address[0])
                data = None if motif else inventory_db.load_latest(conn)
            finally:
                conn.close()
        except inventory_db.DbError as exc:
            return self.repondre(503, {"erreur": str(exc)})
        if motif:
            self.log_message("jeton refuse : %s", motif)
            return self.repondre(403, {"erreur": motif})
        if data is None:
            return self.repondre(503, {"erreur": "aucun snapshot en base"})

        inventaire = orch.build_ansible(data, self.conf)
        self.log_message("inventaire remis a %s (%d hotes)",
                         emetteur, len(inventaire["_meta"]["hostvars"]))
        self.repondre(200, {
            "snapshot": data["run_id"],
            "genere_le": data["generated_at"],
            "emis_par": emetteur,
            "inventaire": inventaire,
        })


def main():
    ap = argparse.ArgumentParser(
        description="Service web de l'inventaire.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--config", default=os.environ.get("INFRA_CONFIG", "infra.yml"))
    ap.add_argument("--host", default="127.0.0.1",
                    help="n'ecoute que la boucle locale par defaut, et c'est "
                         "ce qui rend l'en-tete d'identite digne de confiance")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--page", metavar="FICHIER",
                    help="sert aussi le schema sur / (en production c'est "
                         "Apache qui s'en charge)")
    ap.add_argument("--dev-user", metavar="NOM",
                    help="identite a utiliser quand Apache n'est pas devant "
                         "(developpement uniquement)")
    ap.add_argument("--dev-groupes", metavar="G1,G2",
                    help="groupes OIDC a simuler avec --dev-user ; sans cette "
                         "option, --dev-user a tous les roles")
    args = ap.parse_args()

    conf = orch.load_config(args.config)
    Handler.conf = conf
    Handler.dev_user = args.dev_user
    if args.dev_groupes is not None:
        Handler.dev_groupes = [g.strip() for g in args.dev_groupes.split(",")
                               if g.strip()]
    Handler.page = args.page

    if args.dev_user and args.host != "127.0.0.1":
        print("refus : --dev-user accepte n'importe quelle requete comme "
              "authentifiee, il ne peut pas servir hors de la boucle locale",
              file=sys.stderr)
        return 2

    serveur = ThreadingHTTPServer((args.host, args.port), Handler)
    print("inventaire : http://{}:{}/api/sante".format(args.host, args.port),
          file=sys.stderr)
    if args.dev_user:
        print("MODE DEVELOPPEMENT : toute requete est vue comme "
              "'{}'".format(args.dev_user), file=sys.stderr)
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        pass
    serveur.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
