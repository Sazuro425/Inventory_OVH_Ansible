#!/usr/bin/env python3
"""
Rentabilite du parc : un arbre de cartes, chacune cumulant ses enfants.

    cluster -> serveur -> VM -> elements declares (site, vhost, domaine...)

Chaque carte a une valeur propre — le cout OVH d'un serveur, les contrats
d'une VM, le prix d'un element — que l'on peut corriger a la main, et cumule
celle de ses enfants :

    PA  = PA propre + somme des PA des enfants
    CA  = PV propre + somme des PV des enfants
    ROI = CA - PA

La regle est la meme a chaque etage : un element declare sous un cluster, un
vhost sous une VM ou un domaine sous ce vhost remontent de la meme facon.
Tout est ramene au mois, comme le cout OVH.

Tout se calcule ici, sans base : calculer() prend le snapshot et les saisies,
et rend l'arbre. Le service web, l'export CSV et tests_rentabilite.py lisent
le meme resultat — il n'y a qu'un calcul, donc qu'un chiffre.

Ce que vaut le ROI par serveur : les VM sont rattachees au noeud sur lequel
elles tournent au moment de la collecte. Dans un cluster, une VM migre ; le
ROI d'un serveur est donc une photo, celui du cluster est le chiffre stable.
Un serveur a perte dans un cluster rentable signale une repartition, pas une
perte.
"""

from __future__ import annotations

import csv
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import infra_inventory as orch  # noqa: E402

# Seuils de capacite. La RAM ne se partage pas : au-dela de 100 % allouee,
# demarrer toutes les VM fait swapper ou tuer l'hote. Le vCPU, lui, se
# partage ; 4 vCPU par thread reste courant pour du web, au-dela on le signale.
RAM_TENDUE = 0.85
VCPU_PAR_THREAD_MAX = 4.0

PROFONDEUR_MAX = 12   # garde-fou : une chaine d'elements plus longue est une erreur


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def naturel(texte):
    """Clef de tri qui range prod-2 avant prod-10."""
    return [int(t) if t.isdigit() else t
            for t in re.split(r"(\d+)", (texte or "").lower())]


def mensuel(prix, periode):
    prix = _num(prix) or 0.0
    return prix / 12 if periode == "an" else prix


def _carte(type_, cle, nom):
    return {
        "type": type_, "cle": cle, "nom": nom,
        # la valeur propre de la carte, apres correction
        "pa_propre": 0.0, "pv_propre": 0.0,
        # ce que la collecte ou les contrats donnaient, avant correction
        "pa_auto": None, "pv_auto": None,
        "corrections": {},
        # les cumuls, remplis par _cumuler()
        "pa": 0.0, "pv": 0.0, "roi": 0.0,
        "infos": {}, "alertes": [], "capacite": None,
        "enfants": [],
    }


def _corriger(carte, champ, auto, surcharges):
    """Applique une correction a la valeur propre. Renvoie la valeur retenue."""
    s = surcharges.get((carte["type"], carte["cle"], champ))
    if s is None:
        return auto
    valeur = _num(s.get("valeur"))
    if valeur is None:
        return auto
    carte["corrections"][champ] = {
        "valeur": valeur, "auto": auto, "motif": s.get("motif") or "",
        "par": s.get("maj_par") or "", "le": str(s.get("maj_le") or ""),
    }
    return valeur


# ---------------------------------------------------------------------------
# Capacite
# ---------------------------------------------------------------------------

def _capacite(phys, vms):
    """Physique contre alloue. phys : vcpu, ram_mb, disque_gb (None si inconnu)."""
    alloue = {
        "vcpu": sum(int(_num(v.get("cpus")) or 0) for v in vms),
        "ram_mb": sum(int(_num(v.get("ram_mb")) or 0) for v in vms),
        "disque_gb": round(sum(_num(v.get("disk_gb")) or 0 for v in vms), 1),
        "ram_mb_en_marche": sum(int(_num(v.get("ram_mb")) or 0) for v in vms
                                if v.get("status") == "running"),
    }
    cap = {"physique": phys, "alloue": alloue}
    if phys.get("ram_mb"):
        cap["ram_pct"] = round(100.0 * alloue["ram_mb"] / phys["ram_mb"], 1)
        cap["ram_libre_mb"] = phys["ram_mb"] - alloue["ram_mb"]
    if phys.get("vcpu"):
        cap["vcpu_ratio"] = round(alloue["vcpu"] / phys["vcpu"], 2)
    if phys.get("disque_gb"):
        cap["disque_pct"] = round(100.0 * alloue["disque_gb"] / phys["disque_gb"], 1)
    return cap


