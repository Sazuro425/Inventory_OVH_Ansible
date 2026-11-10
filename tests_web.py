#!/usr/bin/env python3
"""
Contrat des controles d'acces du service web.

    python3 tests_web.py

Ni socket ni base : une requete se resume ici a ses en-tetes, et ce sont eux
qu'Apache pose apres l'OIDC. Ces tests fixent qui lit, qui ecrit, qui emet un
jeton, et qu'une requete d'un autre site n'ecrit rien.
"""

from __future__ import annotations

import sys
import traceback

import serveur_web as web

CAS = []


def test(libelle):
    def decorateur(fn):
        CAS.append((libelle, fn))
        return fn
    return decorateur


def egal(obtenu, attendu, quoi=""):
    if obtenu != attendu:
        raise AssertionError("{} : attendu {!r}, obtenu {!r}".format(
            quoi or "valeur", attendu, obtenu))


CONF = {"web": {"groupe_lecture": "inventaire-lecture",
                "groupe_commercial": "inventaire-commercial",
                "groupe_admin": "inventaire-admin",
                "commerciaux": ["compta"]}}


def requete(entetes=None, conf=CONF, dev_user=None, dev_groupes=None):
    """Un Handler sans socket : seuls les en-tetes et la conf comptent."""
    h = web.Handler.__new__(web.Handler)
    h.headers = dict(entetes or {})
    h.conf = conf
    h.dev_user = dev_user
    h.dev_groupes = dev_groupes
    return h


def oidc(user, groupes=""):
    return requete({"X-Remote-User": user, "X-Remote-Groups": groupes})


# --------------------------------------------------------------------------

@test("sans identite, aucun role")
def _():
    egal(requete().roles(), set())


@test("lecteur : lit, n'ecrit pas, n'emet pas de jeton")
def _():
    r = oidc("alice", "inventaire-lecture,autre")
    egal(r.roles(), {"lecture"})
    egal(r.peut_vendre()[0], False)
    egal(r.peut_emettre(), "admin", "un admin est declare")


@test("commercial par groupe : lit et ecrit")
def _():
    egal(oidc("bob", "inventaire-commercial").roles(), {"lecture", "commercial"})


@test("commercial par compte, sans groupe")
def _():
    egal(oidc("compta").roles(), {"lecture", "commercial"})


@test("authentifie mais hors des groupes : rien")
def _():
    egal(oidc("eve", "rh,compta-fournisseurs").roles(), set())


@test("les groupes se comparent sans la casse")
def _():
    egal(oidc("bob", "Inventaire-Admin").roles(), {"lecture", "admin"})


@test("un en-tete de groupes non resolu par Apache ne donne rien")
def _():
    egal(oidc("bob", "(null)").groupes(), [])


@test("sans lecture declaree, toute identite lit ; sans commercial, nul n'ecrit")
def _():
    r = requete({"X-Remote-User": "x"}, conf={"web": {}})
    egal(r.roles(), {"lecture"})
    ok, motif = r.peut_vendre()
    egal(ok, False)
    egal("aucun commercial declare" in motif, True, "motif explicite")
    egal(r.peut_emettre(), "lecture", "pas d'admin declare")


@test("--dev-user sans groupes : tous les roles")
def _():
    egal(requete(dev_user="evan").roles(), {"lecture", "commercial", "admin"})


@test("--dev-user avec groupes : les regles normales")
def _():
    egal(requete(dev_user="evan", dev_groupes=["inventaire-lecture"]).roles(),
         {"lecture"})


@test("--dev-user ne s'applique pas a une requete qui porte deja une identite")
def _():
    r = requete({"X-Remote-User": "eve", "X-Remote-Groups": ""}, dev_user="evan")
    egal(r.identite(), "eve")
    egal(r.roles(), set())


@test("garde d'origine : meme site oui, autre site non")
def _():
    egal(requete({"Sec-Fetch-Site": "same-origin"}).meme_origine(), True)
    egal(requete({"Sec-Fetch-Site": "cross-site"}).meme_origine(), False)
    egal(requete({"Sec-Fetch-Site": "same-site"}).meme_origine(), False,
         "un sous-domaine voisin n'est pas la page")
    egal(requete({"Origin": "https://inv.lan", "Host": "inv.lan"}).meme_origine(),
         True)
    egal(requete({"Origin": "https://evil.example", "Host": "inv.lan"})
         .meme_origine(), False)
    egal(requete({}).meme_origine(), True, "client hors navigateur")


