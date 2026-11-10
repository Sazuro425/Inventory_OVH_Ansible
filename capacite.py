#!/usr/bin/env python3
"""
Historique et projection de capacite. Sans base : des lignes en entree, des
lignes en sortie, de quoi tester chaque regle.

    lignes_historique(arbre)   ce que la collecte inscrit, un point par carte
    series_mensuelles(lignes)  le dernier point de chaque mois, par carte
    projections(lignes, jour)  quand chaque cluster atteindra 85 % puis 100 %
                               de RAM allouee, et 4 vCPU par thread

La question a laquelle la projection repond : « a ce rythme, quand faut-il
reprendre un serveur ? ». Zabbix mesure ce que les VM consomment ; ici on
suit ce qui leur est promis, alloue, contre ce que le materiel offre.

Methode. La RAM allouee des derniers mois (90 jours) donne une pente par
moindres carres ; on la prolonge depuis la derniere valeur jusqu'aux seuils
de la capacite physique ACTUELLE. Pourquoi pas une pente du pourcentage :
ajouter un serveur fait chuter le pourcentage d'un coup, et une pente qui
traverse ce saut ne dit plus rien. La RAM allouee, elle, ne bouge qu'au gre
des VM.
"""

from __future__ import annotations

import datetime as dt

RAM_TENDUE = 85.0
RAM_PLEINE = 100.0
VCPU_PAR_THREAD_MAX = 4.0
FENETRE = dt.timedelta(days=90)
POINTS_MIN = 5
ETENDUE_MIN = dt.timedelta(days=14)
HORIZON = dt.timedelta(days=5 * 365)    # au-dela : « pas avant 5 ans »


# ---------------------------------------------------------------------------
# Ce que la collecte inscrit
# ---------------------------------------------------------------------------

def _nb_vm(c):
    return sum((1 if e["type"] == "vm" else 0) + _nb_vm(e) for e in c["enfants"])


def _point(niveau, cle, nom, pa, pv, roi, cap, nb_vm):
    phys = (cap or {}).get("physique") or {}
    alloue = (cap or {}).get("alloue") or {}
    return {
        "niveau": niveau, "cle": str(cle)[:190], "nom": str(nom)[:190],
        "pa": pa, "pv": pv, "roi": roi,
        # une capacite inconnue (hote injoignable) reste inconnue : un zero
        # ferait croire a une chute
        "ram_alloue_mb": alloue.get("ram_mb") if cap else None,
        "ram_physique_mb": phys.get("ram_mb") if cap else None,
        "vcpu_alloue": alloue.get("vcpu") if cap else None,
        "vcpu_physique": phys.get("vcpu") if cap else None,
        "nb_vm": nb_vm,
    }


def lignes_historique(resultat):
    """Les points du jour : le parc, chaque cluster, chaque serveur."""
    g = resultat["globale"]
    out = [_point("parc", "parc", "Parc entier", g["pa"], g["pv"], g["roi"],
                  g.get("capacite"), g.get("nb_vm"))]
    for cl in resultat["arbre"]:
        out.append(_point("cluster", cl["cle"], cl["nom"], cl["pa"], cl["pv"],
                          cl["roi"], cl.get("capacite"), _nb_vm(cl)))
        for s in cl["enfants"]:
            if s["type"] == "serveur":
                out.append(_point("serveur", s["cle"], s["nom"], s["pa"], s["pv"],
                                  s["roi"], s.get("capacite"), _nb_vm(s)))
    return out


# ---------------------------------------------------------------------------
# Ce que la page trace
# ---------------------------------------------------------------------------

def _jour(v):
    return v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v)[:10])


def _num(v):
    return None if v is None else float(v)


def series_mensuelles(lignes, niveau):
    """[{cle, nom, points: [{mois, pa, pv, roi, ram_alloue_mb, ...}]}] : pour
    chaque carte du niveau, le dernier point de chaque mois, dans l'ordre."""
    par_cle = {}
    for l in lignes:
        if l["niveau"] != niveau:
            continue
        j = _jour(l["jour"])
        mois = j.strftime("%Y-%m")
        serie = par_cle.setdefault(l["cle"], {"cle": l["cle"], "nom": l["nom"],
                                              "_mois": {}})
        avant = serie["_mois"].get(mois)
        if avant is None or avant[0] < j:
            serie["_mois"][mois] = (j, l)
        if j >= serie.get("_dernier", j):
            serie["nom"], serie["_dernier"] = l["nom"], j
    out = []
    for serie in par_cle.values():
        points = []
        for mois in sorted(serie["_mois"]):
            _, l = serie["_mois"][mois]
            points.append({"mois": mois, **{k: _num(l.get(k)) for k in (
                "pa", "pv", "roi", "ram_alloue_mb", "ram_physique_mb",
                "vcpu_alloue", "vcpu_physique", "nb_vm")}})
        out.append({"cle": serie["cle"], "nom": serie["nom"], "points": points})
    return sorted(out, key=lambda s: s["nom"].lower())


