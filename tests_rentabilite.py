#!/usr/bin/env python3
"""
Contrat du calcul de rentabilite et de son export.

    python3 tests_rentabilite.py

Aucune base : rentabilite.calculer() prend un snapshot et des saisies, ces
tests lui en fabriquent. Ils fixent ce que le chiffre doit garantir — qu'un
contrat ne compte qu'une fois, qu'un enfant remonte a n'importe quelle
profondeur, qu'une correction remplace la valeur collectee sans effacer les
enfants — et pas seulement qu'un total sort.
"""

from __future__ import annotations

import csv
import io
import sys
import traceback

import generer_demo
import infra_inventory as orch
import rentabilite as R


# --------------------------------------------------------------------------
# Harnais minimal — le projet n'a pas de framework de test
# --------------------------------------------------------------------------

CAS = []


def test(libelle):
    def decorateur(fn):
        CAS.append((libelle, fn))
        return fn
    return decorateur


def egal(obtenu, attendu, quoi=""):
    if isinstance(attendu, float):
        ok = abs(float(obtenu) - attendu) < 0.005
    else:
        ok = obtenu == attendu
    if not ok:
        raise AssertionError("{} : attendu {!r}, obtenu {!r}".format(
            quoi or "valeur", attendu, obtenu))


# --------------------------------------------------------------------------
# Fabrique de parc
# --------------------------------------------------------------------------

def serveur(n, dc="gra"):
    return {"ovh_name": "ns{}.ovh.net".format(n), "reverse": "ns{}.eu".format(n),
            "datacenter": dc, "offer": "ADVANCE-2", "cpu_threads": 16,
            "ram_mb": 65536}


def sonde(n, cluster, noeud, erreur=None):
    return {"host": "ns{}.eu".format(n), "node": None if erreur else noeud,
            "cluster": None if erreur else cluster, "members": [],
            "error": erreur,
            "hw": {} if erreur else {"cpu_logical": 16, "ram_total_mb": 65536,
                                     "disk_total_gb": 1000.0}}


def invite(nom, vmid, cluster, noeud, via, ram=4096, statut="running"):
    return {"name": nom, "vmid": vmid, "cluster": cluster, "node": noeud,
            "via_host": "ns{}.eu".format(via), "cpus": 2, "ram_mb": ram,
            "disk_gb": 50.0, "status": statut, "type": "qemu"}


def cout(n, prix):
    return {"ovh_name": "ns{}.ovh.net".format(n), "price_month": prix,
            "price_currency": "EUR"}


def vente(vm, prix, contrat, periode="mois", cluster=None):
    return {"vm_name": vm, "prix_vente": prix, "contrat": contrat,
            "periode": periode, "devise": "EUR", "cluster_name": cluster}


def element(id_, parent_type, parent_ref, nom, pv=0, pa=0, periode="mois",
            role=None):
    return {"id": id_, "parent_type": parent_type, "parent_ref": parent_ref,
            "nom": nom, "prix_vente": pv, "prix_achat": pa,
            "periode": periode, "devise": "EUR", "role": role}


def role(type_, ref, code):
    return {"cible_type": type_, "cible_ref": ref, "role": code}


CATALOGUE = [{"code": "lamp", "libelle": "Serveur LAMP"},
             {"code": "site", "libelle": "Site web"},
             {"code": "proxy", "libelle": "Proxy / reverse proxy"}]


def correction(type_, ref, champ, valeur, motif="test"):
    return {"cible_type": type_, "cible_ref": ref, "champ": champ,
            "valeur": valeur, "motif": motif, "maj_par": "evan",
            "maj_le": "2026-11-09 10:00:00"}


def trouve(arbre, *noms):
    """Descend l'arbre par noms successifs."""
    niveau = arbre
    carte = None
    for nom in noms:
        carte = next((c for c in niveau if c["nom"] == nom), None)
        if carte is None:
            raise AssertionError("carte introuvable : " + " > ".join(noms))
        niveau = carte["enfants"]
    return carte


# --------------------------------------------------------------------------
# L'exemple de la demande
# --------------------------------------------------------------------------

