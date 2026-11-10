#!/usr/bin/env python3
"""
Boite de depot scellee : deposer un jeton PVE depuis la page, sans que le
service web puisse jamais en relire un.

Le service web est le seul composant expose. Il n'a ni la clef maitresse ni
aucun droit sur secret_store, et ce depot ne change rien a cela :

    page ── TLS ──> service web ── scelle ──> depot_jeton (en attente)
                    (clef PUBLIQUE                 │
                     seulement)                    ▼
                                       collecte : ouvre (clef privee, elle-
                                       meme chiffree dans secret_store),
                                       teste le jeton sur l'hyperviseur,
                                       le range dans secret_store, ou le
                                       refuse avec son motif

Un service web compromis peut lire les jetons deposes APRES sa compromission
— il les voit passer en clair le temps de les sceller. Il ne peut ni ouvrir
les depots anterieurs, ni lire un seul jeton deja en service.

Le scellement : X25519 ephemere vers la clef publique de depot, HKDF-SHA256,
AES-256-GCM. L'hote est lie au chiffre (donnee associee) : un depot recopie
sur la ligne d'un autre hote ne s'ouvre pas.

    python3 inventory_cli.py --init-depot      une fois, apres --init-key
    python3 inventory_cli.py --traiter-depots  a chaque collecte (collecte.sh)
"""

from __future__ import annotations

import base64
import datetime as dt
import re

import inventory_db

NOM_CLE_PRIVEE = "depot_cle_privee"
INFO = b"inventaire-depot-v1"
# user@realm!nom=secret ; le secret d'un jeton PVE est un UUID
FORMAT_JETON = re.compile(
    r"^[A-Za-z0-9._-]{1,64}@[A-Za-z0-9._-]{1,32}![A-Za-z0-9._-]{1,64}="
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
HOTES_MAX = 64
ECHEANCE_MAX = dt.timedelta(days=5 * 366)
_SQL = inventory_db.pymysql.Error if inventory_db.pymysql else ()


class DepotErreur(RuntimeError):
    """Depot impossible a sceller ou a ouvrir."""


# ---------------------------------------------------------------------------
# Chiffrement, sans base
# ---------------------------------------------------------------------------

def _primitives():
    # import tardif : le service ne charge cryptography que s'il scelle
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey, X25519PublicKey)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return hashes, serialization, X25519PrivateKey, X25519PublicKey, AESGCM, HKDF


def _brut(cle_publique):
    _, serialization, *_ = _primitives()
    return cle_publique.public_bytes(serialization.Encoding.Raw,
                                     serialization.PublicFormat.Raw)


def _derive(partage, eph, dest):
    hashes, _, _, _, _, HKDF = _primitives()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=eph + dest,
                info=INFO).derive(partage)


def generer_paire():
    """(privee, publique), 32 octets bruts chacune."""
    _, serialization, X25519PrivateKey, *_ = _primitives()
    privee = X25519PrivateKey.generate()
    return (privee.private_bytes(serialization.Encoding.Raw,
                                 serialization.PrivateFormat.Raw,
                                 serialization.NoEncryption()),
            _brut(privee.public_key()))


def sceller(publique, texte, contexte):
    """Chiffre texte pour le detenteur de la clef privee. Rend des octets :
    clef ephemere (32) | nonce (12) | chiffre."""
    import os
    _, _, X25519PrivateKey, X25519PublicKey, AESGCM, _ = _primitives()
    eph = X25519PrivateKey.generate()
    eph_pub = _brut(eph.public_key())
    cle = _derive(eph.exchange(X25519PublicKey.from_public_bytes(publique)),
                  eph_pub, publique)
    nonce = os.urandom(12)
    return eph_pub + nonce + AESGCM(cle).encrypt(
        nonce, texte.encode("utf-8"), contexte.encode("utf-8"))


def ouvrir(privee, scelle, contexte):
    from cryptography.exceptions import InvalidTag
    _, _, X25519PrivateKey, X25519PublicKey, AESGCM, _ = _primitives()
    if len(scelle) < 32 + 12 + 16:
        raise DepotErreur("depot tronque")
    eph_pub, nonce, chiffre = scelle[:32], scelle[32:44], scelle[44:]
    k = X25519PrivateKey.from_private_bytes(privee)
    cle = _derive(k.exchange(X25519PublicKey.from_public_bytes(eph_pub)),
                  eph_pub, _brut(k.public_key()))
    try:
        return AESGCM(cle).decrypt(nonce, chiffre,
                                   contexte.encode("utf-8")).decode("utf-8")
    except InvalidTag:
        raise DepotErreur("depot illisible : autre clef, ou autre hote") from None