# --------------------------------------------------------------------------
# Vues enregistrees : ce qui entre en base sera rejoue par la page
# --------------------------------------------------------------------------

import inventory_db as db  # noqa: E402


def refuse(fn, *args):
    try:
        fn(*args)
    except db.VenteInvalide as exc:
        return str(exc)
    raise AssertionError("aurait du etre refuse : {!r}".format(args))


@test("une vue valide est normalisee : listes triees, sans doublon")
def _():
    ident, nom, partagee, d = db.verifier_vue({"nom": " Commercial ",
        "definition": {"mode": "graphiques", "clients": ["zenith", "abc", "abc"],
                       "perte": False, "alertes": True}})
    egal((ident, nom, partagee), (None, "Commercial", True))
    egal(d, {"mode": "graphiques", "clients": ["abc", "zenith"], "alertes": True})


@test("une vue refuse un champ inconnu, un mode inconnu, un nom vide")
def _():
    refuse(db.verifier_vue, {"nom": "x", "definition": {"script": "alert(1)"}})
    refuse(db.verifier_vue, {"nom": "x", "definition": {"mode": "carte"}})
    refuse(db.verifier_vue, {"nom": " ", "definition": {}})
    refuse(db.verifier_vue, {"nom": "x", "definition": "pas un objet"})


@test("une vue refuse les caracteres de controle et les listes demesurees")
def _():
    refuse(db.verifier_vue, {"nom": "a" + chr(10) + "b", "definition": {}})
    refuse(db.verifier_vue, {"nom": "x", "definition": {"clients": ["a" + chr(0)]}})
    refuse(db.verifier_vue, {"nom": "x",
                             "definition": {"clients": [str(i) for i in range(301)]}})
    refuse(db.verifier_vue, {"nom": "x", "definition": {"tri": "roi; DROP"}})


@test("modifier la vue d'un autre est interdit (403), pas invalide (422)")
def _():
    egal(issubclass(db.SaisieInterdite, db.VenteInvalide), True)


# --------------------------------------------------------------------------
# Enfants en lot et roles
# --------------------------------------------------------------------------

LOT = {"parent_type": "vm", "parent_ref": "vm-a", "role": "Site",
       "prix_vente": "40", "periode": "mois"}


@test("un lot : un element par nom, le reste en commun")
def _():
    lot = db.verifier_lot(dict(LOT, noms=[" site1.fr", "site2.fr", "", "site3.fr ",
                                          "site4.fr"]))
    egal([e["nom"] for e in lot], ["site1.fr", "site2.fr", "site3.fr", "site4.fr"])
    egal({(e["role"], e["prix_vente"], e["parent_ref"]) for e in lot},
         {("site", 40.0, "vm-a")}, "role en minuscules, prix commun")
    egal(sum(e["prix_vente"] for e in lot), 160.0)


@test("un lot refuse un doublon, une liste vide, un nom pirate, un exces")
def _():
    refuse(db.verifier_lot, dict(LOT, noms=["a.fr", "A.fr"]))
    refuse(db.verifier_lot, dict(LOT, noms=["", "  "]))
    refuse(db.verifier_lot, dict(LOT, noms="a.fr"))
    refuse(db.verifier_lot, dict(LOT, noms=["a.fr", "b" + chr(10) + "x"]))
    refuse(db.verifier_lot, dict(LOT, noms=[str(i) for i in range(201)]))
    refuse(db.verifier_lot, dict(LOT, noms=["a.fr"], prix_vente="-1"))


@test("le code d'un role se deduit du libelle, sans accent ni espace")
def _():
    egal(db.verifier_role({"libelle": "Serveur LAMP"})["code"], "serveur-lamp")
    egal(db.verifier_role({"libelle": " Base de données "}),
         {"code": "base-de-donnees", "libelle": "Base de données"})
    egal(db.verifier_role({"libelle": "Proxy", "code": "RP"})["code"], "rp")
    refuse(db.verifier_role, {"libelle": "€€"})
    refuse(db.verifier_role, {"libelle": "x", "code": "a b"})


@test("un role se pose sur cluster, serveur ou VM ; vide le leve")
def _():
    egal(db.verifier_role_carte({"cible_type": "vm", "cible_ref": "vm-a",
                                 "role": "LAMP"})["role"], "lamp")
    egal(db.verifier_role_carte({"cible_type": "serveur", "cible_ref": "ns1",
                                 "role": ""})["role"], None)
    refuse(db.verifier_role_carte, {"cible_type": "element", "cible_ref": "1",
                                    "role": "site"})
    refuse(db.verifier_role_carte, {"cible_type": "vm", "cible_ref": "",
                                    "role": "lamp"})


