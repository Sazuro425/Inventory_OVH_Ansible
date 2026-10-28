#!/usr/bin/env python3
"""
Genere un parc fictif et l'ecrit en base, pour eprouver toute la chaine a
l'echelle sans toucher a la production.

Une fois le snapshot en base, tout l'aval fonctionne normalement :

    python generer_demo.py --store-db
    python infra_inventory.py --from-db --table
    python topologie.py
    python bastion_diff.py --acces demo_acces.json

Les adresses sont volontairement prises dans les plages de documentation
(RFC 5737 et RFC 1918) : elles ne sont routables nulle part. Un playbook lance
par erreur sur cet inventaire ne peut atteindre aucune machine reelle, ce qui
ne serait pas vrai avec des IP OVH plausibles.

Le tirage est reproductible : meme --seed, meme parc.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import infra_inventory as orch  # noqa: E402
import inventory_db  # noqa: E402

DATACENTRES = ["rbx6", "sbg5", "gra11", "bhs8"]
OFFRES = [
    ("RISE-S | AMD Ryzen 7 9700X", "R7-9700X", 1, 8, 16, 65536,
     2, "2x512GB NVME"),
    ("ADVANCE-2 | AMD EPYC 4344P", "EPYC-4344P", 1, 8, 16, 32768,
     2, "2x960GB NVME"),
    ("SCALE-3 | AMD EPYC 9354", "EPYC-9354", 2, 32, 64, 393216,
     4, "4x3840GB NVME"),
]
OS_LINUX = [
    "Debian GNU/Linux 12 (bookworm)", "Debian GNU/Linux 13 (trixie)",
    "Ubuntu 24.04.1 LTS", "Rocky Linux 9.4", "Alpine Linux 3.20",
]
OS_WINDOWS = ["win11", "win2022", "win2019"]
ROLES = [
    ("web", ["lamp", "php82"]), ("db", ["mysql"]), ("cache", ["redis"]),
    ("ci", []), ("proxy", ["haproxy"]), ("backup", []), ("ad", []),
]
CLIENTS = ["abc", "delta", "orion", "zenith"]


def generer(nb_dedies, vm_min, vm_max, seed, taux_cluster, taux_panne,
            taux_windows, taux_sans_ip):
    alea = random.Random(seed)
    serveurs, probes, invites = [], [], []

    # Repartition en clusters : on regroupe des dedies consecutifs, ce qui
    # produit a la fois des clusters de 2 a 4 noeuds et des noeuds isoles.
    restants = nb_dedies
    groupes, i = [], 0
    while restants > 0:
        if alea.random() < taux_cluster and restants >= 2:
            taille = min(restants, alea.randint(2, 4))
        else:
            taille = 1
        groupes.append(list(range(i, i + taille)))
        i += taille
        restants -= taille

    vmid = 100
    for n_cluster, membres in enumerate(groupes):
        en_cluster = len(membres) > 1
        nom_cluster = "prod-{}".format(n_cluster + 1) if en_cluster else None
        dc = alea.choice(DATACENTRES)
        noeuds_du_cluster = []

        for rang, idx in enumerate(membres):
            offre, cpu, sockets, coeurs, threads, ram, nb_d, desc_d = \
                alea.choice(OFFRES)
            # RFC 5737 : 198.51.100.0/24, reservee a la documentation
            ip = "198.51.100.{}".format(idx + 1)
            reverse = "ns{}.ip-198-51-100.eu".format(100000 + idx)
            noeud = "pve-{}".format(idx + 1)
            panne = alea.random() < taux_panne

            serveurs.append({
                "ovh_name": "ns{}.ovh.net".format(100000 + idx),
                "reverse": reverse, "ip": ip, "offer": offre,
                "datacenter": dc, "os": "proxmox9_64", "state": "ok",
                "expiration": "202{}-{:02d}-{:02d}".format(
                    alea.randint(6, 9), alea.randint(1, 12), alea.randint(1, 28)),
                "cpu_model": cpu, "cpu_sockets": sockets, "cpu_cores": coeurs,
                "cpu_threads": threads, "ram_mb": ram,
                "disk_count": nb_d, "disk_desc": desc_d,
            })

            if panne:
                probes.append({
                    "host": reverse, "node": None, "cluster": None,
                    "members": [], "error": "port 8006 ferme sur " + reverse,
                    "server": {}, "hw": {},
                })
                continue

            probes.append({
                "host": reverse, "node": noeud,
                "cluster": nom_cluster or ("standalone:" + noeud),
                "members": [], "error": None, "server": {},
                "hw": {
                    "cpu_model": "AMD {} Processor".format(cpu),
                    "cpu_sockets": sockets, "cpu_cores": coeurs,
                    "cpu_logical": threads, "cpu_mhz": alea.choice([2400, 2992, 3400]),
                    "ram_total_mb": int(ram * 0.975),
                    "ram_used_mb": int(ram * alea.uniform(0.3, 0.85)),
                    "disk_count": nb_d, "disk_total_gb": round(nb_d * 476.9, 1),
                    "disk_desc": "{}x nvme SAMSUNG MZVL2".format(nb_d),
                    "pve_version": alea.choice(["9.2.21", "9.2.18", "8.3.4"]),
                    "kernel": "Linux 7.0.14-23-pve",
                },
            })
            noeuds_du_cluster.append((reverse, noeud, rang))

        # Les invites sont rattaches au representant du cluster, comme le
        # ferait /cluster/resources.
        if not noeuds_du_cluster:
            continue
        representant = noeuds_du_cluster[0][0]
        for reverse, noeud, rang in noeuds_du_cluster:
            for _ in range(alea.randint(vm_min, vm_max)):
                vmid += 1
                windows = alea.random() < taux_windows
                role, tags_role = alea.choice(ROLES)
                client = alea.choice(CLIENTS)
                arretee = alea.random() < 0.12
                sans_ip = windows or alea.random() < taux_sans_ip or arretee
                tags = ["client-" + client] + tags_role
                invites.append({
                    "node": noeud,
                    "type": "lxc" if (not windows and alea.random() < 0.25)
                            else "qemu",
                    "vmid": vmid,
                    "name": "{}-{}-{:02d}".format(role, client, vmid % 100),
                    "status": "stopped" if arretee else "running",
                    "cpus": alea.choice([1, 2, 2, 4, 4, 8]),
                    "ram_mb": alea.choice([1024, 2048, 4096, 8192, 16384]),
                    "disk_gb": float(alea.choice([20, 40, 50, 80, 120, 250])),
                    "uptime": "" if arretee else "{}j {}h".format(
                        alea.randint(1, 200), alea.randint(0, 23)),
                    "os": alea.choice(OS_WINDOWS) if windows
                          else alea.choice(OS_LINUX),
                    # RFC 1918 pour les invites
                    "ipv4": "" if sans_ip else "10.{}.{}.{}".format(
                        10 + rang, (vmid // 250) % 256, vmid % 250 + 2),
                    "tags": ";".join(tags),
                    "cluster": probes[-1]["cluster"] if False else None,
                    "via_host": representant,
                })

    # rattache chaque invite au cluster de son hote representant
    cluster_par_hote = {p["host"]: p["cluster"] for p in probes}
    for g in invites:
        g["cluster"] = cluster_par_hote.get(g["via_host"])

    return {"servers": serveurs, "probes": probes, "guests": invites,
            "generated_at": int(time.time())}


def generer_acces(data, alea, taux_manquant=0.08, nb_orphelins=5):
    """Accès bastion plausibles : la plupart déclarés, quelques oublis, et des
    entrées mortes — pour que le comparatif ait quelque chose à dire."""
    acces = []
    for srv in data["servers"]:
        pr = next((p for p in data["probes"] if p["host"] == srv["reverse"]), None)
        if pr is None or pr.get("error"):
            continue
        if alea.random() < taux_manquant:
            continue
        acces.append({"ip": srv["ip"], "user": "root", "port": 22})
    for g in data["guests"]:
        ip = (g.get("ipv4") or "").split(" ")[0]
        if not ip or str(g.get("os", "")).lower().startswith("win"):
            continue
        if alea.random() < taux_manquant:
            continue
        acces.append({"ip": ip, "user": "ansible", "port": 22})
    for i in range(nb_orphelins):
        # RFC 5737 : 203.0.113.0/24, egalement reservee a la documentation
        acces.append({"ip": "203.0.113.{}".format(10 + i),
                      "user": "ansible", "port": 22})
    return {"error_code": "OK", "command": "selfListAccesses",
            "value": {"groups": [{"group": "infra-prod", "accesses": acces}]}}


def main():
    ap = argparse.ArgumentParser(
        description="Genere un parc fictif pour eprouver la chaine.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--config", default="infra.yml")
    ap.add_argument("--dedies", type=int, default=30)
    ap.add_argument("--vm-min", type=int, default=4)
    ap.add_argument("--vm-max", type=int, default=16)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--taux-cluster", type=float, default=0.45,
                    help="probabilite qu'un groupe soit un cluster (defaut 0.45)")
    ap.add_argument("--taux-panne", type=float, default=0.05)
    ap.add_argument("--taux-windows", type=float, default=0.10)
    ap.add_argument("--taux-sans-ip", type=float, default=0.06)
    ap.add_argument("--store-db", action="store_true",
                    help="ecrit le snapshot en base")
    ap.add_argument("--acces", metavar="FICHIER",
                    help="ecrit aussi un fichier d'acces bastion coherent")
    ap.add_argument("--json", metavar="FICHIER", help="ecrit le parc brut")
    args = ap.parse_args()

    data = generer(args.dedies, args.vm_min, args.vm_max, args.seed,
                   args.taux_cluster, args.taux_panne, args.taux_windows,
                   args.taux_sans_ip)

    clusters = {p["cluster"] for p in data["probes"] if not p["error"]}
    vrais = {c for c in clusters if c and not c.startswith("standalone:")}
    print("parc genere (seed {}) :".format(args.seed), file=sys.stderr)
    print("  {} dedie(s), dont {} injoignable(s)".format(
        len(data["servers"]),
        sum(1 for p in data["probes"] if p["error"])), file=sys.stderr)
    print("  {} cluster(s) distinct(s) dont {} multi-noeuds".format(
        len(clusters), len(vrais)), file=sys.stderr)
    print("  {} invite(s), dont {} sans IP".format(
        len(data["guests"]),
        sum(1 for g in data["guests"] if not g["ipv4"])), file=sys.stderr)

    if args.json:
        Path(args.json).write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
        print("  parc ecrit dans " + args.json, file=sys.stderr)

    if args.acces:
        charge = generer_acces(data, random.Random(args.seed))
        Path(args.acces).write_text(
            "JSON_OUTPUT=" + json.dumps(charge), encoding="utf-8")
        print("  {} acces bastion ecrits dans {}".format(
            len(charge["value"]["groups"][0]["accesses"]), args.acces),
            file=sys.stderr)

    if args.store_db:
        conf = orch.load_config(args.config)
        try:
            conn = inventory_db.connect(conf)
            run_id = inventory_db.store_run(
                conn, data, message="parc genere (demo, seed {})".format(args.seed))
            conn.close()
        except inventory_db.DbError as exc:
            print("Erreur : " + str(exc), file=sys.stderr)
            return 1
        print("  snapshot #{} enregistre en base".format(run_id), file=sys.stderr)
        print("  (marque 'demo' dans collect_run.message)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