@test("l'exemple cl-nvs-vps : trois noeuds seuls regroupes a la main")
def _():
    # trois PVE seuls dans trois datacenters, que seul le commerce regroupe
    data = {
        "servers": [serveur(1, "gra"), serveur(2, "rbx"), serveur(3, "sbg")],
        "probes": [sonde(1, "standalone:ns1.eu", "pve"),
                   sonde(2, "standalone:ns2.eu", "pve"),
                   sonde(3, "standalone:ns3.eu", "pve")],
        "guests": [invite("vps-a", 100, "standalone:ns1.eu", "pve", 1),
                   invite("vps-b", 101, "standalone:ns1.eu", "pve", 1),
                   invite("vps-c", 100, "standalone:ns2.eu", "pve", 2),
                   invite("vps-d", 101, "standalone:ns2.eu", "pve", 2),
                   invite("vps-e", 100, "standalone:ns3.eu", "pve", 3),
                   invite("vps-f", 101, "standalone:ns3.eu", "pve", 3)],
        "costs": [cout(1, 383.99), cout(2, 383.99), cout(3, 383.99)],
    }
    ventes = [vente("vps-a", 400.00, "A"), vente("vps-b", 349.99, "B"),
              vente("vps-c", 600.00, "C"), vente("vps-d", 550.21, "D"),
              vente("vps-e", 600.00, "E"), vente("vps-f", 556.73, "F")]
    corr = [correction("serveur", "ns{}.ovh.net".format(n), "cluster",
                       "cl-nvs-vps") for n in (1, 2, 3)]
    r = R.calculer(data, ventes, (), corr)

    egal(len(r["arbre"]), 1, "un seul groupe")
    cl = r["arbre"][0]
    egal(cl["nom"], "cl-nvs-vps")
    egal(cl["pa"], 1151.97, "PA du cluster")
    # la demande ecrit 3 056.96 et 1 904.99 : la somme de ses propres lignes
    # donne 3 056.93 et 1 904.96
    egal(cl["pv"], 3056.93, "CA du cluster")
    egal(cl["roi"], 1904.96, "ROI du cluster")
    egal(trouve(r["arbre"], "cl-nvs-vps", "ns1.eu")["roi"], 366.00)
    egal(trouve(r["arbre"], "cl-nvs-vps", "ns2.eu")["roi"], 766.22)
    # la demande ecrit 772.24 ; 1156.73 - 383.99 = 772.74
    egal(trouve(r["arbre"], "cl-nvs-vps", "ns3.eu")["roi"], 772.74)
    egal(trouve(r["arbre"], "cl-nvs-vps", "ns1.eu", "vps-b")["pv"], 349.99)


@test("deux noeuds seuls au nom PVE identique restent deux groupes")
def _():
    data = {"servers": [serveur(1), serveur(2)],
            "probes": [sonde(1, "standalone:ns1.eu", "pve"),
                       sonde(2, "standalone:ns2.eu", "pve")],
            "guests": [invite("a", 100, "standalone:ns1.eu", "pve", 1),
                       invite("b", 100, "standalone:ns2.eu", "pve", 2)],
            "costs": [cout(1, 100), cout(2, 100)]}
    r = R.calculer(data)
    egal(len(r["arbre"]), 2, "groupes")
    egal(sorted(len(c["enfants"][0]["enfants"]) for c in r["arbre"]), [1, 1],
         "une VM sous chaque serveur")


# --------------------------------------------------------------------------
# Rattachement des VM
# --------------------------------------------------------------------------

def cluster_trois():
    """Cluster de trois noeuds ; la collecte n'a interroge que ns1."""
    return {
        "servers": [serveur(1), serveur(2), serveur(3)],
        "probes": [sonde(1, "prod", "pve-1"), sonde(2, "prod", "pve-2"),
                   sonde(3, "prod", "pve-3")],
        "guests": [invite("web1", 100, "prod", "pve-1", 1),
                   invite("web2", 101, "prod", "pve-2", 1),
                   invite("web3", 102, "prod", "pve-3", 1),
                   invite("db1", 103, "prod", "pve-3", 1)],
        "costs": [cout(1, 100), cout(2, 100), cout(3, 100)],
    }