# ---------------------------------------------------------------------------
# Controle d'un depot, sans base
# ---------------------------------------------------------------------------

def verifier_depot(charge, hotes_connus, aujourd_hui=None):
    """Rend (hotes, jeton, echeance). hotes_connus : les reverse du dernier
    snapshot. echeance : la date d'expiration declaree, ou None.

    On ne depose que pour un hote que la collecte connait : un nom tape a la
    main finirait en secret orphelin que personne ne lit jamais.
    """
    jeton = charge.get("jeton")
    if not isinstance(jeton, str) or not FORMAT_JETON.match(jeton.strip()):
        raise inventory_db.VenteInvalide(
            "jeton illisible : attendu utilisateur@realm!nom=uuid, tel que "
            "l'affiche pveum user token add")
    hotes = charge.get("hotes")
    if not isinstance(hotes, list) or not hotes:
        raise inventory_db.VenteInvalide("au moins un hyperviseur")
    if len(hotes) > HOTES_MAX:
        raise inventory_db.VenteInvalide(
            "{} hyperviseurs au plus par depot".format(HOTES_MAX))
    propres = []
    for h in hotes:
        if not isinstance(h, str) or h not in hotes_connus:
            raise inventory_db.VenteInvalide(
                "hyperviseur inconnu de la derniere collecte : {}".format(
                    str(h)[:80]))
        if h not in propres:
            propres.append(h)
    aujourd_hui = aujourd_hui or dt.date.today()
    echeance = None
    if charge.get("expire_le") not in (None, ""):
        try:
            echeance = dt.date.fromisoformat(str(charge["expire_le"]))
        except ValueError:
            raise inventory_db.VenteInvalide(
                "echeance illisible : AAAA-MM-JJ attendu") from None
        if echeance <= aujourd_hui:
            raise inventory_db.VenteInvalide("l'echeance est deja passee")
        if echeance > aujourd_hui + ECHEANCE_MAX:
            raise inventory_db.VenteInvalide("echeance a plus de cinq ans")
    return propres, jeton.strip(), echeance


# ---------------------------------------------------------------------------
# Base, cote service web : clef publique, depot, etat
# ---------------------------------------------------------------------------

def cle_publique(conn):
    """(version, octets) de la clef de depot active, ou None."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version, publique FROM cle_depot WHERE actif = 1 "
                        "ORDER BY version DESC LIMIT 1")
            r = cur.fetchone()
    except _SQL as exc:
        if (exc.args[0] if exc.args else 0) == 1146:
            return None
        raise inventory_db._traduit(exc, "la lecture de la clef de depot") from exc
    return (r["version"], bytes(r["publique"])) if r else None


def deposer(conn, hotes, jeton, par, echeance=None):
    """Scelle le jeton pour chaque hote et l'enregistre en attente."""
    cle = cle_publique(conn)
    if cle is None:
        raise inventory_db.VenteInvalide(
            "boite de depot non initialisee : inventory_cli.py --init-depot "
            "sur la VM de collecte")
    version, publique = cle
    maintenant = dt.datetime.now()
    try:
        with conn.cursor() as cur:
            for h in hotes:
                cur.execute(
                    "INSERT INTO depot_jeton (hote, cle_version, scelle, "
                    "depose_le, depose_par, expire_le, etat) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 'en_attente')",
                    (h, version, sceller(publique, jeton, h), maintenant, par,
                     echeance))
                # le journal dit qui a depose pour quel hote, jamais quoi
                inventory_db.journaliser(cur, par, "depot jeton pve",
                                         "hote " + h, None,
                                         {"depot": cur.lastrowid})
    except _SQL as exc:
        conn.rollback()
        raise inventory_db._traduit(exc, "le depot du jeton") from exc
    conn.commit()
    return len(hotes)