def _alertes_capacite(cap):
    out = []
    pct = cap.get("ram_pct")
    if pct is not None and pct > 100:
        out.append("RAM surréservée ({:.0f} % allouée)".format(pct))
    elif pct is not None and pct >= RAM_TENDUE * 100:
        out.append("RAM tendue ({:.0f} % allouée)".format(pct))
    ratio = cap.get("vcpu_ratio")
    if ratio is not None and ratio > VCPU_PAR_THREAD_MAX:
        out.append("vCPU chargés ({:.1f} par thread)".format(ratio))
    return out


def _somme_capacites(caps):
    caps = [c for c in caps if c]
    if not caps:
        return None
    phys = {k: sum(c["physique"].get(k) or 0 for c in caps) or None
            for k in ("vcpu", "ram_mb", "disque_gb")}
    tot = {"physique": phys, "alloue": {
        k: sum(c["alloue"][k] for c in caps)
        for k in ("vcpu", "ram_mb", "disque_gb", "ram_mb_en_marche")}}
    if phys["ram_mb"]:
        tot["ram_pct"] = round(100.0 * tot["alloue"]["ram_mb"] / phys["ram_mb"], 1)
        tot["ram_libre_mb"] = phys["ram_mb"] - tot["alloue"]["ram_mb"]
    if phys["vcpu"]:
        tot["vcpu_ratio"] = round(tot["alloue"]["vcpu"] / phys["vcpu"], 2)
    if phys["disque_gb"]:
        tot["disque_pct"] = round(
            100.0 * tot["alloue"]["disque_gb"] / phys["disque_gb"], 1)
    return tot


# ---------------------------------------------------------------------------
# Le calcul
# ---------------------------------------------------------------------------