@test("une VM se range sous le noeud qui la fait tourner, pas le representant")
def _():
    r = R.calculer(cluster_trois(), [vente("db1", 300, "X")])
    egal([v["nom"] for v in trouve(r["arbre"], "prod", "ns3.eu")["enfants"]],
         ["web3", "db1"], "VM de ns3")
    egal(trouve(r["arbre"], "prod", "ns2.eu")["roi"], -100.0, "ns2 sans vente")
    egal(trouve(r["arbre"], "prod", "ns3.eu")["roi"], 200.0, "ns3")
    egal(r["arbre"][0]["roi"], 0.0, "cluster")


@test("un noeud injoignable : ses VM restent rattachees au representant")
def _():
    data = cluster_trois()
    data["probes"][2] = sonde(3, None, None, erreur="port 8006 ferme")
    hote = orch.hote_des_invites(data["probes"], data["guests"])
    egal(hote[id(data["guests"][3])], "ns1.eu", "repli sur via_host")
    r = R.calculer(data)
    egal(sum(len(s["enfants"]) for c in r["arbre"] for s in c["enfants"]), 4,
         "aucune VM perdue")


@test("deux VM homonymes : le contrat ne compte qu'une fois")
def _():
    data = {"servers": [serveur(1), serveur(2)],
            "probes": [sonde(1, "alpha", "a1"), sonde(2, "beta", "b1")],
            "guests": [invite("web", 100, "alpha", "a1", 1),
                       invite("web", 100, "beta", "b1", 2)],
            "costs": [cout(1, 0), cout(2, 0)]}
    r = R.calculer(data, [vente("web", 50, "X")],
                   (), [correction("vm", "web", "prix_achat", "5")])
    egal(r["globale"]["pv"], 50.0, "CA total")
    egal(r["globale"]["pa"], 5.0, "la correction ne s'applique qu'une fois")
    # le cluster indique sur la vente tranche
    r = R.calculer(data, [vente("web", 50, "X", cluster="beta")])
    egal(trouve(r["arbre"], "beta")["pv"], 50.0, "vente dans beta")
    egal(trouve(r["arbre"], "alpha")["pv"], 0.0, "rien dans alpha")


# --------------------------------------------------------------------------
# Identite des VM : cluster/vmid (migration 0002)
# --------------------------------------------------------------------------

def deux_homonymes(nom_alpha="web", nom_beta="web"):
    return {"servers": [serveur(1), serveur(2)],
            "probes": [sonde(1, "alpha", "a1"), sonde(2, "beta", "b1")],
            "guests": [invite(nom_alpha, 100, "alpha", "a1", 1),
                       invite(nom_beta, 100, "beta", "b1", 2)],
            "costs": [cout(1, 0), cout(2, 0)]}


@test("la carte d'une VM a pour cle cluster/vmid, le nom reste son libelle")
def _():
    r = R.calculer(deux_homonymes())
    a = trouve(r["arbre"], "alpha", "ns1.eu", "web")
    egal((a["cle"], a["nom"]), ("alpha/100", "web"))
    egal(trouve(r["arbre"], "beta", "ns2.eu", "web")["cle"], "beta/100")


@test("par reference, deux homonymes ont chacun leur vente, role, correction")
def _():
    v = dict(vente("web", 30, "X"), vm_ref="beta/100")
    r = R.calculer(deux_homonymes(), [v],
                   [element(1, "vm", "beta/100", "site.fr", pv=10)],
                   [correction("vm", "beta/100", "prix_achat", "4")],
                   roles=[role("vm", "alpha/100", "lamp"),
                          role("vm", "beta/100", "proxy")],
                   catalogue=CATALOGUE)
    a = trouve(r["arbre"], "alpha", "ns1.eu", "web")
    b = trouve(r["arbre"], "beta", "ns2.eu", "web")
    egal((a["pv"], b["pv"]), (0.0, 40.0), "la vente et le site vont a beta")
    egal((a["pa"], b["pa"]), (0.0, 4.0), "la correction aussi")
    egal((a["infos"]["role"], b["infos"]["role"]), ("lamp", "proxy"))