# ---------------------------------------------------------------------------
# La projection
# ---------------------------------------------------------------------------

def _pente(points):
    """Moindres carres : (pente par jour, nombre de points). points : [(x, y)]."""
    n = len(points)
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    var = sum((x - mx) ** 2 for x, _ in points)
    if not var:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in points) / var


def _echeance(actuel, cible, pente, aujourd_hui):
    """La date ou actuel atteint cible a la pente donnee, ou :
    "atteint" (deja au-dela), None (jamais a ce rythme, ou au-dela de
    l'horizon)."""
    if actuel >= cible:
        return "atteint"
    if pente <= 0:
        return None
    jours = (cible - actuel) / pente
    if jours > HORIZON.days:
        return None
    return (aujourd_hui + dt.timedelta(days=int(jours))).isoformat()


def projection(points, aujourd_hui):
    """La projection d'une carte. points : ses lignes d'historique."""
    debut = aujourd_hui - FENETRE
    pts = sorted((p for p in points
                  if _jour(p["jour"]) >= debut and p.get("ram_alloue_mb") is not None
                  and p.get("ram_physique_mb")), key=lambda p: _jour(p["jour"]))
    if not pts:
        return {"etat": "inconnu"}
    dernier = pts[-1]
    alloue, phys = float(dernier["ram_alloue_mb"]), float(dernier["ram_physique_mb"])
    out = {"ram_pct": round(100 * alloue / phys, 1),
           "ram_alloue_go": round(alloue / 1024, 1),
           "ram_physique_go": round(phys / 1024, 1)}
    if dernier.get("vcpu_physique"):
        out["vcpu_ratio"] = round(float(dernier["vcpu_alloue"] or 0)
                                  / float(dernier["vcpu_physique"]), 2)
    out["etat"] = ("critique" if out["ram_pct"] > RAM_PLEINE
                   else "tendu" if out["ram_pct"] >= RAM_TENDUE else "ok")
    j0 = _jour(pts[0]["jour"])
    if len(pts) < POINTS_MIN or _jour(dernier["jour"]) - j0 < ETENDUE_MIN:
        # trop peu de recul : l'etat du jour, pas de tendance
        out["tendance"] = "insuffisante"
        return out
    pente = _pente([((_jour(p["jour"]) - j0).days, float(p["ram_alloue_mb"]))
                    for p in pts])
    out["tendance"] = "hausse" if pente > 0 else ("baisse" if pente < 0 else "stable")
    out["ram_go_par_mois"] = round(pente * 30 / 1024, 1)
    out["ram_85"] = _echeance(alloue, phys * RAM_TENDUE / 100, pente, aujourd_hui)
    out["ram_100"] = _echeance(alloue, phys * RAM_PLEINE / 100, pente, aujourd_hui)
    vcpu = [p for p in pts if p.get("vcpu_physique")]
    if len(vcpu) >= POINTS_MIN:
        pv = _pente([((_jour(p["jour"]) - j0).days, float(p["vcpu_alloue"] or 0))
                     for p in vcpu])
        out["vcpu_par_mois"] = round(pv * 30, 1)
        out["vcpu_max"] = _echeance(float(vcpu[-1]["vcpu_alloue"] or 0),
                                    float(vcpu[-1]["vcpu_physique"]) * VCPU_PAR_THREAD_MAX,
                                    pv, aujourd_hui)
    return out


def _urgence(p):
    """Pour trier : deja plein d'abord, puis la date la plus proche."""
    e = p.get("ram_100")
    if e == "atteint":
        return ""
    return e or "9999"


def projections(lignes, aujourd_hui, niveau="cluster"):
    """La projection de chaque carte du niveau, la plus urgente d'abord."""
    par_cle = {}
    for l in lignes:
        if l["niveau"] == niveau:
            par_cle.setdefault(l["cle"], []).append(l)
    out = []
    for cle, pts in par_cle.items():
        nom = max(pts, key=lambda p: _jour(p["jour"]))["nom"]
        out.append(dict(projection(pts, aujourd_hui), cle=cle, nom=nom))
    return sorted(out, key=lambda p: (_urgence(p), -(p.get("ram_pct") or 0)))
