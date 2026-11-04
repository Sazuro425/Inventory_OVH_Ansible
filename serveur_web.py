#!/usr/bin/env python3
"""
Service web de l'inventaire : emet les jetons et sert l'inventaire aux postes.

    python serveur_web.py                      ecoute sur 127.0.0.1:8080
    python serveur_web.py --dev-user evan      sans Apache devant, pour tester

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
    GET  /api/rentabilite cout OVH contre ventes, par cluster (identite requise)
    GET  /api/commercial  les ventes, ?vm=nom pour une seule
    POST /api/commercial  cree ou corrige une vente (role commercial)
    POST /api/commercial/resilier   passe un contrat a inactif

Le role commercial se declare dans infra.yml, sous web: soit une liste de
comptes (commerciaux:), soit un groupe OIDC (groupe_commercial:) qu'Apache
transmet dans X-Remote-Groups. Tant que ni l'un ni l'autre n'est renseigne,
aucune ecriture n'est acceptee.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import infra_inventory as orch  # noqa: E402
import inventory_db  # noqa: E402

DUREE_JETON_MIN = 10


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
    page = None

    def log_message(self, format, *args):
        print("{} {} {}".format(
            self.address_string(), self.log_date_time_string(), format % args),
            file=sys.stderr)

    # -- utilitaires -------------------------------------------------------
    def repondre(self, code, charge):
        corps = json.dumps(charge, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corps)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corps)

    def identite(self):
        """Identite OIDC transmise par Apache, ou compte de developpement."""
        return self.headers.get("X-Remote-User") or self.dev_user

    def groupes(self):
        """Groupes OIDC, si Apache les transmet :

            RequestHeader set X-Remote-Groups %{OIDC_CLAIM_groups}s
        """
        brut = self.headers.get("X-Remote-Groups") or ""
        return [g.strip() for g in brut.replace(";", ",").split(",") if g.strip()]

    def peut_vendre(self):
        """Qui a le droit d'ecrire une vente.

        Refus par defaut : tant que rien n'est declare dans infra.yml,
        personne n'ecrit. Une interface de saisie qui s'ouvre d'elle-meme
        parce que la configuration est vide, c'est l'inverse de ce qu'on veut.

        En --dev-user, tout passe : l'option annonce deja qu'elle tient
        n'importe quelle requete pour authentifiee.
        """
        utilisateur = self.identite()
        if not utilisateur:
            return False, "identite absente"
        if self.dev_user and not self.headers.get("X-Remote-User"):
            return True, None

        web = self.conf.get("web") or {}
        comptes = [c.lower() for c in (web.get("commerciaux") or [])]
        groupe = (web.get("groupe_commercial") or "").lower()

        if comptes and utilisateur.lower() in comptes:
            return True, None
        if groupe and groupe in [g.lower() for g in self.groupes()]:
            return True, None
        if not comptes and not groupe:
            return False, ("aucun commercial declare : renseigne "
                           "web.commerciaux ou web.groupe_commercial "
                           "dans infra.yml")
        return False, "{} n'a pas le droit d'enregistrer une vente".format(
            utilisateur)

    def corps_json(self, limite=16384):
        """Lit le corps de la requete.

        La limite evite qu'un Content-Length fantaisiste fasse grossir le
        processus : on n'alloue jamais plus que ce qu'une vente pese.
        """
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

    # -- routes ------------------------------------------------------------
    def do_POST(self):
        route = self.path.split("?")[0].rstrip("/")
        if route == "/api/commercial":
            return self.enregistrer_vente()
        if route == "/api/commercial/resilier":
            return self.resilier_vente()
        if route != "/api/token":
            return self.repondre(404, {"erreur": "route inconnue"})
        utilisateur = self.identite()
        if not utilisateur:
            return self.repondre(
                401, {"erreur": "identite absente : ce service doit etre "
                                "place derriere une authentification"})
        try:
            conn = self.base()
            jeton, duree = emettre_jeton(conn, utilisateur)
            conn.close()
        except inventory_db.DbError as exc:
            return self.repondre(503, {"erreur": str(exc)})
        self.log_message("jeton emis pour %s", utilisateur)
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
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(corps)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corps)

    # -- volet commercial --------------------------------------------------
    def requete(self):
        return parse_qs(urlparse(self.path).query)

    def lister_ventes(self):
        """Les ventes, toutes ou celles d'une VM. Lecture : identite suffit."""
        if not self.identite():
            return self.repondre(401, {"erreur": "identite absente"})
        q = self.requete()
        vm = (q.get("vm") or [None])[0]
        toutes = (q.get("toutes") or ["0"])[0] in ("1", "true", "oui")
        try:
            conn = self.base()
            ventes = inventory_db.lister_ventes(conn, vm=vm, actives=not toutes)
            conn.close()
        except inventory_db.DbError as exc:
            return self.repondre(503, {"erreur": str(exc)})
        peut, _ = self.peut_vendre()
        self.repondre(200, {"ventes": ventes, "peut_ecrire": peut})

    def enregistrer_vente(self):
        """Cree ou corrige une vente. Reserve aux comptes declares."""
        peut, motif = self.peut_vendre()
        if not peut:
            code = 401 if motif == "identite absente" else 403
            return self.repondre(code, {"erreur": motif})

        charge, erreur = self.corps_json()
        if erreur:
            return self.repondre(400, {"erreur": erreur})

        # On controle la saisie avant d'ouvrir la base : une ligne refusee ne
        # doit pas couter une connexion, et le commercial doit lire "prix
        # negatif" plutot qu'une erreur de connexion qui ne le concerne pas.
        try:
            inventory_db.verifier_vente(charge)
        except inventory_db.VenteInvalide as exc:
            return self.repondre(422, {"erreur": str(exc)})

        utilisateur = self.identite()
        try:
            conn = self.base()
            try:
                vente = inventory_db.enregistrer_vente(conn, charge, utilisateur)
            finally:
                conn.close()
        except inventory_db.VenteInvalide as exc:
            return self.repondre(422, {"erreur": str(exc)})
        except inventory_db.DbError as exc:
            return self.repondre(503, {"erreur": str(exc)})

        self.log_message("vente %s par %s : %s -> %.2f %s/%s",
                         "creee" if vente["cree"] else "corrigee", utilisateur,
                         vente["vm_name"], vente["prix_vente"],
                         vente["devise"], vente["periode"])
        self.repondre(201 if vente["cree"] else 200,
                      {"enregistre": True, "cree": vente["cree"],
                       "vente": {k: v for k, v in vente.items()
                                 if k != "cree"},
                       "par": utilisateur})

    def resilier_vente(self):
        peut, motif = self.peut_vendre()
        if not peut:
            code = 401 if motif == "identite absente" else 403
            return self.repondre(code, {"erreur": motif})

        charge, erreur = self.corps_json()
        if erreur:
            return self.repondre(400, {"erreur": erreur})

        utilisateur = self.identite()
        try:
            conn = self.base()
            try:
                touche = inventory_db.desactiver_vente(
                    conn, charge.get("contrat"), utilisateur)
            finally:
                conn.close()
        except inventory_db.VenteInvalide as exc:
            return self.repondre(422, {"erreur": str(exc)})
        except inventory_db.DbError as exc:
            return self.repondre(503, {"erreur": str(exc)})

        if not touche:
            return self.repondre(404, {"erreur": "aucun contrat actif sous ce "
                                                 "numero"})
        self.log_message("contrat resilie par %s : %s", utilisateur,
                        charge.get("contrat"))
        self.repondre(200, {"resilie": True, "par": utilisateur})

    def rentabilite(self):
        """Cout OVH contre ventes : par cluster, par serveur, et en tout."""
        if not self.identite():
            return self.repondre(401, {"erreur": "identite absente"})
        try:
            conn = self.base()
            res = inventory_db.rentabilite(conn)
            conn.close()
        except inventory_db.DbError as exc:
            return self.repondre(503, {"erreur": str(exc)})
        peut, _ = self.peut_vendre()
        res["peut_ecrire"] = peut
        self.repondre(200, res)

    def sante(self):
        try:
            conn = self.base()
            data = inventory_db.load_latest(conn)
            conn.close()
        except inventory_db.DbError as exc:
            return self.repondre(503, {"etat": "degrade", "erreur": str(exc)})
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
            emetteur, motif = consommer_jeton(
                conn, jeton, self.client_address[0])
            if motif:
                conn.close()
                self.log_message("jeton refuse : %s", motif)
                return self.repondre(403, {"erreur": motif})
            data = inventory_db.load_latest(conn)
            conn.close()
        except inventory_db.DbError as exc:
            return self.repondre(503, {"erreur": str(exc)})
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
    args = ap.parse_args()

    conf = orch.load_config(args.config)
    Handler.conf = conf
    Handler.dev_user = args.dev_user
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