def etat_depots(conn):
    """Le dernier depot de chaque hote : {hote: {etat, depose_le, ...}}."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT d.hote, d.etat, d.depose_le, d.depose_par, d.traite_le,"
                " d.motif, d.expire_le FROM depot_jeton d JOIN (SELECT hote, "
                "MAX(id) AS id "
                "FROM depot_jeton GROUP BY hote) m ON m.id = d.id")
            lignes = cur.fetchall()
    except _SQL as exc:
        if (exc.args[0] if exc.args else 0) == 1146:
            return {}
        raise inventory_db._traduit(exc, "la lecture des depots") from exc
    def lisible(v):
        if isinstance(v, dt.datetime):
            return v.isoformat(sep=" ", timespec="minutes")
        return v.isoformat() if isinstance(v, dt.date) else v
    return {r["hote"]: {k: lisible(v) for k, v in r.items() if k != "hote"}
            for r in lignes}


# ---------------------------------------------------------------------------
# Base, cote collecte : la paire de clefs, le traitement
# ---------------------------------------------------------------------------

def initialiser(conn, cle_maitresse, par):
    """Tire la paire de depot. La privee va dans secret_store, chiffree par
    la clef maitresse ; la publique dans cle_depot, lisible du service web.
    Rend la version creee."""
    import secret_store
    privee, publique = generer_paire()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(version), 0) AS v FROM cle_depot")
            version = cur.fetchone()["v"] + 1
    except _SQL as exc:
        raise inventory_db._traduit(exc, "la clef de depot") from exc
    secret_store.ecrire(conn, NOM_CLE_PRIVEE,
                        base64.b64encode(privee).decode("ascii"), par,
                        cle_maitresse)
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE cle_depot SET actif = 0 WHERE actif = 1")
            cur.execute("INSERT INTO cle_depot (version, publique, actif, cree_le,"
                        " cree_par) VALUES (%s, %s, 1, %s, %s)",
                        (version, publique, dt.datetime.now(), par))
    except _SQL as exc:
        conn.rollback()
        raise inventory_db._traduit(exc, "la clef de depot") from exc
    conn.commit()
    return version


def en_attente(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id, hote, cle_version, scelle, depose_par "
                    "FROM depot_jeton WHERE etat = 'en_attente' ORDER BY id")
        return cur.fetchall()


def _clore(conn, ident, etat, motif, par, hote):
    with conn.cursor() as cur:
        cur.execute("UPDATE depot_jeton SET etat = %s, traite_le = %s, "
                    "motif = %s WHERE id = %s",
                    (etat, dt.datetime.now(), (motif or "")[:300] or None, ident))
        if etat != "remplace":
            inventory_db.journaliser(cur, par, "jeton pve " + etat,
                                     "hote " + hote, None,
                                     {"depot": ident, "motif": motif})
    conn.commit()


def traiter(conn, cle_maitresse, tester, nom_secret, dire=print):
    """Ouvre, teste et range les depots en attente. Rend (acceptes, refuses).

    tester(hote, jeton) -> (ok, motif) ; nom_secret(hote) -> nom du secret.
    Pour un hote, seul le depot le plus recent compte : les precedents
    passent a 'remplace' sans etre ouverts.
    """
    import secret_store
    depots = en_attente(conn)
    if not depots:
        return 0, 0
    privee = base64.b64decode(secret_store.lire(conn, NOM_CLE_PRIVEE,
                                                cle_maitresse))
    dernier = {}
    for d in depots:
        dernier[d["hote"]] = d
    acceptes = refuses = 0
    for d in depots:
        hote = d["hote"]
        if dernier[hote] is not d:
            _clore(conn, d["id"], "remplace", "un depot plus recent existe",
                   "collecte", hote)
            continue
        try:
            jeton = ouvrir(privee, bytes(d["scelle"]), hote)
        except DepotErreur as exc:
            _clore(conn, d["id"], "refuse", str(exc), d["depose_par"], hote)
            refuses += 1
            dire("  {} : refuse ({})".format(hote, exc))
            continue
        ok, motif = tester(hote, jeton)
        if not ok:
            _clore(conn, d["id"], "refuse", motif, d["depose_par"], hote)
            refuses += 1
            dire("  {} : refuse ({})".format(hote, motif))
            continue
        secret_store.ecrire(conn, nom_secret(hote), jeton,
                            "depot:" + str(d["depose_par"]), cle_maitresse)
        _clore(conn, d["id"], "accepte", None, d["depose_par"], hote)
        acceptes += 1
        dire("  {} : accepte, range sous {}".format(hote, nom_secret(hote)))
    return acceptes, refuses