@test("une VM renommee garde ses ventes, enfants, role et correction")
def _():
    saisies = dict(
        ventes=[dict(vente("ancien-nom", 30, "X"), vm_ref="alpha/100")],
        elements=[element(1, "vm", "alpha/100", "site.fr", pv=10)],
        surcharges=[correction("vm", "alpha/100", "prix_achat", "4")],
        roles=[role("vm", "alpha/100", "lamp")], catalogue=CATALOGUE)
    r = R.calculer(deux_homonymes(nom_alpha="nouveau-nom"), **saisies)
    a = trouve(r["arbre"], "alpha", "ns1.eu", "nouveau-nom")
    egal((a["pv"], a["pa"], a["infos"]["role"]), (40.0, 4.0, "lamp"))
    egal(r["orphelins"], [], "rien ne part hors parc")


@test("une reference qui ne mene plus a rien : visible, jamais perdue")
def _():
    r = R.calculer(deux_homonymes(),
                   [dict(vente("web", 30, "X"), vm_ref="gamma/7")],
                   [element(1, "vm", "gamma/7", "site.fr", pv=10)],
                   [correction("vm", "gamma/7", "prix_achat", "4")])
    egal(sorted(o["genre"] for o in r["orphelins"]), ["contrat", "élément"])
    egal(r["globale"]["pv"], 40.0, "toujours compte dans le total")
    egal(any("ne visent plus aucune VM" in x for x in r["globale"]["alertes"]),
         True, "la correction orpheline est signalee")


# --------------------------------------------------------------------------
# Enfants declares
# --------------------------------------------------------------------------

@test("une VM LAMP et ses 4 sites a 40 EUR : 160 EUR, roles lisibles")
def _():
    data = {"servers": [serveur(1)], "probes": [sonde(1, "prod", "pve-1")],
            "guests": [invite("vm-a", 100, "prod", "pve-1", 1),
                       invite("rp1", 101, "prod", "pve-1", 1)],
            "costs": [cout(1, 90)]}
    sites = [element(i, "vm", "vm-a", "site{}.fr".format(i), pv=40, role="site")
             for i in range(1, 5)]
    r = R.calculer(data, (), sites,
                   roles=[role("vm", "vm-a", "lamp"), role("serveur",
                          "ns1.ovh.net", "proxy")],
                   catalogue=CATALOGUE)
    a = trouve(r["arbre"], "prod", "ns1.eu", "vm-a")
    egal(a["pv"], 160.0, "ce que la VM rapporte")
    egal(a["infos"]["role"], "lamp")
    egal(a["infos"]["role_libelle"], "Serveur LAMP")
    egal({e["infos"]["role_libelle"] for e in a["enfants"]}, {"Site web"})
    egal(trouve(r["arbre"], "prod", "ns1.eu")["infos"]["role"], "proxy",
         "le role de l'hyperviseur")
    egal(trouve(r["arbre"], "prod", "ns1.eu", "rp1")["infos"]["role"], None,
         "une VM sans role")
    egal(r["arbre"][0]["roi"], 70.0, "ROI du cluster : 160 - 90")
    ligne = [l for l in R.lignes(r) if l["Nom"].strip() == "vm-a"][0]
    egal(ligne["Rôle"], "Serveur LAMP", "colonne Rôle du CSV")


@test("un role retire du catalogue reste lisible par son code")
def _():
    data = {"servers": [serveur(1)], "probes": [sonde(1, "prod", "pve-1")],
            "guests": [invite("vm-a", 100, "prod", "pve-1", 1)], "costs": []}
    r = R.calculer(data, roles=[role("vm", "vm-a", "ancien")],
                   catalogue=CATALOGUE)
    egal(trouve(r["arbre"], "prod", "ns1.eu", "vm-a")["infos"]["role_libelle"],
         "ancien")


@test("deux VM homonymes : le role va a celle qui porte les contrats")
def _():
    data = {"servers": [serveur(1), serveur(2)],
            "probes": [sonde(1, "alpha", "a1"), sonde(2, "beta", "b1")],
            "guests": [invite("web", 100, "alpha", "a1", 1),
                       invite("web", 100, "beta", "b1", 2)],
            "costs": []}
    r = R.calculer(data, roles=[role("vm", "web", "lamp")], catalogue=CATALOGUE)
    egal(trouve(r["arbre"], "alpha", "ns1.eu", "web")["infos"]["role"], "lamp")
    egal(trouve(r["arbre"], "beta", "ns2.eu", "web")["infos"]["role"], None)

