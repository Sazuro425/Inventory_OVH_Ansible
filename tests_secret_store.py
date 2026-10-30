#!/usr/bin/env python3
"""
Contrat de la couche de chiffrement de secret_store.

    python3 tests_secret_store.py

Aucune base, aucun systemd, aucune API : ces tests tournent sur un poste
nu. Ils definissent ce que le chiffrement doit garantir, et pas seulement
qu'il fonctionne dans le cas nominal — un chiffrement qui fait l'aller-
retour mais accepte un chiffre deplace d'une autre ligne est inutile.

Ils echouent tant que secret_store n'est pas implemente, ce qui est le
comportement attendu : ils sont le cahier des charges.
"""

from __future__ import annotations

import base64
import os
import sys
import traceback

import secret_store
from secret_store import SecretErreur

CLE = b"\x01" * 32          # clef de test, deliberement reconnaissable
AUTRE_CLE = b"\x02" * 32


# --------------------------------------------------------------------------
# Harnais minimal — le projet n'a pas de framework de test
# --------------------------------------------------------------------------

CAS = []


def test(libelle):
    def decorateur(fn):
        CAS.append((libelle, fn))
        return fn
    return decorateur


def doit_lever(exception, fn, *a, **kw):
    """Verifie que l'appel leve bien `exception`, et pas autre chose."""
    try:
        resultat = fn(*a, **kw)
    except exception:
        return
    except NotImplementedError:
        raise
    except Exception as exc:
        raise AssertionError(
            "attendu {}, obtenu {} : {}".format(
                exception.__name__, type(exc).__name__, exc))
    raise AssertionError(
        "attendu {}, aucune exception levee (rendu : {!r})".format(
            exception.__name__, resultat))


# --------------------------------------------------------------------------
# Aller-retour
# --------------------------------------------------------------------------

@test("une valeur chiffree puis dechiffree rend la valeur d'origine")
def _():
    nonce, chiffre = secret_store.chiffrer(CLE, "pve_token", "root@pam!ro=abc123")
    clair = secret_store.dechiffrer(CLE, "pve_token", nonce, chiffre)
    assert clair == "root@pam!ro=abc123", clair


@test("les caracteres non ASCII et les espaces internes sont preserves")
def _():
    # un jeton ne contient pas d'accent, mais un commentaire ou un mot de
    # passe de base peut en contenir : l'encodage ne doit rien abimer
    valeur = "mot de passe a accents : ete 2026 — ok"
    nonce, chiffre = secret_store.chiffrer(CLE, "db_password", valeur)
    assert secret_store.dechiffrer(CLE, "db_password", nonce, chiffre) == valeur


@test("une valeur vide reste distincte d'une valeur absente")
def _():
    nonce, chiffre = secret_store.chiffrer(CLE, "pve_token", "")
    assert secret_store.dechiffrer(CLE, "pve_token", nonce, chiffre) == ""


# --------------------------------------------------------------------------
# L'aad lie le chiffre a sa ligne — le test qui compte le plus
# --------------------------------------------------------------------------

@test("un chiffre deplace vers un autre nom est refuse")
def _():
    # scenario : acces en ecriture a la base, le chiffre de db_password est
    # recopie dans la ligne pve_token. Sans aad, le dechiffrement reussit et
    # la collecte presente le mot de passe de la base a un hyperviseur.
    nonce, chiffre = secret_store.chiffrer(CLE, "db_password", "s3cret-de-la-base")
    doit_lever(SecretErreur,
               secret_store.dechiffrer, CLE, "pve_token", nonce, chiffre)


@test("le nom exact est exige, pas un prefixe ni une variante de casse")
def _():
    nonce, chiffre = secret_store.chiffrer(CLE, "pve_token", "v")
    for faux in ("pve_token ", "PVE_TOKEN", "pve_tok", "pve_token_2"):
        doit_lever(SecretErreur,
                   secret_store.dechiffrer, CLE, faux, nonce, chiffre)


# --------------------------------------------------------------------------
# Nonce : jamais reutilise
# --------------------------------------------------------------------------

@test("deux chiffrements de la meme valeur donnent deux nonces differents")
def _():
    # Si le nonce etait un jour derive du nom du secret ou d'un compteur,
    # deux chiffrements sous la meme clef rendraient la clef
    # d'authentification recuperable : AES-GCM s'effondre entierement.
    # Ce test verrouille le tirage aleatoire contre une « optimisation ».
    vus = set()
    for _i in range(50):
        nonce, chiffre = secret_store.chiffrer(CLE, "pve_token", "identique")
        assert nonce not in vus, "nonce reutilise apres {} tirages".format(len(vus))
        vus.add(nonce)


@test("le nonce fait 12 octets")
def _():
    nonce, _ = secret_store.chiffrer(CLE, "pve_token", "v")
    assert len(nonce) == 12, len(nonce)


@test("deux chiffrements de la meme valeur donnent deux chiffres differents")
def _():
    _, a = secret_store.chiffrer(CLE, "pve_token", "identique")
    _, b = secret_store.chiffrer(CLE, "pve_token", "identique")
    assert a != b, "chiffre deterministe : le nonce ne varie pas"


