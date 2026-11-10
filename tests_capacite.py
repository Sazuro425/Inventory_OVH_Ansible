#!/usr/bin/env python3
"""
Contrat de l'historique et de la projection de capacite.

    python3 tests_capacite.py

Sans base : capacite.py prend des lignes et rend des lignes. Ces tests
fixent ce que la projection doit garantir — une date juste sur une
croissance reguliere, aucune date inventee sans recul, et un ajout de
serveur qui ne fausse pas la tendance.
"""

from __future__ import annotations

import datetime as dt
import sys
import traceback

import capacite as K
import tests_rentabilite as T

CAS = []
J = dt.date(2026, 11, 10)
GO = 1024


def test(libelle):
    def decorateur(fn):
        CAS.append((libelle, fn))
        return fn
    return decorateur


egal = T.egal


def croissance(jours, debut_go, go_par_jour, phys_go, cle="prod", saut=None):
    """Un point par jour jusqu'a J. saut : (jour, nouvelle RAM physique)."""
    out = []
    for i in range(jours, -1, -1):
        j = J - dt.timedelta(days=i)
        phys = phys_go
        if saut and j >= saut[0]:
            phys = saut[1]
        out.append({"jour": j, "niveau": "cluster", "cle": cle, "nom": cle,
                    "ram_alloue_mb": (debut_go + go_par_jour * (jours - i)) * GO,
                    "ram_physique_mb": phys * GO, "vcpu_alloue": 40,
                    "vcpu_physique": 32})
    return out


@test("historique : un point pour le parc, chaque cluster, chaque serveur")
def _():
    data = {"servers": [T.serveur(1), T.serveur(2)],
            "probes": [T.sonde(1, "prod", "pve-1"),
                       T.sonde(2, None, None, erreur="injoignable")],
            "guests": [T.invite("web", 100, "prod", "pve-1", 1, ram=8192)],
            "costs": [T.cout(1, 100)]}
    lignes = K.lignes_historique(T.R.calculer(data))
    par = {(l["niveau"], l["cle"]): l for l in lignes}
    egal(sorted(par), [("cluster", "prod"), ("cluster", "seul:ns2.ovh.net"),
                       ("parc", "parc"), ("serveur", "ns1.ovh.net"),
                       ("serveur", "ns2.ovh.net")])
    egal(par[("serveur", "ns1.ovh.net")]["ram_alloue_mb"], 8192)
    egal(par[("serveur", "ns1.ovh.net")]["nb_vm"], 1)
    egal(par[("serveur", "ns2.ovh.net")]["ram_physique_mb"], None,
         "un hote injoignable n'a pas une capacite nulle, mais inconnue")
    egal(par[("parc", "parc")]["pa"], 100.0)


@test("series : le dernier point de chaque mois, mois dans l'ordre")
def _():
    lignes = [{"jour": "2026-10-03", "niveau": "cluster", "cle": "a", "nom": "a",
               "pv": 10}, {"jour": "2026-10-28", "niveau": "cluster", "cle": "a",
                           "nom": "a-renomme", "pv": 12},
              {"jour": "2026-09-30", "niveau": "cluster", "cle": "a", "nom": "a",
               "pv": 9}, {"jour": "2026-10-28", "niveau": "serveur", "cle": "s",
                          "nom": "s", "pv": 1}]
    s = K.series_mensuelles(lignes, "cluster")
    egal(len(s), 1)
    egal([(p["mois"], p["pv"]) for p in s[0]["points"]],
         [("2026-09", 9.0), ("2026-10", 12.0)])
    egal(s[0]["nom"], "a-renomme", "le nom le plus recent")


@test("projection : 1 Go/jour sur 256 Go, les dates tombent juste")
def _():
    # 150 Go il y a 60 jours, 210 aujourd'hui ; 85 % = 217,6 Go ; 100 % = 256
    p = K.projection(croissance(60, 150, 1, 256), J)
    egal((p["tendance"], p["etat"], p["ram_go_par_mois"]), ("hausse", "ok", 30.0))
    egal(p["ram_85"], (J + dt.timedelta(days=7)).isoformat(), "217,6 - 210 = 7,6 j")
    egal(p["ram_100"], (J + dt.timedelta(days=46)).isoformat(), "256 - 210 = 46 j")
    egal(p["ram_pct"], 82.0)


@test("projection : sans recul, l'etat du jour et pas de date inventee")
def _():
    p = K.projection(croissance(3, 200, 5, 256), J)
    egal(p["tendance"], "insuffisante")
    egal("ram_100" in p, False)
    egal(K.projection([], J)["etat"], "inconnu")


@test("projection : en baisse, aucune echeance ; au-dela, deja atteint")
def _():
    p = K.projection(croissance(60, 200, -0.5, 256), J)
    egal((p["tendance"], p["ram_100"]), ("baisse", None))
    p = K.projection(croissance(60, 240, 0.5, 256), J)
    egal((p["etat"], p["ram_85"], p["ram_100"]), ("critique", "atteint", "atteint"))


@test("projection : un serveur ajoute ne fausse pas la tendance")
def _():
    # 512 Go physiques depuis 20 jours : le pourcentage chute, l'allocation
    # continue de monter de 1 Go/jour ; l'echeance part de la capacite actuelle
    p = K.projection(croissance(60, 150, 1, 256,
                                saut=(J - dt.timedelta(days=20), 512)), J)
    egal(p["ram_go_par_mois"], 30.0)
    egal(p["ram_physique_go"], 512.0)
    egal(p["ram_100"], (J + dt.timedelta(days=302)).isoformat(), "512 - 210")


@test("projection : une echeance a plus de cinq ans n'est pas une date")
def _():
    egal(K.projection(croissance(60, 100, 0.01, 4096), J)["ram_100"], None)


@test("projections : la plus urgente d'abord, la deja pleine en tete")
def _():
    lignes = (croissance(60, 150, 1, 256, cle="lent")
              + croissance(60, 150, 1.5, 256, cle="rapide")   # 240 Go, plein sous 11 j
              + croissance(60, 250, 0, 200, cle="plein"))
    egal([p["cle"] for p in K.projections(lignes, J)], ["plein", "rapide", "lent"])


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
