"""
Les vues de rentabilite enregistrees : des filtres nommes, partages ou non.
"""

from __future__ import annotations

import datetime as dt
import json
import re

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

# Tableau de bord : les colonnes du tableau, niveau par niveau, et les
# panneaux du mode graphiques. La page ne connait que ces formes ; un
# identifiant de colonne ou de mesure qu'elle ne connait pas est ignore a
# l'affichage, d'ou un simple controle de forme ici.
NIVEAUX_DONNEES = ("cluster", "serveur", "vm", "element")
TYPES_PANNEAU = ("figure", "barres", "chiffre", "tableau")
FIGURES_PANNEAU = ("roi", "capa", "clients", "ram")
GROUPES_PANNEAU = ("cluster", "serveur", "datacenter", "client", "role")
PANNEAUX_MAX = 24
COLONNES_MAX = 40
_ID_VUE = re.compile(r"^[a-z][a-z0-9_]{0,29}$")


def _identifiant(v, quoi):
    if not isinstance(v, str) or not _ID_VUE.match(v):
        raise VenteInvalide("{} illisible".format(quoi))
    return v


def verifier_colonnes(d):
    """{niveau: [colonne, ...]} : l'ordre est celui de l'affichage."""
    if not isinstance(d, dict):
        raise VenteInvalide("colonnes attend un objet par niveau")
    propre = {}
    for niveau, ids in d.items():
        if niveau not in NIVEAUX_VUE:
            raise VenteInvalide("niveau de colonnes inconnu : " + str(niveau)[:20])
        if not isinstance(ids, list) or not ids or len(ids) > COLONNES_MAX:
            raise VenteInvalide("colonnes : une liste de 1 a {} colonnes".format(
                COLONNES_MAX))
        vus = []
        for i in ids:
            i = _identifiant(i, "colonne")
            if i not in vus:
                vus.append(i)
        propre[niveau] = vus
    return propre


def verifier_panneau(p):
    """Un panneau du tableau de bord, normalise comme la page l'ecrit."""
    if not isinstance(p, dict):
        raise VenteInvalide("un panneau est un objet")
    connus = {"type", "figure", "titre", "niveau", "mesure", "groupe", "top",
              "largeur"}
    inconnus = sorted(set(p) - connus)
    if inconnus:
        raise VenteInvalide("champ de panneau inconnu : " + ", ".join(inconnus))
    t = p.get("type")
    if t not in TYPES_PANNEAU:
        raise VenteInvalide("type de panneau inconnu : attendu "
                            + ", ".join(TYPES_PANNEAU))
    propre = {"type": t, "largeur": 2 if p.get("largeur") == 2 else 1}
    titre = _texte(p.get("titre"), "le titre du panneau", 80)
    if titre:
        propre["titre"] = titre
    if t == "figure":
        if p.get("figure") not in FIGURES_PANNEAU:
            raise VenteInvalide("figure inconnue : attendu "
                                + ", ".join(FIGURES_PANNEAU))
        propre["figure"] = p["figure"]
        return propre
    if p.get("niveau") not in NIVEAUX_DONNEES:
        raise VenteInvalide("niveau de panneau inconnu")
    propre["niveau"] = p["niveau"]
    propre["mesure"] = _identifiant(p.get("mesure"), "mesure")
    if p.get("groupe") not in (None, ""):
        if p["groupe"] not in GROUPES_PANNEAU:
            raise VenteInvalide("regroupement inconnu : attendu "
                                + ", ".join(GROUPES_PANNEAU))
        propre["groupe"] = p["groupe"]
    if t != "chiffre":
        top = p.get("top", 10)
        if isinstance(top, bool) or not isinstance(top, int) or not 1 <= top <= 100:
            raise VenteInvalide("nombre de lignes du panneau : de 1 a 100")
        propre["top"] = top
    return propre


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
              "colonnes", "panneaux", *LISTES_VUE}
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
    if d.get("colonnes") not in (None, {}):
        propre["colonnes"] = verifier_colonnes(d["colonnes"])
    if d.get("panneaux") is not None:
        lst = d["panneaux"]
        if not isinstance(lst, list) or len(lst) > PANNEAUX_MAX:
            raise VenteInvalide("panneaux : une liste de {} au plus".format(
                PANNEAUX_MAX))
        propre["panneaux"] = [verifier_panneau(p) for p in lst]
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

