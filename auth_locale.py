#!/usr/bin/env python3
"""
Comptes locaux : l'authentification du service tant que l'OIDC n'est pas
branche.

    infra.yml, section web :   auth: local

Un compte se cree en ligne de commande, jamais depuis la page :

    python3 inventory_cli.py --compte-local admin

Le mot de passe est tire au hasard, affiche une fois, et doit etre change a
la premiere connexion : tant qu'il ne l'est pas, le compte n'a aucun role.

Ce que ce module garantit, et que tests_web.py fixe :

- le mot de passe n'est stocke que sous forme scrypt, sel aleatoire ;
- cinq echecs de suite bloquent le compte quinze minutes, et un identifiant
  inconnu coute le meme calcul qu'un vrai, pour ne pas se trahir au temps
  de reponse ;
- la session est un jeton aleatoire dans un cookie __Host- (Secure,
  HttpOnly, SameSite=Strict) ; la base n'en garde que l'empreinte, si bien
  qu'une copie de la table ne permet de reprendre aucune session ;
- une session meurt apres une heure sans activite, huit heures au plus, ou
  a la deconnexion, et changer de mot de passe ferme toutes les autres ;
- connexions, echecs, deconnexions et changements vont au journal des
  saisies, que le compte web ne peut ni modifier ni effacer.

Le compte web lit et met a jour compte_local, il ne peut pas y creer de
compte : c'est le compte de collecte, sur la VM, qui le fait.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import re
import secrets

import inventory_db

ROLES = ("lecture", "commercial", "admin")
ECHECS_MAX = 5
BLOCAGE = dt.timedelta(minutes=15)
INACTIVITE = dt.timedelta(hours=1)
DUREE_MAX = dt.timedelta(hours=8)
LONGUEUR_MIN = 12
# Le prefixe __Host- interdit au navigateur d'accepter ce cookie s'il n'est
# pas Secure, pose par cet hote exact et sur tout le chemin : un sous-domaine
# voisin ne peut pas l'ecraser.
COOKIE = "__Host-inventaire"
# scrypt : environ 50 ms et 16 Mo par calcul, ce qui rend une liste de
# mots de passe couteuse a essayer, meme sur une copie de la table.
_N, _R, _P = 2 ** 14, 8, 1
_LOGIN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MSG_REFUS = "identifiant ou mot de passe incorrect"
# sans pymysql (poste de developpement), un tuple vide n'attrape rien
_SQL = inventory_db.pymysql.Error if inventory_db.pymysql else ()


class Refus(Exception):
    """Connexion ou changement refuse. code : le statut HTTP a rendre."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Mots de passe
# ---------------------------------------------------------------------------

def _b64(b):
    return base64.b64encode(b).decode("ascii")


def hacher(mdp):
    sel = secrets.token_bytes(16)
    h = hashlib.scrypt(mdp.encode("utf-8"), salt=sel, n=_N, r=_R, p=_P,
                       dklen=32, maxmem=64 * 1024 * 1024)
    return "scrypt${}${}${}${}${}".format(_N, _R, _P, _b64(sel), _b64(h))


def verifier(mdp, empreinte):
    try:
        algo, n, r, p, sel, h = (empreinte or "").split("$")
        if algo != "scrypt":
            return False
        attendu = base64.b64decode(h)
        calcule = hashlib.scrypt(
            (mdp or "").encode("utf-8"), salt=base64.b64decode(sel),
            n=int(n), r=int(r), p=int(p), dklen=len(attendu),
            maxmem=64 * 1024 * 1024)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(calcule, attendu)


# Un identifiant inconnu est verifie contre cette empreinte : meme calcul,
# meme duree, et la reponse ne dit pas si le compte existe.
_LEURRE = hacher(secrets.token_urlsafe(16))


def generer_mdp():
    """24 caracteres, 144 bits : a recopier une fois, puis a changer."""
    return secrets.token_urlsafe(18)


