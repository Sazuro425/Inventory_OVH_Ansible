#!/usr/bin/env python3
"""
Stockage chiffre des secrets en base — chiffrement d'enveloppe.

La cle maitresse reste protegee par systemd-creds ; seules les valeurs
chiffrees vivent en base. Les deux elements doivent se rencontrer pour
qu'un secret soit lisible : un vidage de la base ne donne rien, et un
disque recupere sans le TPM ne donne rien non plus.

Ce module ne porte **pas** le mot de passe de la base, ni la cle maitresse
elle-meme : il faut le premier pour atteindre la base, et la seconde est
tout l'interet du montage. Ces deux secrets d'amorcage restent dans
systemd-creds, lus par secrets_infra. Tout le reste — les jetons PVE, les
clefs OVH — passe en base.

Ordre d'implementation : les quatre fonctions de chiffrement d'abord, qui
se testent sans base ni systemd (voir tests_secret_store.py). La couche
SQL ensuite, une fois le contrat crypto verrouille.
"""

from __future__ import annotations

import base64
import binascii
import os
import secrets

import secrets_infra
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Version de la cle maitresse en service. Stockee sur chaque ligne afin que
# --rotate-key puisse rechiffrer secret par secret : un basculement atomique
# de trente secrets ne se reprend pas s'il casse au milieu.
VERSION_CLE_COURANTE = 1

# AES-GCM : 32 octets de clef, 12 octets de nonce. Le nonce de 12 octets est
# la taille pour laquelle le mode est specifie ; une autre taille passe par
# une derivation interne et perd cette garantie.
TAILLE_CLE = 32
TAILLE_NONCE = 12


class SecretErreur(RuntimeError):
    """Secret illisible, absent, ou refuse a l'authentification."""


# --------------------------------------------------------------------------
# Couche chiffrement — aucune dependance a la base, testable seule
# --------------------------------------------------------------------------

def charger_cle_maitresse():

    brut = secrets_infra.lire_secret("master_key", "INVENTAIRE_MASTER_KEY")
    if brut is None:
        raise SecretErreur("cle maitresse absente")
    try:
        cle = base64.b64decode(brut.strip(), validate=True)
    except (binascii.Error, ValueError):
        # sans ce garde-fou, une clef mal recopiee remonte en binascii.Error
        # depuis les entrailles de la pile, loin de sa cause
        raise SecretErreur("cle maitresse illisible : base64 invalide") from None
    if len(cle) != TAILLE_CLE:
        raise SecretErreur("cle maitresse de longueur incorrecte")
    return cle


def chiffrer(cle, nom, valeur):

    nonce = os.urandom(TAILLE_NONCE)
    chiffre = AESGCM(cle).encrypt(nonce, valeur.encode(), nom.encode())
    return nonce, chiffre
    


def dechiffrer(cle, nom, nonce, chiffre):
    try:
        return AESGCM(cle).decrypt(nonce, chiffre, nom.encode()).decode()
    except InvalidTag:
        # InvalidTag seulement : une clef de mauvaise taille leve ValueError
        # et un nonce a None leve TypeError. Les convertir ici ferait passer
        # un bug d'appel pour un probleme de clef.
        raise SecretErreur("dechiffrement refuse : " + nom) from None



def generer_cle():
    key = secrets.token_bytes(32) 
    return base64.b64encode(key).decode()


# --------------------------------------------------------------------------
# Couche base — a ecrire apres que les tests de chiffrement passent
# --------------------------------------------------------------------------

def lire(conn, nom, cle=None):
    if cle is None:
        cle = charger_cle_maitresse()
    with conn.cursor() as cur:
        # ORDER BY : deux versions actives ne devraient pas coexister, mais
        # une ecriture interrompue entre l'INSERT et l'UPDATE en laisserait
        # deux. Sans tri, on lirait une ligne au hasard — donc peut-etre
        # l'ancien jeton, sans que rien ne le signale.
        cur.execute(
            "SELECT version, nonce, chiffre FROM secret_store "
            "WHERE nom = %s AND actif = 1 "
            "ORDER BY version DESC LIMIT 1",
            (nom,))
        ligne = cur.fetchone()
    if ligne is None:
        raise SecretErreur("secret absent : " + nom)
    # DictCursor : les lignes sont des dictionnaires, pas des tuples
    return dechiffrer(cle, nom, ligne["nonce"], ligne["chiffre"])


def ecrire(conn, nom, valeur, par, cle=None, cle_version=None):
    if cle is None:
        cle = charger_cle_maitresse()
    if cle_version is None:
        cle_version = VERSION_CLE_COURANTE
    nonce, chiffre = chiffrer(cle, nom, valeur)
    # La connexion est ouverte en autocommit=False : la transaction est deja
    # en cours, il n'y a qu'a la conclure.
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM secret_store "
                "WHERE nom = %s",
                (nom,))
            v = cur.fetchone()["v"] + 1
            # Si deux ecritures concurrentes calculaient le meme v, la clef
            # primaire (nom, version) en refuserait une : l'echec est franc
            # plutot qu'un ecrasement silencieux.
            cur.execute(
                "INSERT INTO secret_store "
                "(nom, version, nonce, chiffre, cle_version, cree_le, "
                " cree_par, actif) "
                "VALUES (%s, %s, %s, %s, %s, NOW(), %s, 1)",
                (nom, v, nonce, chiffre, cle_version, par))
            cur.execute(
                "UPDATE secret_store SET actif = 0 "
                "WHERE nom = %s AND version < %s",
                (nom, v))
        conn.commit()
    except Exception:
        # sans rollback, la connexion garde une transaction ouverte et la
        # prochaine lecture verrait un etat a moitie ecrit
        conn.rollback()
        raise
    return v


def lister(conn):
    if conn is None:
        raise SecretErreur("connexion absente")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT nom, version, cle_version, cree_le, cree_par "
            "FROM secret_store WHERE actif = 1 ORDER BY nom")
        return cur.fetchall()


def resoudre(valeur, conn=None, cle=None):
    """Developpe une reference de configuration, `db:` incluse.

    `db:nom` lit un secret en base ; `cred:` et `env:` sont delegues a
    secrets_infra. Toute autre valeur est rendue telle quelle.

    Enveloppe le resolveur de secrets_infra plutot que de l'etendre : ce
    module-la doit rester sans dependance, puisque c'est lui qui lit le mot
    de passe de la base et la cle maitresse.
    """
    if not isinstance(valeur, str):
        # la configuration porte aussi des entiers : un port, un TTL
        return valeur
    if valeur.startswith("db:"):
        if conn is None:
            raise SecretErreur("reference db: sans connexion")
        return lire(conn, valeur[3:], cle)
    return secrets_infra.resoudre(valeur)