def calculer(data, ventes=(), elements=(), surcharges=(), roles=(),
             catalogue=()):
    """Construit l'arbre de rentabilite.

    data       : le snapshot (servers, probes, guests, costs)
    ventes     : contrats actifs (v_vente_active)
    elements   : elements declares actifs (table element)
    surcharges : corrections dont la valeur n'est pas nulle (table surcharge)
    roles      : roles poses sur les clusters, serveurs et VM (role_carte)
    catalogue  : les roles connus, code et libelle (table role)
    """
    ventes, elements, surcharges = list(ventes), list(elements), list(surcharges)
    role_de = {(r["cible_type"], r["cible_ref"]): r["role"]
               for r in roles if r.get("role")}
    libelle_role = {r["code"]: r["libelle"] for r in catalogue}
    serveurs = data.get("servers") or []
    probes = data.get("probes") or []
    invites = data.get("guests") or []
    couts = {c.get("ovh_name"): c for c in (data.get("costs") or [])}
    probe_de = {p["host"]: p for p in probes}
    corr = {(s["cible_type"], s["cible_ref"], s["champ"]): s
            for s in surcharges if s.get("valeur") not in (None, "")}
    devises = set()

    nb_corrections = len(corr)

    # -- serveurs, et le cluster de chacun ----------------------------------
    clusters = {}
    carte_srv = {}            # ovh_name -> carte
    srv_par_reverse = {}      # reverse -> carte
    for srv in serveurs:
        ovh = srv.get("ovh_name") or srv.get("reverse") or ""
        c = _carte("serveur", ovh, srv.get("reverse") or ovh)
        probe = probe_de.get(srv.get("reverse"))
        injoignable = probe is None or bool(probe.get("error"))
        pve = None if injoignable else probe.get("cluster")
        if pve and pve.startswith("standalone:"):
            pve = None

        # le cluster : corrige a la main, sinon celui de Proxmox, sinon le
        # serveur forme a lui seul un groupe d'un membre
        manuel = corr.get(("serveur", ovh, "cluster"))
        if manuel:
            cle_cl, nom_cl = manuel["valeur"], manuel["valeur"]
            c["corrections"]["cluster"] = {
                "valeur": manuel["valeur"], "auto": pve,
                "motif": manuel.get("motif") or "",
                "par": manuel.get("maj_par") or "",
                "le": str(manuel.get("maj_le") or "")}
        elif pve:
            cle_cl, nom_cl = pve, pve
        else:
            cle_cl, nom_cl = "seul:" + ovh, srv.get("reverse") or ovh

        cout = couts.get(srv.get("ovh_name")) or {}
        auto = _num(cout.get("price_month"))
        if cout.get("price_currency"):
            devises.add(cout["price_currency"])
        c["pa_auto"] = auto
        c["pa_propre"] = _corriger(c, "prix_achat", auto, corr) or 0.0
        c["pv_propre"] = _corriger(c, "prix_vente", None, corr) or 0.0
        if auto is None and "prix_achat" not in c["corrections"]:
            c["alertes"].append("aucun tarif OVH relevé : PA sous-estimé")
        if injoignable:
            c["alertes"].append("hyperviseur injoignable : VM inconnues, "
                                "CA sous-estimé")

        hw = (probe or {}).get("hw") or {}
        c["infos"] = {
            "ovh_name": srv.get("ovh_name"), "reverse": srv.get("reverse"),
            "offre": (srv.get("offer") or "").split("|")[0].strip() or None,
            "datacenter": (srv.get("datacenter") or "").upper() or None,
            "expiration": srv.get("expiration") or None,
            "noeud": None if injoignable else probe.get("node"),
            "cluster_pve": pve,
            "joignable": not injoignable,
        }
        c["_phys"] = None if injoignable else {
            "vcpu": _num(hw.get("cpu_logical")) or _num(srv.get("cpu_threads")),
            "ram_mb": _num(hw.get("ram_total_mb")) or _num(srv.get("ram_mb")),
            "disque_gb": _num(hw.get("disk_total_gb")),
        }
        c["_vms"] = []
        c["_cle_cluster"] = cle_cl

        cl = clusters.get(cle_cl)
        if cl is None:
            cl = clusters[cle_cl] = _carte("cluster", cle_cl, nom_cl)
            cl["infos"] = {"seul": cle_cl.startswith("seul:"),
                           "manuel": bool(manuel), "datacenters": []}
        if manuel:
            cl["infos"]["manuel"] = True
        dc = c["infos"]["datacenter"]
        if dc and dc not in cl["infos"]["datacenters"]:
            cl["infos"]["datacenters"].append(dc)
        cl["enfants"].append(c)
        carte_srv[ovh] = c
        if srv.get("reverse"):
            srv_par_reverse[srv["reverse"]] = c

    # -- VM, sous le noeud qui les fait tourner -------------------------------
    hote = orch.hote_des_invites(probes, invites)
    vm_par_ref = {}           # cluster/vmid -> carte : la designation stable
    vm_par_nom = {}           # nom -> [cartes] : les saisies anterieures
    sans_serveur = 0
    for g in invites:
        parent = srv_par_reverse.get(hote[id(g)])
        if parent is None:
            sans_serveur += 1
            continue
        nom = g.get("name") or "vm-{}".format(g.get("vmid"))
        c = _carte("vm", orch.ref_vm(g), nom)
        c["infos"] = {
            "vmid": g.get("vmid"), "type": (g.get("type") or "").lower(),
            "statut": g.get("status") or None, "os": g.get("os") or None,
            "ip": (g.get("ipv4") or "").split(" ")[0] or None,
            "noeud": g.get("node"), "cpus": _num(g.get("cpus")),
            "ram_mb": _num(g.get("ram_mb")), "disque_gb": _num(g.get("disk_gb")),
            "contrats": [],
        }
        c["_cluster"] = parent["_cle_cluster"]
        c["_pve_cluster"] = g.get("cluster")
        parent["enfants"].append(c)
        parent["_vms"].append(g)
        vm_par_ref[c["cle"]] = c
        vm_par_nom.setdefault(nom, []).append(c)
    for lot in vm_par_nom.values():
        lot.sort(key=lambda c: (c["_cluster"], c["infos"]["vmid"] or 0))

    def vm_pour(designation, cluster=None):
        """La VM designee par sa reference cluster/vmid — ce qu'ecrit la
        page — ou, pour une saisie anterieure a la migration 0002, par son
        nom. Deux VM homonymes : le cluster indique tranche, sinon la premiere
        dans l'ordre des clusters — toujours la meme, pour qu'un contrat ne
        compte qu'une fois.
        """
        designation = designation or ""
        if "/" in designation:
            return vm_par_ref.get(designation)
        lot = vm_par_nom.get(designation) or []
        if cluster:
            for c in lot:
                if cluster in (c["_cluster"], c["_pve_cluster"]):
                    return c
        return lot[0] if lot else None

    # -- ventes ---------------------------------------------------------------
    orphelins = []
    for v in ventes:
        c = vm_pour(v.get("vm_ref") or v.get("vm_name"), v.get("cluster_name"))
        if v.get("devise"):
            devises.add(v["devise"])
        m = _num(v.get("vente_mois"))
        if m is None:
            m = mensuel(v.get("prix_vente"), v.get("periode"))
        if c is None:
            orphelins.append({"genre": "contrat", "nom": v.get("vm_name"),
                              "contrat": v.get("contrat"),
                              "client": v.get("client"), "pv": m, "pa": 0.0,
                              "motif": "VM introuvable dans l'inventaire"})
            continue
        c["pv_auto"] = (c["pv_auto"] or 0.0) + m
        c["infos"]["contrats"].append({
            "contrat": v.get("contrat"), "client": v.get("client"),
            "prix_vente": _num(v.get("prix_vente")), "periode": v.get("periode"),
            "vente_mois": m, "devise": v.get("devise")})

    # Une correction ou un role saisi par nom, avant la migration 0002, vise
    # la meme VM que les contrats : un seul des homonymes. On le recopie sous
    # sa reference ; une designation qui ne mene a aucune VM est signalee
    # plutot que perdue en silence.
    sans_cible = 0
    for (t, ref, champ), s in list(corr.items()):
        if t != "vm":
            continue
        c = vm_pour(ref)
        if c is None:
            sans_cible += 1
        elif "/" not in ref:
            corr.setdefault(("vm", c["cle"], champ), s)
    for (t, ref), code in list(role_de.items()):
        if t == "vm" and "/" not in ref:
            c = vm_pour(ref)
            if c is not None:
                role_de.setdefault(("vm", c["cle"]), code)
    for c in vm_par_ref.values():
        c["pv_propre"] = _corriger(c, "prix_vente", c["pv_auto"], corr) or 0.0
        c["pa_propre"] = _corriger(c, "prix_achat", None, corr) or 0.0
    for cl in clusters.values():
        cl["pa_propre"] = _corriger(cl, "prix_achat", None, corr) or 0.0
        cl["pv_propre"] = _corriger(cl, "prix_vente", None, corr) or 0.0

    # -- elements declares ----------------------------------------------------
    cartes_el = {}
    for e in elements:
        c = _carte("element", str(e["id"]), e.get("nom") or "")
        c["pa_propre"] = mensuel(e.get("prix_achat"), e.get("periode"))
        c["pv_propre"] = mensuel(e.get("prix_vente"), e.get("periode"))
        if e.get("devise"):
            devises.add(e["devise"])
        c["infos"] = {k: e.get(k) for k in (
            "contrat", "client", "note", "periode", "parent_type",
            "parent_ref")}
        c["infos"]["prix_vente"] = _num(e.get("prix_vente"))
        c["infos"]["prix_achat"] = _num(e.get("prix_achat"))
        c["_e"] = e
        cartes_el[c["cle"]] = c

    def racine_ok(c):
        """Une chaine d'elements doit finir sur une carte du parc, sans boucle."""
        vus = set()
        while c["_e"]["parent_type"] == "element":
            if c["cle"] in vus or len(vus) > PROFONDEUR_MAX:
                return False
            vus.add(c["cle"])
            c = cartes_el.get(str(c["_e"]["parent_ref"]))
            if c is None:
                return False
        return True

    for c in sorted(cartes_el.values(), key=lambda c: c["nom"].lower()):
        e = c["_e"]
        t, ref = e.get("parent_type"), str(e.get("parent_ref") or "")
        parent = None
        if t == "cluster":
            parent = clusters.get(ref)
        elif t == "serveur":
            parent = carte_srv.get(ref)
        elif t == "vm":
            parent = vm_pour(ref)
        elif t == "element" and racine_ok(c):
            parent = cartes_el.get(ref)
        if parent is None:
            orphelins.append({"genre": "élément", "nom": c["nom"],
                              "contrat": e.get("contrat"),
                              "client": e.get("client"), "id": e.get("id"),
                              "pv": c["pv_propre"], "pa": c["pa_propre"],
                              "motif": "parent introuvable ({} {})".format(
                                  t, ref)})
            continue
        parent["enfants"].append(c)

    # -- roles ----------------------------------------------------------------
    # Un element porte le sien ; les autres cartes le recoivent de role_carte.
    # Deux VM homonymes : le role va a celle qui porte les contrats.
    def attribuer(c):
        if c["type"] == "element":
            code = c["_e"].get("role")
        else:
            code = role_de.get((c["type"], c["cle"]))
        c["infos"]["role"] = code or None
        # un role retire du catalogue reste lisible par son code
        c["infos"]["role_libelle"] = libelle_role.get(code, code) if code else None
        for e in c["enfants"]:
            attribuer(e)

    for cl in clusters.values():
        attribuer(cl)

    # -- capacite et cumuls ---------------------------------------------------
    for c in carte_srv.values():
        if c["_phys"] is not None:
            c["capacite"] = _capacite(c["_phys"], c["_vms"])
            c["alertes"].extend(_alertes_capacite(c["capacite"]))
    for cl in clusters.values():
        cl["capacite"] = _somme_capacites(
            [s["capacite"] for s in cl["enfants"] if s["type"] == "serveur"])
        if cl["capacite"]:
            cl["alertes"].extend(_alertes_capacite(cl["capacite"]))
        sans = sum(1 for s in cl["enfants"] if s["type"] == "serveur"
                   and s["pa_auto"] is None
                   and "prix_achat" not in s["corrections"])
        if sans:
            cl["alertes"].append("{} serveur(s) sans tarif : PA sous-estimé"
                                 .format(sans))

    def cumuler(c, prof=0):
        pa, pv = c["pa_propre"], c["pv_propre"]
        for e in c["enfants"]:
            if prof < PROFONDEUR_MAX:
                cumuler(e, prof + 1)
            pa += e["pa"]
            pv += e["pv"]
        c["pa"], c["pv"] = round(pa, 2), round(pv, 2)
        c["roi"] = round(pv - pa, 2)

    def nettoyer(c):
        for k in [k for k in c if k.startswith("_")]:
            del c[k]
        c["enfants"].sort(key=lambda e: (
            {"serveur": 0, "vm": 1, "element": 2}.get(e["type"], 3),
            e["infos"].get("vmid") or 0 if e["type"] == "vm" else 0,
            naturel(e["nom"])))
        for e in c["enfants"]:
            nettoyer(e)

    arbre = sorted(clusters.values(), key=lambda c: (
        c["infos"]["seul"], naturel(c["nom"])))
    for cl in arbre:
        cumuler(cl)
        nettoyer(cl)

    # -- le total -----------------------------------------------------------
    tous_srv = list(carte_srv.values())
    # Hors parc : contrats sans VM, elements sans parent. Ils sont factures
    # ou payes quand meme, donc comptes dans le total ; isoles a part pour
    # que l'ecart avec la somme des clusters se lise au lieu de s'expliquer.
    hors_pv = round(sum(o["pv"] for o in orphelins), 2)
    hors_pa = round(sum(o["pa"] for o in orphelins), 2)
    pa = round(sum(c["pa"] for c in arbre) + hors_pa, 2)
    pv = round(sum(c["pv"] for c in arbre) + hors_pv, 2)
    alertes = []
    if len(devises - {"EUR"}) > 0:
        alertes.append("devises mêlées ({}) : les montants sont sommés sans "
                       "conversion".format(", ".join(sorted(devises))))
    if sans_serveur:
        alertes.append("{} VM sur un hôte absent de la liste OVH, non comptées"
                       .format(sans_serveur))
    if sans_cible:
        alertes.append("{} correction(s) de VM ne visent plus aucune VM du "
                       "parc : supprimée, ou recréée sous un autre vmid"
                       .format(sans_cible))
    globale = {
        "pa": pa, "pv": pv, "roi": round(pv - pa, 2),
        "pv_hors_parc": hors_pv, "pa_hors_parc": hors_pa,
        "nb_clusters": sum(1 for c in arbre if not c["infos"]["seul"]),
        "nb_groupes": len(arbre),
        "nb_serveurs": len(tous_srv),
        "nb_vm": sum(len(v) for v in vm_par_nom.values()),
        "nb_elements": len(cartes_el),
        "nb_contrats": len(ventes),
        "nb_orphelins": len(orphelins),
        "nb_corrections": nb_corrections,
        "groupes_a_perte": sum(1 for c in arbre if c["roi"] < 0),
        "serveurs_a_perte": sum(1 for c in tous_srv if c["roi"] < 0),
        "serveurs_sans_tarif": sum(
            1 for c in tous_srv if c["pa_auto"] is None
            and "prix_achat" not in c["corrections"]),
        "capacite": _somme_capacites([c["capacite"] for c in tous_srv]),
        "alertes": alertes,
    }
    return {"globale": globale, "arbre": arbre, "orphelins": orphelins}