def politique(mdp, login):
    """Le motif de refus d'un nouveau mot de passe, ou None."""
    if not isinstance(mdp, str) or len(mdp) < LONGUEUR_MIN:
        return "{} caracteres au moins".format(LONGUEUR_MIN)
    if len(mdp) > 256:
        return "256 caracteres au plus"
    if any(ord(c) < 32 or ord(c) == 127 for c in mdp):
        return "pas de caractere de controle"
    if login and login.lower() in mdp.lower():
        return "le mot de passe ne doit pas contenir l'identifiant"
    return None


def verifier_login(login):
    login = (login or "").strip().lower()
    if not _LOGIN.match(login):
        raise inventory_db.VenteInvalide(
            "identifiant : minuscules, chiffres, point, tiret, 64 au plus")
    return login


def lire_roles(texte):
    """"admin,commercial" -> {"admin", "commercial", "lecture"}.

    Commercial et admin emportent la lecture, comme pour les groupes OIDC.
    """
    r = {x.strip().lower() for x in (texte or "").split(",") if x.strip()}
    inconnus = r - set(ROLES)
    if inconnus:
        raise inventory_db.VenteInvalide(
            "role inconnu : {} (attendu : {})".format(
                ", ".join(sorted(inconnus)), ", ".join(ROLES)))
    if r:
        r.add("lecture")
    return r


def empreinte_session(jeton):
    return hashlib.sha256(jeton.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Decisions, sans base : ce que tests_web.py eprouve, et ce que le banc
# d'essai reutilise tel quel
# ---------------------------------------------------------------------------

def evaluer_connexion(compte, mdp, maintenant):
    """Decide d'une tentative de connexion.

    Rend (maj, refus) : maj, les colonnes du compte a mettre a jour (ou
    None) ; refus, un Refus a lever apres avoir ecrit maj, ou None.
    """
    if compte is None or not compte.get("actif"):
        verifier(mdp, _LEURRE)
        return None, Refus(401, _MSG_REFUS)
    bloque = compte.get("bloque_jusqu_a")
    if bloque and bloque > maintenant:
        return None, Refus(429, "trop d'echecs : compte bloque jusqu'a "
                                + bloque.strftime("%H:%M"))
    if not verifier(mdp, compte["empreinte"]):
        echecs = int(compte.get("echecs") or 0) + 1
        if echecs >= ECHECS_MAX:
            return ({"echecs": 0, "bloque_jusqu_a": maintenant + BLOCAGE},
                    Refus(401, _MSG_REFUS))
        return {"echecs": echecs}, Refus(401, _MSG_REFUS)
    return ({"echecs": 0, "bloque_jusqu_a": None,
             "derniere_connexion": maintenant}, None)


def session_valide(s, maintenant):
    """Une ligne de session_locale jointe a son compte : encore valable ?"""
    return bool(s) and not s.get("revoquee") and s.get("actif", 1) \
        and s["expire_le"] > maintenant \
        and s["vu_le"] > maintenant - INACTIVITE


def compte_de_session(s):
    """Ce que le service retient d'une session : qui, et avec quels roles.

    Un mot de passe a changer ne donne aucun role : la seule chose permise
    est d'en choisir un nouveau.
    """
    doit = bool(s.get("doit_changer"))
    return {"login": s["login"], "doit_changer": doit,
            "roles": set() if doit else lire_roles(s.get("roles"))}


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

def _maj_compte(cur, login, maj):
    if maj:
        cur.execute("UPDATE compte_local SET {} WHERE login = %s".format(
            ", ".join(k + " = %s" for k in maj)), list(maj.values()) + [login])


def _public(c):
    """Un compte tel qu'il peut entrer au journal : sans l'empreinte."""
    return {k: v for k, v in (c or {}).items() if k != "empreinte"} or None


def creer_ou_reinitialiser(conn, login, roles, par):
    """Cree le compte, ou le reinitialise. Rend le mot de passe genere.

    Reinitialiser ferme toutes ses sessions et le debloque : c'est la
    procedure quand un mot de passe est perdu ou suspect.
    """
    login = verifier_login(login)
    propres = lire_roles(roles)
    if not propres:
        raise inventory_db.VenteInvalide("au moins un role")
    mdp = generer_mdp()
    texte_roles = ",".join(r for r in ROLES if r in propres)
    maintenant = dt.datetime.now()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM compte_local WHERE login = %s FOR UPDATE",
                        (login,))
            avant = cur.fetchone()
            cur.execute(
                "INSERT INTO compte_local (login, empreinte, roles, doit_changer,"
                " actif, echecs, cree_le, cree_par) "
                "VALUES (%s, %s, %s, 1, 1, 0, %s, %s) "
                "ON DUPLICATE KEY UPDATE empreinte = VALUES(empreinte), "
                "roles = VALUES(roles), doit_changer = 1, actif = 1, echecs = 0,"
                " bloque_jusqu_a = NULL, maj_le = VALUES(cree_le)",
                (login, hacher(mdp), texte_roles, maintenant, par))
            cur.execute("UPDATE session_locale SET revoquee = 1 "
                        "WHERE login = %s AND revoquee = 0", (login,))
            inventory_db.journaliser(
                cur, par, "compte local cree" if avant is None
                else "compte local reinitialise", "compte " + login,
                _public(avant), {"roles": texte_roles, "doit_changer": 1})
    except _SQL as exc:
        conn.rollback()
        raise inventory_db._traduit(exc, "la creation du compte") from exc
    conn.commit()
    return mdp


