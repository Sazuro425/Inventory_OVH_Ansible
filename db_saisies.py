"""
Ce que les humains saisissent : ventes, elements declares, corrections,
roles. Et la rentabilite, qui les combine au dernier instantane.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata

from db_base import (
    DEVISES,
    DbError,
    PERIODES,
    VenteInvalide,
    _date_saisie,
    _json_ok,
    _prix,
    _texte,
    _traduit,
    journaliser,
    pymysql,
)
from db_snapshots import (
    load_latest,
)


# ---------------------------------------------------------------------------
# Volet commercial : les ventes, et la rentabilite qui en decoule.
#
# Ces fonctions ecrivent, contrairement a tout le reste du module qui ne fait
# que deposer des instantanes. C'est la seule donnee de la base qu'un humain
# saisit, donc la seule ou la validation compte vraiment : un prix negatif ou
# un contrat vide fausserait le total du parc sans rien declencher.
# ---------------------------------------------------------------------------

_REF_VM = re.compile(r"^[A-Za-z0-9._:-]{1,160}/\d{1,9}$")


def verifier_ref_vm(ref):
    """Une reference cluster/vmid, ou None. Voir infra_inventory.ref_vm."""
    if ref in (None, ""):
        return None
    ref = str(ref).strip()
    if not _REF_VM.match(ref):
        raise VenteInvalide("reference de VM illisible : cluster/vmid attendu")
    return ref


def verifier_vente(v):
    """Normalise et controle une vente. Renvoie le dict propre a ecrire.

    vm_ref (cluster/vmid) designe la VM ; vm_name en est le libelle, et le
    repli d'une saisie faite sans reference (client d'API, ancienne page).
    """
    nom = _texte(v.get("vm_name"), "le nom de la VM", 190, obligatoire=True)
    contrat = _texte(v.get("contrat"), "le numero de contrat", 100,
                     obligatoire=True)
    if v.get("prix_vente") in (None, ""):
        raise VenteInvalide("le prix de vente est obligatoire")
    prix = _prix(v.get("prix_vente"), "le prix de vente")

    periode = (v.get("periode") or "mois").strip().lower()
    if periode not in PERIODES:
        raise VenteInvalide("periode inconnue : attendu " + " ou ".join(PERIODES))

    devise = (v.get("devise") or "EUR").strip().upper()
    if devise not in DEVISES:
        raise VenteInvalide("devise inconnue : " + devise)

    debut = _date_saisie(v.get("debut"), "debut")
    fin = _date_saisie(v.get("fin"), "fin")
    if debut and fin and fin < debut:
        raise VenteInvalide("la date de fin precede la date de debut")

    return {
        "vm_name": nom,
        "vm_ref": verifier_ref_vm(v.get("vm_ref")),
        "cluster_name": _texte(v.get("cluster_name"), "le cluster", 190),
        "contrat": contrat,
        "client": _texte(v.get("client"), "le client", 190),
        "prix_vente": prix,
        "devise": devise,
        "periode": periode,
        "debut": debut,
        "fin": fin,
    }


def enregistrer_vente(conn, vente, par):
    """Cree la vente, ou met a jour celle qui porte deja ce numero de contrat.

    Le numero de contrat est la clef : resaisir le meme contrat corrige la
    ligne au lieu d'en ajouter une seconde. C'est ce qu'un commercial attend
    quand il se rend compte qu'il s'est trompe de prix.
    """
    v = verifier_vente(vente)
    maintenant = dt.datetime.now()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM commercial WHERE contrat = %s "
                        "FOR UPDATE", (v["contrat"],))
            avant = cur.fetchone()
            cur.execute(
                "INSERT INTO commercial (vm_name, vm_ref, cluster_name, "
                "contrat, client, prix_vente, devise, periode, debut, fin, "
                "actif, cree_le, cree_par) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s) "
                "ON DUPLICATE KEY UPDATE "
                "  vm_name = VALUES(vm_name), "
                "  vm_ref = VALUES(vm_ref), "
                "  cluster_name = VALUES(cluster_name), "
                "  client = VALUES(client), "
                "  prix_vente = VALUES(prix_vente), "
                "  devise = VALUES(devise), "
                "  periode = VALUES(periode), "
                "  debut = VALUES(debut), "
                "  fin = VALUES(fin), "
                "  actif = 1, "
                "  maj_le = %s, maj_par = %s",
                (v["vm_name"], v["vm_ref"], v["cluster_name"], v["contrat"],
                 v["client"],
                 v["prix_vente"], v["devise"], v["periode"], v["debut"],
                 v["fin"], maintenant, par, maintenant, par))
            # rowcount : 1 = insertion, 2 = mise a jour (convention MySQL)
            cree = cur.rowcount == 1
            journaliser(cur, par, "vente", "contrat " + v["contrat"],
                        avant, v)
    except pymysql.Error as exc:
        conn.rollback()
        if (exc.args[0] if exc.args else 0) == 1146:
            raise DbError(
                "la table commercial n'existe pas : rejoue sql/schema.sql"
            ) from exc
        raise _traduit(exc, "l'enregistrement de la vente") from exc
    conn.commit()
    return dict(v, cree=cree)


def desactiver_vente(conn, contrat, par):
    """Resilie : la ligne reste, actif passe a 0. Jamais de DELETE."""
    contrat = (contrat or "").strip()
    if not contrat:
        raise VenteInvalide("numero de contrat absent")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE commercial SET actif = 0, maj_le = %s, maj_par = %s "
                "WHERE contrat = %s AND actif = 1",
                (dt.datetime.now(), par, contrat))
            touche = cur.rowcount
            if touche:
                journaliser(cur, par, "resiliation", "contrat " + contrat,
                            {"actif": 1}, {"actif": 0})
    except pymysql.Error as exc:
        conn.rollback()
        raise _traduit(exc, "la resiliation") from exc
    conn.commit()
    return touche > 0


def lister_ventes(conn, vm=None, actives=True):
    sql = "SELECT * FROM commercial"
    where, args = [], []
    if vm:
        # une reference cluster/vmid, ou un nom
        where.append("vm_ref = %s" if "/" in vm else "vm_name = %s")
        args.append(vm)
    if actives:
        where.append("actif = 1")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY vm_name, contrat"
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return [{k: _json_ok(x) for k, x in r.items()} for r in cur.fetchall()]
    except pymysql.Error as exc:
        if (exc.args[0] if exc.args else 0) == 1146:
            return []
        raise _traduit(exc, "la lecture des ventes") from exc


def lire_saisies(conn):
    """Ce que les humains ont saisi : ventes actives, elements, corrections,
    roles poses sur les cartes, et le catalogue des roles.

    Une table absente (schema pas encore rejoue) rend une liste vide plutot
    qu'une erreur : le calcul reste possible, simplement sans ces saisies.
    """
    requetes = {
        "ventes": "SELECT * FROM v_vente_active",
        "elements": "SELECT * FROM element WHERE actif = 1",
        "surcharges": "SELECT * FROM surcharge WHERE valeur IS NOT NULL",
        "roles": "SELECT * FROM role_carte WHERE role IS NOT NULL",
        "catalogue": "SELECT code, libelle FROM role WHERE actif = 1 "
                     "ORDER BY libelle",
    }
    out = {}
    with conn.cursor() as cur:
        for cle, sql in requetes.items():
            try:
                cur.execute(sql)
                out[cle] = [{k: _json_ok(v) for k, v in r.items()}
                            for r in cur.fetchall()]
            except pymysql.Error as exc:
                if (exc.args[0] if exc.args else 0) not in (1146, 1356):
                    raise _traduit(exc, "la lecture des saisies") from exc
                out[cle] = []
    return out


def rentabilite(conn):
    """L'arbre de rentabilite du dernier snapshot, saisies comprises.

    Le calcul est dans rentabilite.py, et nulle part ailleurs : ce module ne
    fait que lui passer les lignes.
    """
    import rentabilite as calcul
    data = load_latest(conn)
    if data is None:
        raise DbError("aucun snapshot en base : lance une collecte")
    saisies = lire_saisies(conn)
    res = calcul.calculer(data, saisies["ventes"], saisies["elements"],
                          saisies["surcharges"], saisies["roles"],
                          saisies["catalogue"])
    res["roles"] = saisies["catalogue"]
    res["snapshot"] = data["run_id"]
    res["genere_le"] = data["generated_at"]
    return res


# ---------------------------------------------------------------------------
# Elements declares et corrections
# ---------------------------------------------------------------------------

PARENTS = ("cluster", "serveur", "vm", "element")
CIBLES = ("cluster", "serveur", "vm")
CHAMPS = ("prix_achat", "prix_vente", "cluster")
CHAMPS_ELEMENT = ("parent_type", "parent_ref", "nom", "role", "contrat",
                  "client", "prix_vente", "prix_achat", "devise", "periode",
                  "note")


def verifier_element(e):
    """Normalise et controle un element declare. Renvoie le dict a ecrire."""
    parent_type = (e.get("parent_type") or "").strip()
    if parent_type not in PARENTS:
        raise VenteInvalide("type de parent inconnu : attendu "
                            + ", ".join(PARENTS))
    periode = (e.get("periode") or "mois").strip().lower()
    if periode not in PERIODES:
        raise VenteInvalide("periode inconnue : attendu " + " ou ".join(PERIODES))
    devise = (e.get("devise") or "EUR").strip().upper()
    if devise not in DEVISES:
        raise VenteInvalide("devise inconnue : " + devise)
    ident = e.get("id")
    if ident in (None, ""):
        ident = None
    else:
        try:
            ident = int(ident)
        except (TypeError, ValueError):
            raise VenteInvalide("identifiant d'element illisible") from None
    propre = {
        "id": ident,
        "parent_type": parent_type,
        "parent_ref": _texte(e.get("parent_ref"), "le parent", 190,
                             obligatoire=True),
        "nom": _texte(e.get("nom"), "le nom", 190, obligatoire=True),
        "role": code_role(e.get("role")),
        "contrat": _texte(e.get("contrat"), "le contrat", 100),
        "client": _texte(e.get("client"), "le client", 190),
        "prix_vente": _prix(e.get("prix_vente"), "le prix de vente"),
        "prix_achat": _prix(e.get("prix_achat"), "le prix d'achat"),
        "devise": devise,
        "periode": periode,
        "note": _texte(e.get("note"), "la note", 500),
    }
    if parent_type == "element":
        if not propre["parent_ref"].isdigit():
            raise VenteInvalide("un element parent se designe par son numero")
        if ident is not None and str(ident) == propre["parent_ref"]:
            raise VenteInvalide("un element ne peut pas etre son propre parent")
    return propre


def _inserer_element(cur, e, par, maintenant):
    cur.execute(
        "INSERT INTO element ({}, actif, cree_le, cree_par) "
        "VALUES ({}, 1, %s, %s)".format(
            ", ".join(CHAMPS_ELEMENT), ", ".join(["%s"] * len(CHAMPS_ELEMENT))),
        [e[c] for c in CHAMPS_ELEMENT] + [maintenant, par])
    e["id"] = cur.lastrowid
    journaliser(cur, par, "element", "element {}".format(e["id"]), None, e)


def enregistrer_element(conn, element, par):
    """Cree un element, ou corrige celui dont l'id est donne."""
    e = verifier_element(element)
    maintenant = dt.datetime.now()
    valeurs = [e[c] for c in CHAMPS_ELEMENT]
    try:
        with conn.cursor() as cur:
            _role_existe(cur, e["role"])
            if e["id"] is None:
                _inserer_element(cur, e, par, maintenant)
                cree = True
            else:
                cur.execute("SELECT * FROM element WHERE id = %s AND actif = 1 "
                            "FOR UPDATE", (e["id"],))
                avant = cur.fetchone()
                if not avant:
                    raise VenteInvalide("aucun element actif sous ce numero")
                cur.execute(
                    "UPDATE element SET {}, maj_le = %s, maj_par = %s "
                    "WHERE id = %s".format(
                        ", ".join(c + " = %s" for c in CHAMPS_ELEMENT)),
                    valeurs + [maintenant, par, e["id"]])
                cree = False
                journaliser(cur, par, "element", "element {}".format(e["id"]),
                            avant, e)
    except VenteInvalide:
        conn.rollback()
        raise
    except pymysql.Error as exc:
        conn.rollback()
        raise _traduit(exc, "l'enregistrement de l'element") from exc
    conn.commit()
    return dict(e, cree=cree)