# ---------------------------------------------------------------------------
# Export CSV
# ---------------------------------------------------------------------------

COLONNES = [
    "Niveau", "Type", "Nom", "Chemin", "Cluster", "Serveur", "VM",
    "Rôle", "Contrat", "Client",
    "PA propre (€/mois)", "PV propre (€/mois)",
    "PA cumulé (€/mois)", "CA cumulé (€/mois)", "ROI (€/mois)",
    "PA collecté (€/mois)", "Correction", "Motif de la correction",
    "vCPU alloués", "vCPU physiques", "RAM allouée (Go)", "RAM physique (Go)",
    "RAM allouée (%)", "Disque alloué (Go)", "Disque brut (Go)",
    "Datacenter", "Nœud PVE", "Alertes",
]

LIBELLE_TYPE = {"cluster": "Cluster", "serveur": "Serveur", "vm": "VM",
                "element": "Élément"}


def texte_sur(v):
    """Neutralise l'injection de formule.

    Excel et LibreOffice executent une cellule qui commence par = + - @ ou
    une tabulation : un nom de site saisi "=HYPERLINK(...)" deviendrait un
    lien ou une commande a l'ouverture du fichier par la compta. Une
    apostrophe en tete force la lecture comme texte. Ne s'applique qu'au
    texte : un montant negatif doit rester un nombre.
    """
    if v is None:
        return ""
    v = str(v).replace("\r", " ").replace("\n", " ")
    if v and v[0] in "=+-@\t":
        return "'" + v
    return v