@test("une vue filtre par roles")
def _():
    egal(db.verifier_vue({"nom": "LAMP", "definition": {
        "roles": ["lamp", "web"]}})[3], {"roles": ["lamp", "web"]})


# --------------------------------------------------------------------------
# Comptes locaux (web.auth: local)
# --------------------------------------------------------------------------

import datetime as dt  # noqa: E402

import auth_locale as al  # noqa: E402

MAINTENANT = dt.datetime(2026, 11, 9, 14, 0)
EMPREINTE = al.hacher("une phrase assez longue")


def compte(**k):
    return dict({"login": "admin", "empreinte": EMPREINTE, "actif": 1,
                 "echecs": 0, "bloque_jusqu_a": None, "roles": "admin,commercial",
                 "doit_changer": 0}, **k)


@test("mot de passe : scrypt sale, verifie, empreinte abimee refusee")
def _():
    egal(al.verifier("une phrase assez longue", EMPREINTE), True)
    egal(al.verifier("une phrase assez longuE", EMPREINTE), False)
    egal(al.hacher("x") != al.hacher("x"), True, "sel different a chaque fois")
    egal(al.verifier("x", "md5$abc"), False)
    egal(al.verifier("x", None), False)
    egal(len(al.generer_mdp()) >= 24, True)


@test("politique : 12 caracteres, sans l'identifiant")
def _():
    egal(al.politique("court", "admin") is not None, True)
    egal(al.politique("monadmin-2026!!", "admin") is not None, True)
    egal(al.politique("ligne" + chr(10) + "cassee-longue", "bob") is not None, True)
    egal(al.politique("cheval agrafe batterie", "admin"), None)


@test("roles d'un compte : commercial et admin emportent la lecture")
def _():
    egal(al.lire_roles("commercial"), {"commercial", "lecture"})
    egal(al.lire_roles(""), set())
    refuse(al.lire_roles, "root")
    refuse(al.verifier_login, "Admin Nouveau")


@test("connexion : bon mot de passe, compteur remis a zero")
def _():
    maj, refus = al.evaluer_connexion(compte(echecs=3), "une phrase assez longue",
                                      MAINTENANT)
    egal(refus, None)
    egal((maj["echecs"], maj["bloque_jusqu_a"]), (0, None))


@test("connexion : identifiant inconnu ou inactif, meme refus que mauvais mdp")
def _():
    _, r1 = al.evaluer_connexion(None, "x", MAINTENANT)
    _, r2 = al.evaluer_connexion(compte(), "x", MAINTENANT)
    _, r3 = al.evaluer_connexion(compte(actif=0), "une phrase assez longue",
                                 MAINTENANT)
    egal({(r.code, str(r)) for r in (r1, r2, r3)},
         {(401, "identifiant ou mot de passe incorrect")})


@test("connexion : cinq echecs bloquent quinze minutes, meme le bon mdp")
def _():
    maj, refus = al.evaluer_connexion(compte(echecs=4), "x", MAINTENANT)
    egal(maj["bloque_jusqu_a"], MAINTENANT + dt.timedelta(minutes=15))
    egal(refus.code, 401)
    bloque = compte(bloque_jusqu_a=maj["bloque_jusqu_a"])
    _, refus = al.evaluer_connexion(bloque, "une phrase assez longue",
                                    MAINTENANT + dt.timedelta(minutes=5))
    egal(refus.code, 429)
    _, refus = al.evaluer_connexion(bloque, "une phrase assez longue",
                                    MAINTENANT + dt.timedelta(minutes=16))
    egal(refus, None, "debloque une fois le delai passe")


@test("session : expiree, inactive ou revoquee ne vaut rien")
def _():
    s = {"login": "admin", "revoquee": 0, "actif": 1, "roles": "admin",
         "doit_changer": 0, "vu_le": MAINTENANT - dt.timedelta(minutes=10),
         "expire_le": MAINTENANT + dt.timedelta(hours=2)}
    egal(al.session_valide(s, MAINTENANT), True)
    egal(al.session_valide(dict(s, revoquee=1), MAINTENANT), False)
    egal(al.session_valide(dict(s, actif=0), MAINTENANT), False)
    egal(al.session_valide(dict(s, expire_le=MAINTENANT), MAINTENANT), False)
    egal(al.session_valide(dict(s, vu_le=MAINTENANT - dt.timedelta(hours=2)),
                           MAINTENANT), False, "une heure sans activite")