# ---------------------------------------------------------------------------
# Gestion des comptes depuis la page (onglet Comptes, admins seulement)
# ---------------------------------------------------------------------------

def _existe(conn, login):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM compte_local WHERE login = %s", (login,))
        return cur.fetchone() is not None


def lister_comptes(conn):
    """Les comptes, sans empreinte, avec leurs sessions ouvertes."""
    maintenant = dt.datetime.now()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT login, roles, actif, doit_changer, echecs, bloque_jusqu_a,"
                " derniere_connexion, cree_le, cree_par, maj_le FROM compte_local"
                " ORDER BY login")
            comptes = cur.fetchall()
            cur.execute(
                "SELECT login, COUNT(*) AS n FROM session_locale WHERE revoquee = 0"
                " AND expire_le > %s GROUP BY login", (maintenant,))
            ouvertes = {r["login"]: r["n"] for r in cur.fetchall()}
    except _SQL as exc:
        raise inventory_db._traduit(exc, "la liste des comptes") from exc
    rendu = []
    for c in comptes:
        bloque = c["bloque_jusqu_a"]
        rendu.append({
            "login": c["login"],
            "roles": [r for r in ROLES if r in lire_roles(c["roles"])],
            "actif": bool(c["actif"]),
            "doit_changer": bool(c["doit_changer"]),
            "bloque_jusqu_a": bloque.isoformat(sep=" ", timespec="minutes")
            if bloque and bloque > maintenant else None,
            "derniere_connexion": c["derniere_connexion"].isoformat(
                sep=" ", timespec="minutes") if c["derniere_connexion"] else None,
            "cree_le": c["cree_le"].isoformat(sep=" ", timespec="minutes"),
            "cree_par": c["cree_par"],
            "sessions": ouvertes.get(c["login"], 0),
        })
    return rendu


def creer_compte(conn, login, roles, par):
    """Un compte neuf ; refuse un identifiant deja pris (une reinitialisation
    se demande explicitement). Rend le mot de passe genere."""
    login = verifier_login(login)
    if _existe(conn, login):
        raise inventory_db.VenteInvalide(
            "le compte {} existe deja : reinitialise-le plutot".format(login))
    # la page envoie une liste, la ligne de commande un texte
    if isinstance(roles, list):
        roles = ",".join(r for r in roles if isinstance(r, str))
    return creer_ou_reinitialiser(conn, login, roles, par)