@test("environnement mutualise : 20 sites sous web6 remontent jusqu'au cluster")
def _():
    data = {"servers": [serveur(1)], "probes": [sonde(1, "prod", "pve-1")],
            "guests": [invite("web6", 100, "prod", "pve-1", 1)],
            "costs": [cout(1, 100)]}
    elements = [element(i, "vm", "web6", "site{}.fr".format(i), pv=15)
                for i in range(1, 21)]
    r = R.calculer(data, [vente("web6", 20, "W6")], elements)
    web6 = trouve(r["arbre"], "prod", "ns1.eu", "web6")
    egal(len(web6["enfants"]), 20, "enfants")
    egal(web6["pv_propre"], 20.0, "PV propre de la VM")
    egal(web6["pv"], 320.0, "CA cumule de la VM")
    egal(r["arbre"][0]["roi"], 220.0, "ROI du cluster")


@test("un enfant d'enfant remonte : domaine -> vhost -> VM")
def _():
    data = {"servers": [serveur(1)], "probes": [sonde(1, "prod", "pve-1")],
            "guests": [invite("mail1", 100, "prod", "pve-1", 1)],
            "costs": [cout(1, 0)]}
    elements = [element(1, "vm", "mail1", "vhost exemple", pv=10, pa=2),
                element(2, "element", "1", "exemple.fr", pv=5, pa=1),
                element(3, "element", "2", "boite contact@", pv=120, pa=12,
                        periode="an")]
    r = R.calculer(data, (), elements)
    vh = trouve(r["arbre"], "prod", "ns1.eu", "mail1", "vhost exemple")
    egal(vh["pv"], 25.0, "CA du vhost : 10 + 5 + 120/12")
    egal(vh["pa"], 4.0, "PA du vhost : 2 + 1 + 12/12")
    egal(r["globale"]["roi"], 21.0, "ROI du parc")


@test("un element declare sur le cluster s'ajoute au cluster")
def _():
    r = R.calculer(cluster_trois(), (),
                   [element(1, "cluster", "prod", "stockage Ceph", pa=90)])
    egal(r["arbre"][0]["pa"], 390.0, "PA du cluster")


@test("une boucle d'elements ne bloque pas : les deux partent hors parc")
def _():
    data = {"servers": [serveur(1)], "probes": [sonde(1, "prod", "pve-1")],
            "guests": [], "costs": [cout(1, 10)]}
    elements = [element(1, "element", "2", "a", pv=7),
                element(2, "element", "1", "b", pv=3)]
    r = R.calculer(data, (), elements)
    egal(len(r["orphelins"]), 2, "orphelins")
    egal(r["globale"]["pv_hors_parc"], 10.0, "hors parc")
    egal(r["globale"]["pv"], 10.0, "compte dans le total")


@test("contrat sans VM et element sans parent : comptes, et isoles")
def _():
    r = R.calculer(cluster_trois(), [vente("disparue", 99, "Z")],
                   [element(1, "serveur", "ns404.ovh.net", "perdu", pa=4)])
    g = r["globale"]
    egal(g["pv_hors_parc"], 99.0)
    egal(g["pa_hors_parc"], 4.0)
    egal(g["pa"], 304.0, "PA total")
    egal(g["pv"], 99.0, "CA total")


# --------------------------------------------------------------------------
# Corrections manuelles
# --------------------------------------------------------------------------

@test("corriger le PA d'un serveur remplace le cout OVH, les VM s'ajoutent")
def _():
    r = R.calculer(cluster_trois(), [vente("web1", 50, "A")],
                   [element(1, "serveur", "ns1.ovh.net", "IP failover", pa=3)],
                   [correction("serveur", "ns1.ovh.net", "prix_achat", "80",
                               "tarif negocie")])
    s = trouve(r["arbre"], "prod", "ns1.eu")
    egal(s["pa_auto"], 100.0, "cout collecte conserve")
    egal(s["pa_propre"], 80.0, "cout corrige")
    egal(s["pa"], 83.0, "plus l'element")
    egal(s["pv"], 50.0)
    egal(s["corrections"]["prix_achat"]["motif"], "tarif negocie")


