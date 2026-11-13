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
    POST /api/elements    declare plusieurs enfants d'un coup (4 sites a 40 EUR)
    POST /api/role        pose ou leve le role d'un cluster, serveur ou VM
    POST /api/roles       ajoute un role au catalogue (web, LAMP, proxy...)
    GET  /api/historique  ?niveau=parc|cluster|serveur : series mensuelles
    GET  /api/projection  quand chaque cluster atteindra 85 % puis 100 % de RAM
    GET  /api/recolte     etat de la recolte PVE par hyperviseur (admin)
    POST /api/recolte/jeton  depose un jeton PVE, scelle (admin) : le service
                          ne peut pas le relire, la collecte le teste et le
                          range (voir depot_scelle.py)
    POST /api/surcharge   corrige une valeur collectee, ou leve la correction
    GET  /api/vues        les vues enregistrees (partagees, et les siennes)
    POST /api/vues        cree ou modifie une vue (lecteur ; auteur ou admin)
    POST /api/vues/retirer          retire une vue

En mode local (web.auth: local, tant que l'OIDC n'est pas branche) :
    GET  /connexion       la page de connexion
    POST /api/connexion   ouvre une session (cookie __Host-inventaire)
    POST /api/deconnexion la ferme
    POST /api/mot-de-passe change le mot de passe, obligatoire a la premiere
                          connexion

Toutes les routes autres que sante, inventaire et connexion exigent
l'identite. Elle vient, selon web.auth :

    oidc   (defaut) de l'en-tete X-Remote-User qu'Apache pose apres l'OIDC
    local  de la session d'un compte local (auth_locale.py) ; les en-tetes
           X-Remote-* sont alors ignores, meme s'ils arrivent
Toutes les ecritures exigent le role commercial, un corps JSON, et une
requete qui vienne de la page elle-meme (voir meme_origine()).

Trois roles, declares dans infra.yml sous web:, chacun par un groupe OIDC
qu'Apache transmet dans X-Remote-Groups, ou par une liste de comptes :

    lecture      groupe_lecture / lecteurs       page, rentabilite, export
    commercial   groupe_commercial / commerciaux ventes, elements, corrections,
                                                 roles
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
from http.cookies import CookieError, SimpleCookie
import os
import re
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import auth_locale  # noqa: E402
import capacite  # noqa: E402
import csp  # noqa: E402
import depot_scelle  # noqa: E402
import infra_inventory as orch  # noqa: E402
import inventory_db  # noqa: E402
import rentabilite as calcul  # noqa: E402

DUREE_JETON_MIN = 10
MODES_AUTH = ("oidc", "local")
PAGE_CONNEXION = Path(__file__).resolve().parent / "assets" / "connexion.html"

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
    # Pas de menage ici : le compte web n'a pas DELETE, et c'est voulu. Les
    # jetons perimes sont purges par la collecte (purger_jetons_clients).
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


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
# Chaque route se declare par @route, avec ce qu'elle exige. L'aiguilleur
# applique les controles avant d'appeler la fonction, dans cet ordre :
#
#   1. mode     une route du mode local n'existe pas en OIDC (404)
#   2. origine  tout POST vient de la page elle-meme (meme_origine), sans
#               exception : c'est la parade contre une page piegee qui
#               ecrirait a la place d'un utilisateur connecte
#   3. acces    public, identite, session, lecture, commercial, admin,
#               jeton (le role d'emission, voir peut_emettre)
#   4. corps    le JSON du corps, lu et passe a la fonction
#
# Une route ne refait donc aucun de ces controles elle-meme ; tests_web.py
# verifie sur la table entiere qu'aucune n'y echappe.

ACCES = ("public", "identite", "session", "lecture", "commercial", "admin",
         "jeton")


class Route:
    __slots__ = ("methode", "chemin", "acces", "corps", "limite", "mode", "fn")

    def __init__(self, methode, chemin, acces, corps, limite, mode, fn):
        self.methode, self.chemin, self.acces = methode, chemin, acces
        self.corps, self.limite, self.mode, self.fn = corps, limite, mode, fn


ROUTES = {}