@test("mot de passe a changer : la session n'a aucun role")
def _():
    egal(al.compte_de_session({"login": "admin", "roles": "admin",
                               "doit_changer": 1})["roles"], set())


LOCAL = {"web": {"auth": "local", "groupe_admin": "inventaire-admin"}}


def session_locale(entetes, compte_session, dev_user=None):
    h = requete(dict(entetes, Cookie=al.COOKIE + "=abc"), conf=LOCAL,
                dev_user=dev_user)
    h._session = ("abc", compte_session)
    return h


@test("mode local : la session donne l'identite et les roles")
def _():
    h = session_locale({}, {"login": "admin", "doit_changer": False,
                            "roles": {"admin", "commercial", "lecture"}})
    egal(h.identite(), "admin")
    egal(h.roles(), {"admin", "commercial", "lecture"})
    egal(h.peut_vendre()[0], True)
    egal(h.peut_emettre(), "admin")


@test("mode local : un X-Remote-User forge ne vaut rien, --dev-user non plus")
def _():
    h = session_locale({"X-Remote-User": "admin",
                        "X-Remote-Groups": "inventaire-admin"}, None,
                       dev_user="evan")
    egal(h.identite(), None)
    egal(h.roles(), set())
    egal(h.groupes(), [])
    egal(h.en_dev(), False)


@test("mode local : mot de passe a changer, connecte mais sans role")
def _():
    h = session_locale({}, {"login": "admin", "doit_changer": True,
                            "roles": set()})
    egal(h.identite(), "admin")
    egal(h.roles(), set())
    egal(h.peut_vendre()[0], False)


# --------------------------------------------------------------------------
# Boite de depot des jetons PVE
# --------------------------------------------------------------------------

import depot_scelle as ds  # noqa: E402

JETON = "inventory@pve!readonly=0f9c2d1e-7a6b-4c3d-9e8f-1a2b3c4d5e6f"
PRIVEE, PUBLIQUE = ds.generer_paire()


@test("depot : le scelle s'ouvre avec la clef privee, pour son hote seulement")
def _():
    sc = ds.sceller(PUBLIQUE, JETON, "ns1.eu")
    egal(ds.ouvrir(PRIVEE, sc, "ns1.eu"), JETON)
    egal(JETON.encode() in sc, False, "rien en clair dans le scelle")
    autre, _ = ds.generer_paire()
    for cle, hote in ((PRIVEE, "ns2.eu"), (autre, "ns1.eu")):
        try:
            ds.ouvrir(cle, sc, hote)
        except ds.DepotErreur:
            continue
        raise AssertionError("ouvert avec la mauvaise clef ou le mauvais hote")
    abime = sc[:-1] + bytes([sc[-1] ^ 1])
    try:
        ds.ouvrir(PRIVEE, abime, "ns1.eu")
    except ds.DepotErreur:
        pass
    else:
        raise AssertionError("un scelle altere s'est ouvert")


@test("depot : jeton au format pveum, hotes connus de la collecte")
def _():
    connus = {"ns1.eu", "ns2.eu"}
    egal(ds.verifier_depot({"jeton": " " + JETON + " ",
                            "hotes": ["ns1.eu", "ns2.eu", "ns1.eu"]}, connus),
         (["ns1.eu", "ns2.eu"], JETON))
    refuse(ds.verifier_depot, {"jeton": "inventory@pve!readonly=pas-un-uuid",
                               "hotes": ["ns1.eu"]}, connus)
    refuse(ds.verifier_depot, {"jeton": JETON + chr(10) + "x",
                               "hotes": ["ns1.eu"]}, connus)
    refuse(ds.verifier_depot, {"jeton": JETON, "hotes": ["ns9.eu"]}, connus)
    refuse(ds.verifier_depot, {"jeton": JETON, "hotes": []}, connus)