def reinitialiser_compte(conn, login, par):
    """Nouveau mot de passe, sessions fermees, compte debloque ; les roles
    restent. Rend le mot de passe genere."""
    login = verifier_login(login)
    with conn.cursor() as cur:
        cur.execute("SELECT roles FROM compte_local WHERE login = %s", (login,))
        c = cur.fetchone()
    if c is None:
        raise inventory_db.VenteInvalide("compte inconnu : {}".format(login))
    return creer_ou_reinitialiser(conn, login, c["roles"], par)


def modifier_compte(conn, login, par, roles=None, actif=None):
    """Change les roles ou active / desactive un compte.

    Deux garde-fous : un admin ne se retire ni son role ni son acces (il se
    fermerait la porte), et il reste toujours un admin actif. Un compte
    desactive perd ses sessions sur-le-champ.
    """
    login = verifier_login(login)
    nouveaux = None
    if roles is not None:
        nouveaux = lire_roles(",".join(roles) if isinstance(roles, list) else roles)
        if not nouveaux:
            raise inventory_db.VenteInvalide("au moins un role")
    if actif is not None and not isinstance(actif, bool):
        raise inventory_db.VenteInvalide("actif : vrai ou faux")
    if login == par and ((nouveaux is not None and "admin" not in nouveaux)
                         or actif is False):
        raise inventory_db.VenteInvalide(
            "tu ne peux pas te retirer le role admin ni te desactiver toi-meme")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM compte_local WHERE login = %s FOR UPDATE",
                        (login,))
            avant = cur.fetchone()
            if avant is None:
                raise inventory_db.VenteInvalide("compte inconnu : {}".format(login))
            maj = {}
            if nouveaux is not None:
                maj["roles"] = ",".join(r for r in ROLES if r in nouveaux)
            if actif is not None:
                maj["actif"] = 1 if actif else 0
            if not maj:
                return _public(avant)
            # il doit rester un admin actif une fois la modification faite
            restera_admin = ("admin" in lire_roles(maj.get("roles", avant["roles"]))
                             and maj.get("actif", avant["actif"]))
            if not restera_admin:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM compte_local WHERE actif = 1 AND"
                    " login <> %s AND FIND_IN_SET('admin', roles) FOR UPDATE",
                    (login,))
                if not cur.fetchone()["n"]:
                    raise inventory_db.VenteInvalide(
                        "c'est le dernier admin actif : donne d'abord le role "
                        "admin a un autre compte")
            maj["maj_le"] = dt.datetime.now()
            _maj_compte(cur, login, maj)
            if maj.get("actif") == 0 or ("roles" in maj and login != par):
                # un compte coupe ou dont les droits changent repart de zero ;
                # l'admin qui se modifie lui-meme garde sa session
                cur.execute("UPDATE session_locale SET revoquee = 1 "
                            "WHERE login = %s AND revoquee = 0", (login,))
            apres = {k: v for k, v in maj.items() if k != "maj_le"}
            inventory_db.journaliser(cur, par, "compte local modifie",
                                     "compte " + login, _public(avant), apres)
    except inventory_db.VenteInvalide:
        conn.rollback()
        raise
    except _SQL as exc:
        conn.rollback()
        raise inventory_db._traduit(exc, "la modification du compte") from exc
    conn.commit()
    return apres


