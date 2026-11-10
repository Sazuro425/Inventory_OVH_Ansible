"""
Les vues de rentabilite enregistrees : des filtres nommes, partages ou non.
"""

from __future__ import annotations

import datetime as dt
import json

from db_base import (
    SaisieInterdite,
    VenteInvalide,
    _texte,
    _traduit,
    journaliser,
    pymysql,
)


# ---------------------------------------------------------------------------
# Vues de rentabilite enregistrees
# ---------------------------------------------------------------------------

MODES_VUE = ("arbre", "tableau", "graphiques")
NIVEAUX_VUE = ("cluster", "serveur", "vm", "element", "tout")
LISTES_VUE = ("clusters", "datacenters", "clients", "roles")


def verifier_vue(v):
    """Controle une vue enregistree. Renvoie (id, nom, partagee, definition).

    La definition est relue et rejouee par la page : seuls les champs connus,
    avec leur type, y entrent. Un champ inattendu est refuse plutot
    qu'ignore, pour qu'une faute de frappe dans un client d'API se voie.
    """
    nom = _texte(v.get("nom"), "le nom de la vue", 80, obligatoire=True)
    ident = v.get("id")
    if ident in (None, ""):
        ident = None
    else:
        try:
            ident = int(ident)
        except (TypeError, ValueError):
            raise VenteInvalide("identifiant de vue illisible") from None
    d = v.get("definition")
    if not isinstance(d, dict):
        raise VenteInvalide("la definition d'une vue est un objet")
    connus = {"mode", "niveau", "texte", "perte", "alertes", "tri", "sens",
              *LISTES_VUE}
    inconnus = sorted(set(d) - connus)
    if inconnus:
        raise VenteInvalide("champ de vue inconnu : " + ", ".join(inconnus))
    propre = {}
    if d.get("mode") is not None:
        if d["mode"] not in MODES_VUE:
            raise VenteInvalide("mode inconnu : attendu " + ", ".join(MODES_VUE))
        propre["mode"] = d["mode"]
    if d.get("niveau") is not None:
        if d["niveau"] not in NIVEAUX_VUE:
            raise VenteInvalide("niveau inconnu")
        propre["niveau"] = d["niveau"]
    texte = _texte(d.get("texte"), "le texte du filtre", 190)
    if texte:
        propre["texte"] = texte
    for k in ("perte", "alertes"):
        if d.get(k) not in (None, True, False):
            raise VenteInvalide("{} attend vrai ou faux".format(k))
        if d.get(k):
            propre[k] = True
    if d.get("tri") is not None:
        tri = str(d["tri"])
        if len(tri) > 40 or not tri.replace("_", "").isalnum():
            raise VenteInvalide("colonne de tri illisible")
        propre["tri"] = tri
        propre["sens"] = -1 if d.get("sens") == -1 else 1
    for k in LISTES_VUE:
        lst = d.get(k)
        if lst in (None, []):
            continue
        if not isinstance(lst, list) or len(lst) > 300:
            raise VenteInvalide("{} attend une liste (300 au plus)".format(k))
        propre[k] = sorted({_texte(x, k, 190, obligatoire=True) for x in lst})
    texte_json = json.dumps(propre, ensure_ascii=False, sort_keys=True)
    if len(texte_json) > 16000:
        raise VenteInvalide("definition de vue trop volumineuse")
    return ident, nom, bool(v.get("partagee", True)), propre


def _vue_lisible(r):
    try:
        definition = json.loads(r["definition"] or "{}")
    except ValueError:
        definition = {}
    return {"id": r["id"], "nom": r["nom"], "partagee": bool(r["partagee"]),
            "definition": definition, "cree_par": r["cree_par"],
            "maj_le": r["maj_le"].isoformat() if r.get("maj_le") else None}


def lister_vues(conn, utilisateur):
    """Les vues partagees, et celles de l'utilisateur."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM vue_rentabilite WHERE actif = 1 "
                        "AND (partagee = 1 OR cree_par = %s) ORDER BY nom",
                        (utilisateur,))
            return [_vue_lisible(r) for r in cur.fetchall()]
    except pymysql.Error as exc:
        if (exc.args[0] if exc.args else 0) == 1146:
            return []
        raise _traduit(exc, "la lecture des vues") from exc


def _vue_modifiable(cur, ident, par, admin):
    cur.execute("SELECT * FROM vue_rentabilite WHERE id = %s AND actif = 1 "
                "FOR UPDATE", (ident,))
    avant = cur.fetchone()
    if not avant:
        raise VenteInvalide("aucune vue active sous ce numero")
    if avant["cree_par"] != par and not admin:
        raise SaisieInterdite("seul l'auteur de la vue ({}) ou un admin la "
                            "modifie".format(avant["cree_par"]))
    return avant


def enregistrer_vue(conn, vue, par, admin=False):
    ident, nom, partagee, definition = verifier_vue(vue)
    texte = json.dumps(definition, ensure_ascii=False, sort_keys=True)
    maintenant = dt.datetime.now()
    try:
        with conn.cursor() as cur:
            if ident is None:
                cur.execute(
                    "INSERT INTO vue_rentabilite (nom, definition, partagee, "
                    "actif, cree_le, cree_par) VALUES (%s, %s, %s, 1, %s, %s)",
                    (nom, texte, int(partagee), maintenant, par))
                ident, avant = cur.lastrowid, None
            else:
                avant = _vue_modifiable(cur, ident, par, admin)
                cur.execute(
                    "UPDATE vue_rentabilite SET nom = %s, definition = %s, "
                    "partagee = %s, maj_le = %s, maj_par = %s WHERE id = %s",
                    (nom, texte, int(partagee), maintenant, par, ident))
            journaliser(cur, par, "vue", "vue {}".format(ident),
                        avant and {"nom": avant["nom"],
                                   "definition": avant["definition"]},
                        {"nom": nom, "definition": texte})
    except VenteInvalide:
        conn.rollback()
        raise
    except pymysql.Error as exc:
        conn.rollback()
        raise _traduit(exc, "l'enregistrement de la vue") from exc
    conn.commit()
    return {"id": ident, "nom": nom, "partagee": partagee,
            "definition": definition, "cree_par": avant["cree_par"]
            if avant else par, "cree": avant is None}


def retirer_vue(conn, ident, par, admin=False):
    try:
        ident = int(ident)
    except (TypeError, ValueError):
        raise VenteInvalide("identifiant de vue illisible") from None
    try:
        with conn.cursor() as cur:
            _vue_modifiable(cur, ident, par, admin)
            cur.execute("UPDATE vue_rentabilite SET actif = 0, maj_le = %s, "
                        "maj_par = %s WHERE id = %s",
                        (dt.datetime.now(), par, ident))
            journaliser(cur, par, "retrait", "vue {}".format(ident),
                        {"actif": 1}, {"actif": 0})
    except VenteInvalide:
        conn.rollback()
        raise
    except pymysql.Error as exc:
        conn.rollback()
        raise _traduit(exc, "le retrait de la vue") from exc
    conn.commit()
    return True