LOT_MAX = 200


def verifier_lot(lot):
    """Plusieurs enfants d'un coup : une liste de noms, le reste en commun.

    "La VM A heberge 4 sites a 40 EUR" : noms = les 4 sites, prix_vente = 40.
    Chaque element est controle comme s'il etait saisi seul. Renvoie la
    liste des dicts a ecrire.
    """
    noms = lot.get("noms")
    if not isinstance(noms, list):
        raise VenteInvalide("noms attend une liste")
    noms = [n.strip() if isinstance(n, str) else n for n in noms]
    noms = [n for n in noms if n not in (None, "")]
    if not noms:
        raise VenteInvalide("aucun nom : un enfant par ligne")
    if len(noms) > LOT_MAX:
        raise VenteInvalide("{} enfants au plus par saisie".format(LOT_MAX))
    vus = set()
    for n in noms:
        cle = str(n).lower()
        if cle in vus:
            raise VenteInvalide("nom en double dans la saisie : {}".format(n))
        vus.add(cle)
    commun = {k: lot.get(k) for k in CHAMPS_ELEMENT if k != "nom"}
    return [verifier_element(dict(commun, nom=n)) for n in noms]


def enregistrer_elements(conn, lot, par):
    """Cree un lot d'elements sous le meme parent, tout ou rien."""
    propres = verifier_lot(lot)
    maintenant = dt.datetime.now()
    try:
        with conn.cursor() as cur:
            _role_existe(cur, propres[0]["role"])
            for e in propres:
                _inserer_element(cur, e, par, maintenant)
    except VenteInvalide:
        conn.rollback()
        raise
    except pymysql.Error as exc:
        conn.rollback()
        raise _traduit(exc, "l'enregistrement des elements") from exc
    conn.commit()
    return propres