def connecter(conn, login, mdp, origine):
    """Ouvre une session. Rend (jeton, compte) ; leve Refus sinon."""
    login = (login or "").strip().lower()[:64]
    maintenant = dt.datetime.now()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM compte_local WHERE login = %s FOR UPDATE",
                        (login,))
            compte = cur.fetchone()
            maj, refus = evaluer_connexion(compte, mdp or "", maintenant)
            _maj_compte(cur, login, maj)
            if refus is not None:
                inventory_db.journaliser(
                    cur, login or "?", "echec connexion", "compte " + login,
                    None, {"origine": origine, "motif": str(refus)})
                conn.commit()
                raise refus
            jeton = secrets.token_urlsafe(32)
            cur.execute(
                "INSERT INTO session_locale (empreinte, login, cree_le, vu_le, "
                "expire_le, revoquee, origine) VALUES (%s, %s, %s, %s, %s, 0, %s)",
                (empreinte_session(jeton), login, maintenant, maintenant,
                 maintenant + DUREE_MAX, (origine or "")[:45]))
            inventory_db.journaliser(cur, login, "connexion", "compte " + login,
                                     None, {"origine": origine})
    except Refus:
        raise
    except _SQL as exc:
        conn.rollback()
        raise inventory_db._traduit(exc, "la connexion") from exc
    conn.commit()
    return jeton, compte_de_session(compte)


def session(conn, jeton):
    """Le compte d'une session valable, ou None."""
    if not jeton:
        return None
    maintenant = dt.datetime.now()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.empreinte, s.login, s.vu_le, s.expire_le, s.revoquee,"
                " c.roles, c.doit_changer, c.actif FROM session_locale s "
                "JOIN compte_local c ON c.login = s.login "
                "WHERE s.empreinte = %s", (empreinte_session(jeton),))
            s = cur.fetchone()
            if not session_valide(s, maintenant):
                return None
            # une ecriture par minute au plus, pas une par requete
            if maintenant - s["vu_le"] > dt.timedelta(minutes=1):
                cur.execute("UPDATE session_locale SET vu_le = %s "
                            "WHERE empreinte = %s", (maintenant, s["empreinte"]))
                conn.commit()
    except _SQL as exc:
        raise inventory_db._traduit(exc, "la lecture de la session") from exc
    return compte_de_session(s)


def deconnecter(conn, jeton, login):
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE session_locale SET revoquee = 1 "
                        "WHERE empreinte = %s", (empreinte_session(jeton),))
            inventory_db.journaliser(cur, login, "deconnexion",
                                     "compte " + login)
    except _SQL as exc:
        conn.rollback()
        raise inventory_db._traduit(exc, "la deconnexion") from exc
    conn.commit()


def changer_mdp(conn, login, actuel, nouveau, jeton):
    """Change le mot de passe ; ferme toutes les autres sessions du compte."""
    motif = politique(nouveau, login)
    if motif:
        raise Refus(422, "mot de passe refuse : " + motif)
    if nouveau == actuel:
        raise Refus(422, "le nouveau mot de passe doit differer de l'actuel")
    maintenant = dt.datetime.now()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM compte_local WHERE login = %s FOR UPDATE",
                        (login,))
            compte = cur.fetchone()
            maj, refus = evaluer_connexion(compte, actuel or "", maintenant)
            if refus is not None:
                _maj_compte(cur, login, maj)
                inventory_db.journaliser(cur, login, "echec mot de passe",
                                         "compte " + login)
                conn.commit()
                raise Refus(403 if refus.code == 401 else refus.code,
                            "mot de passe actuel incorrect"
                            if refus.code == 401 else str(refus))
            _maj_compte(cur, login, {"empreinte": hacher(nouveau),
                                     "doit_changer": 0, "echecs": 0,
                                     "bloque_jusqu_a": None,
                                     "maj_le": maintenant})
            cur.execute("UPDATE session_locale SET revoquee = 1 WHERE login = %s"
                        " AND empreinte <> %s AND revoquee = 0",
                        (login, empreinte_session(jeton)))
            inventory_db.journaliser(cur, login, "mot de passe change",
                                     "compte " + login)
    except Refus:
        raise
    except _SQL as exc:
        conn.rollback()
        raise inventory_db._traduit(exc, "le changement de mot de passe") from exc
    conn.commit()