@test("un serveur sans tarif mais corrige n'est plus signale sans tarif")
def _():
    data = cluster_trois()
    data["costs"] = data["costs"][:2]
    r = R.calculer(data)
    egal(r["globale"]["serveurs_sans_tarif"], 1)
    r = R.calculer(data, (), (), [correction("serveur", "ns3.ovh.net",
                                             "prix_achat", "100")])
    egal(r["globale"]["serveurs_sans_tarif"], 0)


@test("une correction levee (valeur nulle) rend la valeur collectee")
def _():
    r = R.calculer(cluster_trois(), (), (),
                   [correction("serveur", "ns1.ovh.net", "prix_achat", None)])
    egal(trouve(r["arbre"], "prod", "ns1.eu")["pa"], 100.0)


@test("un tarif annuel est ramene au mois")
def _():
    r = R.calculer(cluster_trois(), [vente("web1", 1200, "A", periode="an")])
    egal(trouve(r["arbre"], "prod", "ns1.eu", "web1")["pv"], 100.0)


# --------------------------------------------------------------------------
# Capacite
# --------------------------------------------------------------------------

@test("la capacite compte la RAM allouee et signale la surreservation")
def _():
    data = cluster_trois()
    data["guests"] = [invite("gros{}".format(i), 100 + i, "prod", "pve-1", 1,
                             ram=16384) for i in range(5)]
    r = R.calculer(data)
    s = trouve(r["arbre"], "prod", "ns1.eu")
    egal(s["capacite"]["alloue"]["ram_mb"], 81920)
    egal(s["capacite"]["ram_pct"], 125.0)
    egal(any("surréservée" in a for a in s["alertes"]), True, "alerte")
    egal(r["arbre"][0]["capacite"]["physique"]["ram_mb"], 3 * 65536.0)


# --------------------------------------------------------------------------
# Le parc de demonstration : des invariants, a l'echelle
# --------------------------------------------------------------------------

@test("parc de demo : rien de perdu, rien de compte deux fois")
def _():
    data = generer_demo.generer(30, 4, 28, 7, 0.45, 0.05, 0.25, 0.1)
    data["costs"] = [cout(0, 0) | {"ovh_name": s["ovh_name"],
                                   "price_month": 50 + i}
                     for i, s in enumerate(data["servers"])]
    ventes = [vente(g["name"], 40, "C{}".format(i))
              for i, g in enumerate(data["guests"][::2])]
    ventes.append(vente("fantome", 99, "F"))
    r = R.calculer(data, ventes)
    g = r["globale"]
    srv = [s for c in r["arbre"] for s in c["enfants"] if s["type"] == "serveur"]
    egal(len(srv), 30, "chaque serveur une fois")
    # les VM des hotes injoignables ne sont pas collectees : elles ne
    # figurent pas dans data["guests"], rien a perdre ici
    egal(sum(1 for s in srv for v in s["enfants"] if v["type"] == "vm"),
         len(data["guests"]), "chaque VM une fois")
    egal(g["pa"], float(sum(50 + i for i in range(30))), "PA")
    egal(g["pv"], 40.0 * len(ventes[:-1]) + 99, "CA, hors parc compris")
    egal(sum(c["pv"] for c in r["arbre"]) + g["pv_hors_parc"], g["pv"],
         "les groupes plus le hors parc font le total")
    # un groupe a perte n'est pas une anomalie ici : un noeud seul injoignable
    # n'a aucune VM connue et garde son cout. Ce qui doit tenir : tout groupe
    # a perte a une raison visible.
    for c in r["arbre"]:
        if c["roi"] < 0 and not c["pv"]:
            egal(bool(c["alertes"] or any(s["alertes"] for s in c["enfants"])),
                 True, "groupe a perte sans explication : " + c["nom"])


# --------------------------------------------------------------------------
# Export CSV
# --------------------------------------------------------------------------

def relire(octets):
    texte = octets.decode("utf-8")
    assert texte.startswith("﻿"), "BOM absent"
    return list(csv.DictReader(io.StringIO(texte[1:]), delimiter=";"))