def retirer_element(conn, ident, par):
    """Desactive un element. Ses enfants deviennent orphelins : ils restent
    comptes, et le calcul les signale, plutot que de disparaitre en silence."""
    try:
        ident = int(ident)
    except (TypeError, ValueError):
        raise VenteInvalide("identifiant d'element illisible") from None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE element SET actif = 0, maj_le = %s, maj_par = %s "
                "WHERE id = %s AND actif = 1", (dt.datetime.now(), par, ident))
            touche = cur.rowcount
            if touche:
                journaliser(cur, par, "retrait", "element {}".format(ident),
                            {"actif": 1}, {"actif": 0})
    except pymysql.Error as exc:
        conn.rollback()
        raise _traduit(exc, "le retrait de l'element") from exc
    conn.commit()
    return touche > 0


def verifier_surcharge(s):
    cible_type = (s.get("cible_type") or "").strip()
    champ = (s.get("champ") or "").strip()
    if cible_type not in CIBLES:
        raise VenteInvalide("cible inconnue : attendu " + ", ".join(CIBLES))
    if champ not in CHAMPS:
        raise VenteInvalide("champ inconnu : attendu " + ", ".join(CHAMPS))
    if champ == "cluster" and cible_type != "serveur":
        raise VenteInvalide("seul un serveur change de cluster")
    valeur = s.get("valeur")
    if valeur in (None, ""):
        valeur = None                       # leve la correction
    elif champ == "cluster":
        valeur = _texte(valeur, "le nom du cluster", 190)
        if valeur.startswith(("seul:", "standalone:", "inconnu:")):
            raise VenteInvalide("ce prefixe est reserve au calcul")
    else:
        valeur = "{:.2f}".format(_prix(valeur, "la valeur"))
    return {
        "cible_type": cible_type,
        "cible_ref": _texte(s.get("cible_ref"), "la cible", 190,
                            obligatoire=True),
        "champ": champ,
        "valeur": valeur,
        # une correction sans motif est une correction que personne ne saura
        # justifier dans six mois
        "motif": _texte(s.get("motif"), "le motif", 300, obligatoire=True),
    }


