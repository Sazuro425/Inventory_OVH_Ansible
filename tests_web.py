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