def nombre(v, dec=2):
    """Virgule decimale, sans separateur de milliers : Excel et Calc en
    francais le lisent comme un nombre, ce qui permet de refaire des sommes."""
    if v is None or v == "":
        return ""
    return ("{:.%df}" % dec).format(float(v)).replace(".", ",")


def _go(mb):
    return None if mb in (None, "") else float(mb) / 1024


def lignes(resultat):
    """L'arbre aplati, dans l'ordre de lecture, une carte par ligne."""
    out = []

    def visiter(c, niveau, chemin, cluster, serveur, vm):
        nom = c["nom"]
        if c["type"] == "cluster":
            cluster = "" if c["infos"].get("seul") else nom
        elif c["type"] == "serveur":
            serveur = nom
        elif c["type"] == "vm":
            vm = nom
        chemin = chemin + [nom]
        corr = c["corrections"]
        cap = c.get("capacite") or {}
        phys, all_ = cap.get("physique") or {}, cap.get("alloue") or {}
        pa_auto = c["pa_auto"]
        out.append({
            "Niveau": str(niveau),
            "Type": LIBELLE_TYPE.get(c["type"], c["type"]),
            # l'indentation rend l'arbre lisible a l'ouverture ; Niveau
            # permet de filtrer sans elle
            "Nom": "    " * niveau + texte_sur(nom),
            "Chemin": texte_sur(" > ".join(chemin)),
            "Cluster": texte_sur(cluster), "Serveur": texte_sur(serveur),
            "VM": texte_sur(vm),
            "Rôle": texte_sur(c["infos"].get("role_libelle")),
            "Contrat": texte_sur(c["infos"].get("contrat") or ", ".join(
                x["contrat"] for x in c["infos"].get("contrats") or [])),
            "Client": texte_sur(c["infos"].get("client") or ", ".join(sorted({
                x["client"] for x in c["infos"].get("contrats") or []
                if x.get("client")}))),
            "PA propre (€/mois)": nombre(c["pa_propre"]),
            "PV propre (€/mois)": nombre(c["pv_propre"]),
            "PA cumulé (€/mois)": nombre(c["pa"]),
            "CA cumulé (€/mois)": nombre(c["pv"]),
            "ROI (€/mois)": nombre(c["roi"]),
            "PA collecté (€/mois)": nombre(pa_auto),
            "Correction": texte_sur(", ".join(
                "{} = {}".format(k, v["valeur"]) for k, v in corr.items())),
            "Motif de la correction": texte_sur(" ; ".join(
                v["motif"] for v in corr.values() if v.get("motif"))),
            "vCPU alloués": nombre(all_.get("vcpu"), 0)
                            if cap else nombre(c["infos"].get("cpus"), 0),
            "vCPU physiques": nombre(phys.get("vcpu"), 0),
            "RAM allouée (Go)": nombre(_go(all_.get("ram_mb")), 1)
                                if cap else nombre(_go(c["infos"].get("ram_mb")), 1),
            "RAM physique (Go)": nombre(_go(phys.get("ram_mb")), 1),
            "RAM allouée (%)": nombre(cap.get("ram_pct"), 1),
            "Disque alloué (Go)": nombre(all_.get("disque_gb"), 1)
                                  if cap else nombre(c["infos"].get("disque_gb"), 1),
            "Disque brut (Go)": nombre(phys.get("disque_gb"), 1),
            "Datacenter": texte_sur(c["infos"].get("datacenter") or " ".join(
                c["infos"].get("datacenters") or [])),
            "Nœud PVE": texte_sur(c["infos"].get("noeud")),
            "Alertes": texte_sur(" ; ".join(c["alertes"])),
        })
        for e in c["enfants"]:
            visiter(e, niveau + 1, chemin, cluster, serveur, vm)

    for cl in resultat["arbre"]:
        visiter(cl, 0, [], "", "", "")

    for o in resultat["orphelins"]:
        out.append({k: "" for k in COLONNES} | {
            "Niveau": "0", "Type": "Hors parc (" + o["genre"] + ")",
            "Nom": texte_sur(o["nom"]),
            "Contrat": texte_sur(o.get("contrat")),
            "Client": texte_sur(o.get("client")),
            "PV propre (€/mois)": nombre(o["pv"]),
            "PA propre (€/mois)": nombre(o["pa"]),
            "PA cumulé (€/mois)": nombre(o["pa"]),
            "CA cumulé (€/mois)": nombre(o["pv"]),
            "ROI (€/mois)": nombre(o["pv"] - o["pa"]),
            "Alertes": texte_sur(o["motif"]),
        })

    g = resultat["globale"]
    out.append({k: "" for k in COLONNES} | {
        "Niveau": "", "Type": "Total", "Nom": "TOTAL DU PARC",
        "PA cumulé (€/mois)": nombre(g["pa"]),
        "CA cumulé (€/mois)": nombre(g["pv"]),
        "ROI (€/mois)": nombre(g["roi"]),
        "Alertes": texte_sur(" ; ".join(g["alertes"])),
    })
    return out


def vers_csv(resultat):
    """Le CSV en octets : UTF-8 avec BOM, point-virgule, fins de ligne CRLF.

    Le BOM fait reconnaitre l'UTF-8 a Excel (sans lui, les accents sortent
    en mojibake) ; le point-virgule est le separateur qu'Excel et Calc
    attendent en francais, la virgule y etant la decimale.
    """
    tampon = io.StringIO()
    w = csv.DictWriter(tampon, fieldnames=COLONNES, delimiter=";",
                       lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    w.writeheader()
    w.writerows(lignes(resultat))
    return ("﻿" + tampon.getvalue()).encode("utf-8")