# --------------------------------------------------------------------------
# Authentification : clef fausse, chiffre altere
# --------------------------------------------------------------------------

@test("une clef qui ne correspond pas est refusee")
def _():
    nonce, chiffre = secret_store.chiffrer(CLE, "pve_token", "v")
    doit_lever(SecretErreur,
               secret_store.dechiffrer, AUTRE_CLE, "pve_token", nonce, chiffre)


@test("un chiffre altere d'un seul octet est refuse")
def _():
    nonce, chiffre = secret_store.chiffrer(CLE, "pve_token", "root@pam!ro=abc")
    for position in (0, len(chiffre) // 2, len(chiffre) - 1):
        altere = bytearray(chiffre)
        altere[position] ^= 0x01
        doit_lever(SecretErreur, secret_store.dechiffrer,
                   CLE, "pve_token", nonce, bytes(altere))


@test("un nonce altere est refuse")
def _():
    nonce, chiffre = secret_store.chiffrer(CLE, "pve_token", "v")
    altere = bytearray(nonce)
    altere[0] ^= 0x01
    doit_lever(SecretErreur,
               secret_store.dechiffrer, CLE, "pve_token", bytes(altere), chiffre)


# --------------------------------------------------------------------------
# Chargement de la cle maitresse
# --------------------------------------------------------------------------

@test("une cle maitresse absente leve SecretErreur")
def _():
    with env_sans("INVENTAIRE_MASTER_KEY", "CREDENTIALS_DIRECTORY"):
        doit_lever(SecretErreur, secret_store.charger_cle_maitresse)


@test("une cle maitresse de mauvaise taille leve SecretErreur")
def _():
    trop_courte = base64.b64encode(b"\x00" * 16).decode()
    with env_avec(INVENTAIRE_MASTER_KEY=trop_courte):
        doit_lever(SecretErreur, secret_store.charger_cle_maitresse)


@test("une cle maitresse valide est chargee et fait 32 octets")
def _():
    attendue = os.urandom(32)
    with env_avec(INVENTAIRE_MASTER_KEY=base64.b64encode(attendue).decode()):
        cle = secret_store.charger_cle_maitresse()
    assert cle == attendue
    assert len(cle) == 32


@test("une cle maitresse valide supporte un saut de ligne de fin")
def _():
    # systemd-creds encrypt depuis un echo ajoute un \n : lire_secret le
    # retire deja, mais le decodage base64 doit y survivre de toute facon
    attendue = os.urandom(32)
    with env_avec(INVENTAIRE_MASTER_KEY=base64.b64encode(attendue).decode() + "\n"):
        assert secret_store.charger_cle_maitresse() == attendue


@test("generer_cle rend 32 octets en base64, differents a chaque appel")
def _():
    a = secret_store.generer_cle()
    b = secret_store.generer_cle()
    assert a != b, "deux cles identiques"
    assert len(base64.b64decode(a)) == 32, len(base64.b64decode(a))


# --------------------------------------------------------------------------
# Utilitaires d'environnement
# --------------------------------------------------------------------------

class env_avec:
    """Pose des variables d'environnement le temps du bloc."""

    def __init__(self, **variables):
        self.variables = variables
        self.avant = {}

    def __enter__(self):
        for nom, valeur in self.variables.items():
            self.avant[nom] = os.environ.get(nom)
            os.environ[nom] = valeur
        # un repertoire de credentials residuel prendrait la priorite
        self.avant["CREDENTIALS_DIRECTORY"] = os.environ.pop(
            "CREDENTIALS_DIRECTORY", None)
        return self

    def __exit__(self, *_):
        for nom, valeur in self.avant.items():
            if valeur is None:
                os.environ.pop(nom, None)
            else:
                os.environ[nom] = valeur
        return False


class env_sans:
    """Retire des variables d'environnement le temps du bloc."""

    def __init__(self, *noms):
        self.noms = noms
        self.avant = {}

    def __enter__(self):
        for nom in self.noms:
            self.avant[nom] = os.environ.pop(nom, None)
        return self

    def __exit__(self, *_):
        for nom, valeur in self.avant.items():
            if valeur is not None:
                os.environ[nom] = valeur
        return False


# --------------------------------------------------------------------------

def main():
    reussis, echecs, a_faire = 0, [], 0
    for libelle, fn in CAS:
        try:
            fn()
        except NotImplementedError:
            a_faire += 1
            print("  [ A FAIRE ] {}".format(libelle))
        except Exception:
            echecs.append((libelle, traceback.format_exc()))
            print("  [  ECHEC  ] {}".format(libelle))
        else:
            reussis += 1
            print("  [   OK    ] {}".format(libelle))

    print()
    print("{} reussis, {} echecs, {} en attente d'implementation".format(
        reussis, len(echecs), a_faire))

    for libelle, trace in echecs:
        print()
        print("--- {} ---".format(libelle))
        print(trace.rstrip())

    return 1 if echecs else 0


if __name__ == "__main__":
    sys.exit(main())