def poser_surcharge(conn, surcharge, par):
    """Pose ou leve une correction. Lever, c'est poser une valeur nulle."""
    s = verifier_surcharge(surcharge)
    cible = "{} {} {}".format(s["cible_type"], s["cible_ref"], s["champ"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT valeur, motif FROM surcharge WHERE cible_type = %s "
                "AND cible_ref = %s AND champ = %s FOR UPDATE",
                (s["cible_type"], s["cible_ref"], s["champ"]))
            avant = cur.fetchone()
            cur.execute(
                "INSERT INTO surcharge (cible_type, cible_ref, champ, valeur, "
                "motif, maj_le, maj_par) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE valeur = VALUES(valeur), "
                "motif = VALUES(motif), maj_le = VALUES(maj_le), "
                "maj_par = VALUES(maj_par)",
                (s["cible_type"], s["cible_ref"], s["champ"], s["valeur"],
                 s["motif"], dt.datetime.now(), par))
            journaliser(cur, par,
                        "correction" if s["valeur"] is not None else "levee",
                        cible, avant, {"valeur": s["valeur"],
                                       "motif": s["motif"]})
    except pymysql.Error as exc:
        conn.rollback()
        raise _traduit(exc, "la correction") from exc
    conn.commit()
    return s


# ---------------------------------------------------------------------------
# Roles : le catalogue, et le role d'une carte
# ---------------------------------------------------------------------------

CIBLES_ROLE = ("cluster", "serveur", "vm")
_CODE_ROLE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


def code_role(v):
    """Un code de role tel qu'il circule : minuscules, sans espace. Vide
    donne None. L'existence dans le catalogue se verifie en base."""
    if v in (None, ""):
        return None
    code = str(v).strip().lower()
    if not _CODE_ROLE.match(code):
        raise VenteInvalide("code de role illisible : lettres, chiffres, - et _")
    return code


def code_depuis_libelle(libelle):
    """« Serveur LAMP » donne serveur-lamp. Les accents tombent, le reste
    devient un tiret."""
    t = unicodedata.normalize("NFKD", libelle).encode("ascii", "ignore")
    return re.sub(r"[^a-z0-9]+", "-", t.decode().lower()).strip("-")[:40].strip("-")


def verifier_role(r):
    """Une entree du catalogue. Le code se deduit du libelle s'il manque."""
    libelle = _texte(r.get("libelle"), "le libelle du role", 80,
                     obligatoire=True)
    code = code_role(r.get("code")) or code_depuis_libelle(libelle)
    if not code:
        raise VenteInvalide("le libelle doit contenir une lettre ou un chiffre")
    return {"code": code, "libelle": libelle}


def verifier_role_carte(r):
    cible_type = (r.get("cible_type") or "").strip()
    if cible_type not in CIBLES_ROLE:
        raise VenteInvalide("cible inconnue : attendu " + ", ".join(CIBLES_ROLE))
    return {
        "cible_type": cible_type,
        "cible_ref": _texte(r.get("cible_ref"), "la cible", 190,
                            obligatoire=True),
        "role": code_role(r.get("role")),        # None leve le role
    }


def _role_existe(cur, code):
    """Refuse un role absent du catalogue, ou retire. None passe."""
    if code is None:
        return
    cur.execute("SELECT actif FROM role WHERE code = %s", (code,))
    r = cur.fetchone()
    if not r or not r["actif"]:
        raise VenteInvalide("role inconnu : " + code)


def creer_role(conn, role, par):
    """Ajoute un role au catalogue. Un role retire se reactive."""
    r = verifier_role(role)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM role WHERE code = %s FOR UPDATE",
                        (r["code"],))
            avant = cur.fetchone()
            if avant and avant["actif"]:
                raise VenteInvalide("le role existe deja : {}".format(
                    avant["libelle"]))
            if avant:
                cur.execute("UPDATE role SET actif = 1, libelle = %s "
                            "WHERE code = %s", (r["libelle"], r["code"]))
            else:
                cur.execute("INSERT INTO role (code, libelle, actif, cree_le, "
                            "cree_par) VALUES (%s, %s, 1, %s, %s)",
                            (r["code"], r["libelle"], dt.datetime.now(), par))
            journaliser(cur, par, "role", "catalogue " + r["code"], avant, r)
    except VenteInvalide:
        conn.rollback()
        raise
    except pymysql.Error as exc:
        conn.rollback()
        raise _traduit(exc, "l'ajout du role") from exc
    conn.commit()
    return r


def poser_role(conn, attribution, par):
    """Pose le role d'un cluster, d'un serveur ou d'une VM ; None le leve."""
    a = verifier_role_carte(attribution)
    cible = "{} {}".format(a["cible_type"], a["cible_ref"])
    try:
        with conn.cursor() as cur:
            _role_existe(cur, a["role"])
            cur.execute("SELECT role FROM role_carte WHERE cible_type = %s "
                        "AND cible_ref = %s FOR UPDATE",
                        (a["cible_type"], a["cible_ref"]))
            avant = cur.fetchone()
            cur.execute(
                "INSERT INTO role_carte (cible_type, cible_ref, role, maj_le, "
                "maj_par) VALUES (%s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE role = VALUES(role), "
                "maj_le = VALUES(maj_le), maj_par = VALUES(maj_par)",
                (a["cible_type"], a["cible_ref"], a["role"],
                 dt.datetime.now(), par))
            journaliser(cur, par, "role", cible, avant, {"role": a["role"]})
    except VenteInvalide:
        conn.rollback()
        raise
    except pymysql.Error as exc:
        conn.rollback()
        raise _traduit(exc, "le role") from exc
    conn.commit()
    return a