def route(methode, chemin, acces, corps=False, limite=16384, mode=None):
    if acces not in ACCES:
        raise ValueError("acces inconnu : " + acces)
    if corps and methode != "POST":
        raise ValueError("un corps se lit sur un POST")

    def decorateur(fn):
        cle = (methode, chemin)
        if cle in ROUTES:
            raise ValueError("route declaree deux fois : {} {}".format(*cle))
        ROUTES[cle] = Route(methode, chemin, acces, corps, limite, mode, fn)
        return fn
    return decorateur


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

    def mode_local(self):
        return (self.conf.get("web") or {}).get("auth") == "local"

    def jeton_session(self):
        try:
            biscuit = SimpleCookie(self.headers.get("Cookie") or "")
        except CookieError:
            return None
        m = biscuit.get(auth_locale.COOKIE)
        return m.value if m else None

    def compte_local(self):
        """Le compte de la session locale, ou None. Lu une fois par requete."""
        jeton = self.jeton_session()
        cache = getattr(self, "_session", None)
        if cache is not None and cache[0] == jeton:
            return cache[1]
        compte = None
        if jeton:
            try:
                conn = self.base()
                try:
                    compte = auth_locale.session(conn, jeton)
                finally:
                    conn.close()
            except inventory_db.DbError as exc:
                self.log_message("session illisible : %s", pour_journal(exc))
        self._session = (jeton, compte)
        return compte

    def origine_client(self):
        """L'adresse du poste, pour le journal : Apache l'ajoute en dernier
        a X-Forwarded-For. Jamais un critere de securite."""
        xff = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
        return (xff or self.client_address[0])[:45]

    def identite(self):
        """L'identite de la requete.

        En mode local, la session et elle seule : un X-Remote-User venu
        d'ailleurs ne vaut rien. Sinon, l'en-tete qu'Apache pose apres
        l'OIDC, ou le compte de developpement.
        """
        if self.mode_local():
            c = self.compte_local()
            return c["login"] if c else None
        return self.headers.get("X-Remote-User") or self.dev_user

    def en_dev(self):
        """Requete servie sans Apache, sous l'identite de --dev-user."""
        return (bool(self.dev_user) and not self.mode_local()
                and not self.headers.get("X-Remote-User"))

    def groupes(self):
        """Groupes OIDC, tels qu'Apache les transmet (claim "groups") :

            RequestHeader set X-Remote-Groups "%{OIDC_CLAIM_groups}e"

        mod_auth_openidc joint les valeurs d'une claim tableau par des
        virgules. Un en-tete absent ou non resolu ("(null)") ne donne rien.
        """
        if self.mode_local():
            return []
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
        if self.mode_local():
            # aucun role tant que le mot de passe genere n'est pas change
            return set(self.compte_local()["roles"])
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
        if self.mode_local():
            pass                # le role est porte par le compte lui-meme
        elif not (web.get("commerciaux") or web.get("groupe_commercial")):
            return False, ("aucun commercial declare : renseigne "
                           "web.commerciaux ou web.groupe_commercial "
                           "dans infra.yml")
        return False, "{} n'a pas le droit d'enregistrer une vente".format(
            utilisateur)

    def peut_emettre(self):
        """Un jeton Ansible : admin si un admin est declare, lecture sinon."""
        web = self.conf.get("web") or {}
        if self.mode_local() or web.get("groupe_admin") or web.get("admins"):
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
        except Exception as exc:
            # Une erreur SQL brute (droit manquant, colonne absente) remontait
            # jusqu'a http.server, qui coupait la connexion : le navigateur
            # lisait une erreur reseau. Le detail va au journal du service.
            if inventory_db.pymysql and isinstance(exc, inventory_db.pymysql.Error):
                self.log_message("erreur SQL : %s", pour_journal(exc))
                self.repondre(503, {"erreur": "erreur de base, voir le journal "
                                              "du service"})
            else:
                raise
        finally:
            conn.close()
        return None, True

    # -- aiguillage ---------------------------------------------------------
    def do_GET(self):
        self.aiguiller("GET")

    def do_POST(self):
        self.aiguiller("POST")

    def aiguiller(self, methode):
        chemin = self.path.split("?")[0].rstrip("/") or "/"
        r = ROUTES.get((methode, chemin))
        if r is None or (r.mode == "local" and not self.mode_local()):
            return self.repondre(404, {"erreur": "route inconnue"})
        if methode == "POST" and not self.meme_origine():
            self.log_message("%s %s refuse : requete d'un autre site",
                             methode, chemin)
            return self.repondre(403, {"erreur": "requete d'une autre origine"})
        if not self.acces_permis(r.acces):
            return
        if not r.corps:
            return r.fn(self)
        charge, erreur = self.corps_json(r.limite)
        if erreur:
            return self.repondre(400, {"erreur": erreur})
        return r.fn(self, charge)

    def acces_permis(self, acces):
        """Vrai si la requete passe ; sinon la reponse d'erreur est partie."""
        if acces == "public":
            return True
        if acces == "session":
            if self.compte_local():
                return True
            self.repondre(401, {"erreur": "session absente ou expiree"})
            return False
        if acces == "identite":
            if self.identite():
                return True
            self.repondre(401, {"erreur": "identite absente"})
            return False
        if acces == "commercial":
            # peut_vendre dit pourquoi : aucun commercial declare n'est pas
            # la meme chose que ne pas en etre un
            peut, motif = self.peut_vendre()
            if not peut:
                self.repondre(401 if motif == "identite absente" else 403,
                              {"erreur": motif})
            return peut
        role = self.peut_emettre() if acces == "jeton" else acces
        return self.exiger(role)

    @route("POST", "/api/token", "jeton")
    def emettre_jeton_client(self):
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

    def rediriger(self, cible):
        self.send_response(303)
        self.send_header("Location", cible)
        self.send_header("Content-Length", "0")
        for k, v in ENTETES_SURS.items():
            self.send_header(k, v)
        self.end_headers()

    @route("GET", "/connexion", "public", mode="local")
    def servir_connexion(self):
        try:
            corps = PAGE_CONNEXION.read_bytes()
        except OSError as exc:
            self.log_message("page de connexion illisible : %s", pour_journal(exc))
            return self.repondre(503, {"erreur": "page de connexion illisible"})
        # par empreintes : la page de connexion recoit des mots de passe, pas
        # de 'unsafe-inline' sur elle
        self.envoyer(200, corps, "text/html; charset=utf-8",
                     {"Content-Security-Policy": csp.politique(
                         corps.decode("utf-8"), cadre=True),
                      "X-Frame-Options": "DENY"})

    # -- session locale ----------------------------------------------------
    def poser_cookie(self, jeton, duree_s):
        return {"Set-Cookie": "{}={}; Path=/; Secure; HttpOnly; "
                              "SameSite=Strict; Max-Age={}".format(
                                  auth_locale.COOKIE, jeton, duree_s)}

    def avec_base(self, fn):
        """Comme lire(), mais pour les routes de session : un Refus devient
        sa reponse HTTP. Renvoie (resultat, None) ou (None, True)."""
        try:
            conn = self.base()
        except inventory_db.DbError as exc:
            self.log_message("base : %s", pour_journal(exc))
            self.repondre(503, {"erreur": "base indisponible"})
            return None, True
        try:
            return fn(conn), None
        except auth_locale.Refus as exc:
            self.repondre(exc.code, {"erreur": str(exc)})
        except inventory_db.DbError as exc:
            self.log_message("base : %s", pour_journal(exc))
            self.repondre(503, {"erreur": "base indisponible"})
        finally:
            conn.close()
        return None, True

    @route("POST", "/api/connexion", "public", corps=True, limite=2048, mode="local")
    def connexion(self, charge):
        login = charge.get("login") if isinstance(charge.get("login"), str) else ""
        mdp = (charge.get("mot_de_passe")
               if isinstance(charge.get("mot_de_passe"), str) else "")
        origine = self.origine_client()
        res, echec = self.avec_base(
            lambda c: auth_locale.connecter(c, login, mdp, origine))
        if echec:
            self.log_message("connexion refusee : %s depuis %s",
                             pour_journal(login), origine)
            return
        jeton, compte = res
        self.log_message("connexion : %s depuis %s", pour_journal(compte["login"]),
                         origine)
        self.envoyer(200, json.dumps({
            "connecte": True, "utilisateur": compte["login"],
            "doit_changer": compte["doit_changer"]}).encode("utf-8"),
            "application/json; charset=utf-8",
            self.poser_cookie(jeton, int(auth_locale.DUREE_MAX.total_seconds())))

    @route("POST", "/api/deconnexion", "public", mode="local")
    def deconnexion(self):
        compte, jeton = self.compte_local(), self.jeton_session()
        if compte:
            _, echec = self.avec_base(
                lambda c: auth_locale.deconnecter(c, jeton, compte["login"]))
            if echec:
                return
            self.log_message("deconnexion : %s", pour_journal(compte["login"]))
        self.envoyer(200, b'{"deconnecte": true}', "application/json; charset=utf-8",
                     self.poser_cookie("", 0))

    @route("POST", "/api/mot-de-passe", "session", corps=True, limite=2048, mode="local")
    def changer_mdp(self, charge):
        compte = self.compte_local()
        actuel, nouveau = charge.get("actuel"), charge.get("nouveau")
        if not (isinstance(actuel, str) and isinstance(nouveau, str)):
            return self.repondre(400, {"erreur": "actuel et nouveau attendus"})
        jeton = self.jeton_session()
        _, echec = self.avec_base(lambda c: auth_locale.changer_mdp(
            c, compte["login"], actuel, nouveau, jeton))
        if echec:
            return
        self.log_message("mot de passe change : %s", pour_journal(compte["login"]))
        self.repondre(200, {"change": True})

    @route("GET", "/", "public")
    @route("GET", "/index.html", "public")
    def servir_page(self):
        """Sert le schema.

        En OIDC, c'est Apache qui le sert, derriere l'authentification ; ici
        c'est utile au developpement. En mode local, c'est le service, et
        seulement a une session ouverte : la page cartographie
        l'infrastructure et porte les prix.
        """
        if not self.page:
            return self.repondre(404, {"erreur": "route inconnue"})
        if self.mode_local():
            c = self.compte_local()
            if not c or "lecture" not in c["roles"]:
                return self.rediriger("/connexion" + (
                    "?changer" if c and c["doit_changer"] else ""))
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

    @route("GET", "/api/commercial", "lecture")
    def lister_ventes(self):
        """Les ventes, toutes ou celles d'une VM."""
        q = self.requete()
        vm = (q.get("vm") or [None])[0]
        toutes = (q.get("toutes") or ["0"])[0] in ("1", "true", "oui")
        ventes, echec = self.lire(lambda c: inventory_db.lister_ventes(
            c, vm=vm, actives=not toutes))
        if echec:
            return
        peut, _ = self.peut_vendre()
        self.repondre(200, {"ventes": ventes, "peut_ecrire": peut})

    @route("POST", "/api/commercial", "commercial", corps=True)
    def enregistrer_vente(self, charge):
        """Cree ou corrige une vente. Reserve aux comptes declares."""

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

    @route("POST", "/api/commercial/resilier", "commercial", corps=True)
    def resilier_vente(self, charge):
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
    @route("POST", "/api/element", "commercial", corps=True)
    def enregistrer_element(self, charge):
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

    @route("POST", "/api/elements", "commercial", corps=True)
    def enregistrer_elements(self, charge):
        utilisateur = self.identite()
        lot, echec = self.lire(
            lambda c: inventory_db.enregistrer_elements(c, charge, utilisateur))
        if echec:
            return
        self.log_message("%d elements crees par %s sous %s %s : #%s a #%s",
                         len(lot), pour_journal(utilisateur),
                         lot[0]["parent_type"], pour_journal(lot[0]["parent_ref"]),
                         lot[0]["id"], lot[-1]["id"])
        self.repondre(201, {"enregistre": True, "elements": lot,
                            "par": utilisateur})

    @route("POST", "/api/role", "commercial", corps=True)
    def poser_role(self, charge):
        utilisateur = self.identite()
        a, echec = self.lire(
            lambda c: inventory_db.poser_role(c, charge, utilisateur))
        if echec:
            return
        self.log_message("role par %s : %s %s = %s", pour_journal(utilisateur),
                         a["cible_type"], pour_journal(a["cible_ref"]),
                         pour_journal(a["role"]))
        self.repondre(200, {"enregistre": True, "role": a, "par": utilisateur})

    @route("POST", "/api/roles", "commercial", corps=True)
    def creer_role(self, charge):
        utilisateur = self.identite()
        r, echec = self.lire(
            lambda c: inventory_db.creer_role(c, charge, utilisateur))
        if echec:
            return
        self.log_message("role ajoute au catalogue par %s : %s",
                         pour_journal(utilisateur), pour_journal(r["code"]))
        self.repondre(201, {"enregistre": True, "role": r, "par": utilisateur})

    @route("POST", "/api/element/retirer", "commercial", corps=True)
    def retirer_element(self, charge):
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

    @route("POST", "/api/surcharge", "commercial", corps=True)
    def poser_surcharge(self, charge):
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

    # -- recolte PVE : etat par hyperviseur, depot de jetons -----------------
    @route("GET", "/api/recolte", "admin")
    def recolte(self):
        """Chaque dedie du dernier snapshot : sonde ou non, joignable ou non,
        d'ou vient son jeton, et ou en est son dernier depot. Le service ne
        sait pas si un jeton est en base — il n'a pas acces a secret_store —,
        il sait ce qui a ete depose et ce que la collecte en a fait."""

        def lire(conn):
            return (inventory_db.load_latest(conn),
                    depot_scelle.etat_depots(conn),
                    depot_scelle.cle_publique(conn) is not None)

        res, echec = self.lire(lire)
        if echec:
            return
        data, depots, pret = res
        if data is None:
            return self.repondre(503, {"erreur": "aucun snapshot en base"})
        pve = self.conf.get("pve") or {}
        nommes = set((pve.get("tokens") or {}).keys())
        skip = set(pve.get("skip") or [])
        sonde_de = {p["host"]: p for p in data.get("probes") or []}
        hotes = []
        for srv in data.get("servers") or []:
            rev = srv.get("reverse") or ""
            p = sonde_de.get(rev)
            cluster = (p or {}).get("cluster") or ""
            hotes.append({
                "hote": rev, "ovh_name": srv.get("ovh_name"),
                "ignore": rev in skip or srv.get("ovh_name") in skip,
                "sonde": p is not None,
                "joignable": bool(p) and not p.get("error"),
                "erreur": (p or {}).get("error"),
                "cluster": None if cluster.startswith("standalone:") else cluster or None,
                "noeud": (p or {}).get("node"),
                "jeton_infra_yml": rev in nommes,
                "depot": depots.get(rev),
            })
        self.repondre(200, {"pret": pret, "jetons_deposes":
                            bool(pve.get("jetons_deposes")),
                            "snapshot": data.get("run_id"),
                            "genere_le": data.get("generated_at"),
                            "hotes": hotes})

    @route("POST", "/api/recolte/jeton", "admin", corps=True, limite=8192)
    def deposer_jeton(self, charge):
        utilisateur = self.identite()

        def faire(conn):
            data = inventory_db.load_latest(conn)
            connus = {x.get("reverse") for x in (data or {}).get("servers") or []}
            hotes, jeton, echeance = depot_scelle.verifier_depot(charge, connus)
            return depot_scelle.deposer(conn, hotes, jeton, utilisateur,
                                        echeance), hotes

        res, echec = self.lire(faire)
        if echec:
            return
        n, hotes = res
        # jamais le jeton : ni ici, ni dans la reponse
        self.log_message("jeton PVE depose par %s pour %d hote(s) : %s",
                         pour_journal(utilisateur), n,
                         pour_journal(", ".join(hotes)))
        self.repondre(201, {"depose": n, "hotes": hotes})

    # -- comptes locaux : onglet Comptes, admins seulement -------------------
    # Un mot de passe genere n'est rendu qu'une fois, dans la reponse a
    # l'admin qui l'a demande ; il n'entre ni au journal ni dans les logs.
    @route("GET", "/api/comptes", "admin", mode="local")
    def lister_comptes(self):
        res, echec = self.lire(auth_locale.lister_comptes)
        if echec:
            return
        self.repondre(200, {"comptes": res, "moi": self.identite(),
                            "roles": list(auth_locale.ROLES)})

    @route("POST", "/api/comptes", "admin", corps=True, limite=2048, mode="local")
    def creer_compte(self, charge):
        par = self.identite()
        roles = charge.get("roles")
        mdp, echec = self.lire(lambda c: auth_locale.creer_compte(
            c, charge.get("login"),
            ",".join(roles) if isinstance(roles, list) else roles, par))
        if echec:
            return
        login = auth_locale.verifier_login(charge.get("login"))
        self.log_message("compte local cree : %s par %s",
                         pour_journal(login), pour_journal(par))
        self.repondre(201, {"login": login, "mot_de_passe": mdp})

    @route("POST", "/api/comptes/reinitialiser", "admin", corps=True, limite=1024,
           mode="local")
    def reinitialiser_compte(self, charge):
        par = self.identite()
        mdp, echec = self.lire(lambda c: auth_locale.reinitialiser_compte(
            c, charge.get("login"), par))
        if echec:
            return
        login = auth_locale.verifier_login(charge.get("login"))
        self.log_message("compte local reinitialise : %s par %s",
                         pour_journal(login), pour_journal(par))
        self.repondre(200, {"login": login, "mot_de_passe": mdp})

    @route("POST", "/api/comptes/modifier", "admin", corps=True, limite=2048,
           mode="local")
    def modifier_compte(self, charge):
        par = self.identite()
        res, echec = self.lire(lambda c: auth_locale.modifier_compte(
            c, charge.get("login"), par, roles=charge.get("roles"),
            actif=charge.get("actif")))
        if echec:
            return
        self.log_message("compte local modifie : %s par %s",
                         pour_journal(str(charge.get("login"))[:64]), pour_journal(par))
        self.repondre(200, {"modifie": res})

    # -- lectures ------------------------------------------------------------
    @route("GET", "/api/rentabilite", "lecture")
    def rentabilite(self):
        """L'arbre de rentabilite : cluster > serveur > VM > elements."""
        res, echec = self.lire(inventory_db.rentabilite)
        if echec:
            return
        peut, _ = self.peut_vendre()
        res["peut_ecrire"] = peut
        self.repondre(200, res)

    @route("GET", "/api/export.csv", "lecture")
    def exporter_csv(self):
        """Le meme arbre, a plat. Telecharge, jamais affiche dans l'onglet."""
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
    @route("GET", "/api/vues", "lecture")
    def lister_vues(self):
        moi = self.identite()
        vues, echec = self.lire(lambda c: inventory_db.lister_vues(c, moi))
        if echec:
            return
        admin = "admin" in self.roles()
        for v in vues:
            v["modifiable"] = admin or v["cree_par"] == moi
        self.repondre(200, {"vues": vues})

    @route("POST", "/api/vues", "lecture", corps=True)
    def enregistrer_vue(self, charge):
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

    @route("POST", "/api/vues/retirer", "lecture", corps=True)
    def retirer_vue(self, charge):
        moi, admin = self.identite(), "admin" in self.roles()
        _, echec = self.lire(lambda c: inventory_db.retirer_vue(
            c, charge.get("id"), moi, admin))
        if echec:
            return
        self.log_message("vue retiree par %s : #%s", pour_journal(moi),
                         pour_journal(charge.get("id")))
        self.repondre(200, {"retiree": True})

    @route("GET", "/api/moi", "identite")
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
        if self.mode_local():
            return self.repondre(200, {
                "utilisateur": utilisateur, "groupes": [],
                "roles": sorted(self.roles()),
                "peut_emettre_jeton": "admin" in self.roles(),
                "developpement": False, "deconnexion": None, "locale": True,
                "doit_changer": self.compte_local()["doit_changer"],
            })
        self.repondre(200, {
            "utilisateur": utilisateur,
            "groupes": self.groupes(),
            "roles": sorted(self.roles()),
            "peut_emettre_jeton": self.peut_emettre() in self.roles(),
            "developpement": self.en_dev(),
            "deconnexion": None if self.en_dev() else retour + "?logout=",
        })

    @route("GET", "/api/historique", "lecture")
    def historique(self):
        """Le dernier point de chaque mois, par carte, sur deux ans."""
        niveau = (self.requete().get("niveau") or ["cluster"])[0]
        if niveau not in ("parc", "cluster", "serveur"):
            return self.repondre(400, {"erreur": "niveau : parc, cluster ou serveur"})
        aujourd_hui = dt.date.today()
        depuis = aujourd_hui.replace(day=1, year=aujourd_hui.year - 2)
        lignes, echec = self.lire(
            lambda c: inventory_db.lire_historique(c, depuis, niveau))
        if echec:
            return
        self.repondre(200, {"niveau": niveau,
                            "series": capacite.series_mensuelles(lignes, niveau)})

    @route("GET", "/api/projection", "lecture")
    def projection(self):
        """La projection de capacite de chaque cluster, la plus urgente d'abord."""
        aujourd_hui = dt.date.today()
        lignes, echec = self.lire(lambda c: inventory_db.lire_historique(
            c, aujourd_hui - capacite.FENETRE, "cluster"))
        if echec:
            return
        self.repondre(200, {
            "jour": aujourd_hui.isoformat(),
            "fenetre_jours": capacite.FENETRE.days,
            "seuils": {"ram_tendue": capacite.RAM_TENDUE,
                       "vcpu_par_thread": capacite.VCPU_PAR_THREAD_MAX},
            "projections": capacite.projections(lignes, aujourd_hui)})

    @route("GET", "/api/journal/integrite", "admin")
    def integrite_journal(self):
        """La chaine du journal tient-elle ? Pour la page, et la supervision."""
        res, echec = self.lire(inventory_db.verifier_journal)
        if echec:
            return
        if not res["ok"]:
            self.log_message("journal des saisies rompu a la ligne %s : %s",
                             res["rupture"]["n"], res["rupture"]["motif"])
        self.repondre(200, res)

    @route("GET", "/api/journal", "lecture")
    def journal(self):
        lignes, echec = self.lire(inventory_db.lire_journal)
        if echec:
            return
        self.repondre(200, {"journal": lignes})

    @route("GET", "/api/sante", "public")
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
                version = inventory_db.version_schema(conn)
            finally:
                conn.close()
        except inventory_db.DbError as exc:
            self.log_message("sante degradee : %s", pour_journal(exc))
            return self.repondre(503, {"etat": "degrade",
                                       "erreur": "base indisponible"})
        attendue = inventory_db.version_attendue()
        if version < attendue:
            # le code attend des tables que la base n'a pas encore
            self.log_message("schema v%s, le code attend v%s : inventory_cli.py "
                             "--migrer", version, attendue)
            return self.repondre(503, {"etat": "degrade",
                                       "erreur": "schema en retard sur le code"})
        if data is None:
            return self.repondre(503, {"etat": "degrade",
                                       "erreur": "aucun snapshot en base"})
        age = int(dt.datetime.now().timestamp() - data["generated_at"])
        self.repondre(200, {
            "etat": "ok", "snapshot": data["run_id"], "age_s": age,
            "serveurs": len(data["servers"]), "invites": len(data["guests"]),
        })

    @route("GET", "/api/inventaire", "public")
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
    auth = (conf.get("web") or {}).get("auth", "oidc")
    if auth not in MODES_AUTH:
        print("web.auth inconnu : {} (attendu : {})".format(
            auth, ", ".join(MODES_AUTH)), file=sys.stderr)
        return 2
    if auth == "local" and not args.page:
        print("web.auth: local demande --page : c'est le service qui sert la "
              "carte, derriere la session", file=sys.stderr)
        return 2
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
    if args.dev_user and auth == "local":
        print("--dev-user ignore : en mode local, seule une session ouvre "
              "le service", file=sys.stderr)
    elif args.dev_user:
        print("MODE DEVELOPPEMENT : toute requete est vue comme "
              "'{}'".format(args.dev_user), file=sys.stderr)
    print("authentification : {}".format(auth), file=sys.stderr)
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        pass
    serveur.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