@test("CSV : BOM, point-virgule, virgule decimale, ligne de total")
def _():
    r = R.calculer(cluster_trois(), [vente("web1", 1234.5, "A")])
    lignes = relire(R.vers_csv(r))
    egal(list(lignes[0].keys()), R.COLONNES, "en-tete")
    egal(lignes[0]["Type"], "Cluster")
    egal(lignes[0]["CA cumulé (€/mois)"], "1234,50")
    egal(lignes[-1]["Nom"], "TOTAL DU PARC")
    egal(lignes[-1]["ROI (€/mois)"], "934,50")
    # un ROI negatif reste un nombre, il ne prend pas d'apostrophe
    ns2 = next(l for l in lignes if l["Nom"].strip() == "ns2.eu")
    egal(ns2["ROI (€/mois)"], "-100,00")


@test("CSV : une formule saisie dans un nom est neutralisee")
def _():
    data = {"servers": [serveur(1)], "probes": [sonde(1, "prod", "pve-1")],
            "guests": [invite("web6", 100, "prod", "pve-1", 1)],
            "costs": [cout(1, 0)]}
    malins = ['=HYPERLINK("http://x","clic")', "+cmd|' /C calc'!A0",
              "-2+3", "@SUM(A1)"]
    r = R.calculer(data, (), [element(i, "vm", "web6", nom, pv=1)
                              for i, nom in enumerate(malins)])
    lignes = relire(R.vers_csv(r))
    for l in lignes:
        for col in ("Nom", "Chemin"):
            v = l[col].lstrip(" ")
            if v and v[0] in "=+-@":
                raise AssertionError("cellule executable : {!r}".format(v))


@test("CSV : un retour a la ligne dans une saisie ne casse pas les lignes")
def _():
    data = {"servers": [serveur(1)], "probes": [sonde(1, "prod", "pve-1")],
            "guests": [invite("web6", 100, "prod", "pve-1", 1)],
            "costs": [cout(1, 0)]}
    r = R.calculer(data, (), [element(1, "vm", "web6", "a\nb;c", pv=1)])
    lignes = relire(R.vers_csv(r))
    egal(any(l["Nom"].strip() == "a b;c" for l in lignes), True,
         "nom sur une ligne, point-virgule protege")


@test("cout OVH : l'offre et ses options, chacune ramenee au mois")
def _():
    import ovh_inventory

    def svc(prix, duree, plan="", route=None):
        s = {"billing": {"pricing": {"price": {"value": prix, "currencyCode": "EUR"},
                                     "duration": duree},
                         "plan": {"code": plan, "invoiceName": plan.upper()}}}
        if route:
            s["route"] = {"path": "/dedicated/server/{serviceName}",
                          "vars": [{"key": "serviceName", "value": route}]}
        return s

    reponses = {
        "/services?routes=/dedicated/server": [11],
        "/services/11": svc(120.0, "P1M", "advance-2", route="ns1.ovh.net"),
        "/services/11/options": [svc(240.0, "P1Y", "ram-64g"),   # 20 / mois
                                 svc(5.0, "P1M", "ip-failover"),
                                 svc(None, "P1M", "bande-passante")],
    }

    class Faux:
        def get_safe(self, chemin, defaut=None):
            return reponses.get(chemin, defaut)

    c = ovh_inventory.fetch_costs(Faux(), 1)["ns1.ovh.net"]
    egal(c["base_month"], 120.0, "offre")
    egal(c["options_month"], 25.0, "options")
    egal(c["price_month"], 145.0, "total facture")
    egal([o["plan"] for o in c["options"]], ["ram-64g", "ip-failover"])
    egal("1 option(s) sans tarif" in (c["erreur"] or ""), True, "option sans prix signalee")

    # le total passe dans le PA du serveur, le detail dans ses infos
    data = {"servers": [serveur(1)], "probes": [sonde(1, "prod", "pve-1")],
            "guests": [], "costs": [c]}
    s = R.calculer(data)["arbre"][0]["enfants"][0]
    egal(s["type"], "serveur")
    egal(s["pa_propre"], 145.0, "PA du serveur")
    egal(s["infos"]["pa_options"], 25.0, "detail des options")


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