@test("traitement : le dernier depot d'un hote compte, teste puis range")
def _():
    import secret_store
    depots = [
        {"id": 1, "hote": "ns1.eu", "depose_par": "evan",
         "scelle": ds.sceller(PUBLIQUE, JETON.replace("0f9c", "aaaa"), "ns1.eu")},
        {"id": 2, "hote": "ns1.eu", "depose_par": "evan",
         "scelle": ds.sceller(PUBLIQUE, JETON, "ns1.eu")},
        {"id": 3, "hote": "ns2.eu", "depose_par": "bob",
         "scelle": ds.sceller(PUBLIQUE, JETON, "ns2.eu")},
        # recopie sur la ligne d'un autre hote : ne s'ouvre pas
        {"id": 4, "hote": "ns3.eu", "depose_par": "eve",
         "scelle": ds.sceller(PUBLIQUE, JETON, "ns1.eu")},
    ]
    clos, ranges = {}, {}
    sauve = (ds.en_attente, ds._clore, secret_store.lire, secret_store.ecrire)
    ds.en_attente = lambda conn: depots
    ds._clore = lambda conn, i, etat, motif, par, hote: clos.__setitem__(i, etat)
    secret_store.lire = lambda conn, nom, cle: __import__("base64").b64encode(
        PRIVEE).decode()
    secret_store.ecrire = lambda conn, nom, val, par, cle: ranges.__setitem__(nom, val)
    try:
        testes = []

        def tester(hote, jeton):
            testes.append((hote, jeton))
            return (hote == "ns1.eu", "401 : jeton refuse par PVE")

        acc, ref = ds.traiter(None, b"k" * 32, tester,
                              secret_store.nom_secret_pve, dire=lambda *a: None)
    finally:
        ds.en_attente, ds._clore, secret_store.lire, secret_store.ecrire = sauve
    egal((acc, ref), (1, 2))
    egal(clos, {1: "remplace", 2: "accepte", 3: "refuse", 4: "refuse"})
    egal(ranges, {"pve_token_ns1_eu": JETON}, "seul le jeton teste est range")
    egal([h for h, _ in testes], ["ns1.eu", "ns2.eu"],
         "le depot illisible n'est meme pas teste")


@test("migrations : nommees NNNN_nom.sql, a partir de 0002, sans doublon")
def _():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        egal(db.migrations_disponibles(d), [])
        egal(db.version_attendue(d), 1, "sans migration, la base (schema.sql)")
        (Path(d) / "0003_b.sql").write_text("")
        (Path(d) / "0002_a.sql").write_text("")
        (Path(d) / "LISEZMOI.md").write_text("")
        egal([(v, n) for v, n, _ in db.migrations_disponibles(d)],
             [(2, "a"), (3, "b")])
        for mauvais in ("0002_double.sql", "0001_base.sql", "2_court.sql"):
            (Path(d) / mauvais).write_text("")
            try:
                db.migrations_disponibles(d)
            except db.DbError:
                pass
            else:
                raise AssertionError("aurait du refuser " + mauvais)
            (Path(d) / mauvais).unlink()
    egal(db.instructions_sql("-- note\nUSE inventory;\nSELECT 1;\n\nSELECT 2;"),
         ["SELECT 1", "SELECT 2"])


@test("collecte : infra.yml d'abord, puis le jeton depose, puis default_token")
def _():
    import infra_inventory as orch

    class Faux:
        def noms_en_base(self):
            return {"pve_token_ns2_eu"}

        def __call__(self, ref):
            return ref

    conf = {"pve": {"tokens": {"ns1.eu": "env:NOMME"}, "default_token": "env:DEF",
                    "jetons_deposes": True}, "_secrets": Faux()}
    egal(orch.token_for(conf, "ns1.eu"), "env:NOMME")
    egal(orch.token_for(conf, "ns2.eu"), "db:pve_token_ns2_eu")
    egal(orch.token_for(conf, "ns3.eu"), "env:DEF")
    conf["pve"]["jetons_deposes"] = False
    egal(orch.token_for(conf, "ns2.eu"), "env:DEF", "depots desactives")


# --------------------------------------------------------------------------

def main():
    reussis, echecs = 0, []
    for libelle, fn in CAS:
        try:
            fn()
        except Exception:
            echecs.append((libelle, traceback.format_exc()))
            print("  ECHEC  " + libelle)
        else:
            reussis += 1
            print("  ok     " + libelle)
    print()
    print("{} reussis, {} echecs".format(reussis, len(echecs)))
    for libelle, trace in echecs:
        print()
        print("--- {} ---".format(libelle))
        print(trace.rstrip())
    return 1 if echecs else 0


if __name__ == "__main__":
    sys.exit(main())
