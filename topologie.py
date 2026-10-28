#!/usr/bin/env python3
"""
Schema interactif de l'infrastructure : datacenter -> serveur dedie ->
hyperviseur -> VM, facon vue topologie.

Produit une page HTML autonome (aucune dependance, aucun appel reseau) ou l'on
peut deplacer les cartes, replier des branches, filtrer et rechercher. Les
deplacements, replis et filtres sont memorises dans le navigateur, donc la
page peut etre servie telle quelle a plusieurs personnes.

    python topologie.py                    depuis la base (dernier snapshot)
    python topologie.py --refresh          collecte d'abord
    python topologie.py --out /var/www/html/infra.html

Python calcule les donnees, le navigateur calcule la mise en page : c'est lui
qui doit la refaire a chaque filtre ou repli.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bastion_diff as bdiff  # noqa: E402
import infra_inventory as orch  # noqa: E402
import inventory_db  # noqa: E402

GABARIT = Path(__file__).resolve().parent / "assets" / "topologie.tmpl.html"

COULEUR_ETAT = {
    "running": "var(--vert)",
    "stopped": "var(--gris)",
    "unreachable": "var(--rouge)",
}


def go(mo):
    return "{:g} Go".format(round(mo / 1024, 1)) if mo else "?"


def badge_expiration(valeur, seuil=120):
    """Rien tant que l'echeance est lointaine : un badge permanent n'alerte
    plus personne et prend la place du titre."""
    if not valeur:
        return None
    try:
        jour = datetime.fromisoformat(str(valeur)[:10]).date()
    except ValueError:
        return None
    reste = (jour - datetime.now().date()).days
    if reste > seuil:
        return None
    return "expire" if reste < 0 else "J-{}".format(reste)


def construire(data, acces=None, conf=None):
    """Aplatit l'arbre en une liste de noeuds (id + parent), lue par le JS."""
    conf = conf or {"ansible": {}}
    serveurs = data.get("servers") or []
    probes = data.get("probes") or []
    invites = data.get("guests") or []

    par_hote = {}
    for g in invites:
        par_hote.setdefault(g.get("via_host"), []).append(g)
    probes_par_hote = {p["host"]: p for p in probes}

    datacenters = {}
    for srv in serveurs:
        datacenters.setdefault((srv.get("datacenter") or "inconnu").lower(),
                               []).append(srv)

    noeuds = []
    compte_dc, compte_cluster, compte_type, compte_etat = {}, {}, {}, {}
    compte_acces = {}

    def etat_acces(ip, user, os_machine=None, tags=None, nom=None):
        """None quand aucune couche d'acces n'est chargee : on n'affiche alors
        rien plutot que de laisser croire a une absence d'acces."""
        if acces is None:
            return None
        if bdiff.hors_perimetre(conf, os_machine, tags, nom, ip):
            return "hors-perimetre"
        if not ip:
            return "inconnu"
        return ("ok" if any(bdiff.couvre(a, ip, user, 22) for a in acces)
                else "manquant")

    def compter(d, cle):
        if cle is not None:
            d[cle] = d.get(cle, 0) + 1

    for dc in sorted(datacenters):
        id_dc = "dc:" + dc
        noeuds.append({
            "id": id_dc, "parent": None, "kind": "dc", "libelle": "Datacenter",
            "titre": dc.upper(),
            "lignes": ["{} serveur(s)".format(len(datacenters[dc]))],
            "etat": None, "badge": None,
            "meta": {"dc": dc},
            "recherche": dc,
            "fiche": {"Datacenter": dc.upper(),
                      "Serveurs": len(datacenters[dc])},
        })

        for srv in sorted(datacenters[dc], key=lambda s: s.get("reverse") or ""):
            id_srv = "srv:" + (srv.get("ovh_name") or srv.get("reverse"))
            offre = (srv.get("offer") or "").split("|")[0].strip()
            compter(compte_dc, dc)
            compter(compte_acces, etat_acces(srv.get("ip"), "root",
                                             srv.get("os"), None,
                                             srv.get("reverse")))
            noeuds.append({
                "id": id_srv, "parent": id_dc, "kind": "srv",
                "libelle": "Serveur dédié", "titre": srv.get("reverse") or "",
                "lignes": [l for l in (
                    offre or None,
                    "{} · {}c/{}t · {}".format(
                        srv.get("cpu_model") or "CPU ?",
                        srv.get("cpu_cores") or "?", srv.get("cpu_threads") or "?",
                        go(srv.get("ram_mb"))),
                    "{} disque(s) · {}".format(
                        srv.get("disk_count") or "?", srv.get("disk_desc") or "?"),
                ) if l],
                "etat": None,
                "badge": badge_expiration(srv.get("expiration")),
                "meta": {"dc": dc, "acces": etat_acces(srv.get("ip"), "root", srv.get("os"),
                                     None, srv.get("reverse"))},
                # valeurs chiffrees : le navigateur en a besoin pour
                # recalculer les totaux quand on filtre
                "mesures": {"cpu_cores": srv.get("cpu_cores") or 0,
                            "ram_mb": srv.get("ram_mb") or 0},
                "recherche": " ".join(filter(None, [
                    srv.get("reverse"), srv.get("ovh_name"), offre,
                    srv.get("cpu_model"), srv.get("ip"), dc])).lower(),
                "fiche": {
                    "Nom OVH": srv.get("ovh_name"), "Reverse": srv.get("reverse"),
                    "IP": srv.get("ip"), "Offre": srv.get("offer"),
                    "Datacenter": dc.upper(), "État": srv.get("state"),
                    "Expiration": srv.get("expiration"),
                    "Processeur": srv.get("cpu_model"),
                    "Cœurs / threads": "{} / {}".format(
                        srv.get("cpu_cores") or "?", srv.get("cpu_threads") or "?"),
                    "Mémoire": go(srv.get("ram_mb")),
                    "Disques": "{} × {}".format(
                        srv.get("disk_count") or "?", srv.get("disk_desc") or "?"),
                    "Système": srv.get("os"),
                    "Accès bastion": {"ok": "déclaré", "manquant": "ABSENT",
                                      "inconnu": "IP inconnue"}.get(
                        etat_acces(srv.get("ip"), "root", srv.get("os"),
                                   None, srv.get("reverse"))),
                },
            })

            probe = probes_par_hote.get(srv.get("reverse"))
            if probe is None:
                continue

            id_node = "node:" + probe["host"]
            if probe.get("error"):
                compter(compte_etat, "unreachable")
                noeuds.append({
                    "id": id_node, "parent": id_srv, "kind": "node",
                    "libelle": "Hyperviseur", "titre": probe["host"],
                    "lignes": [str(probe["error"])[:54]],
                    "etat": "unreachable", "badge": None,
                    "meta": {"dc": dc, "etat": "unreachable"},
                    "recherche": (probe["host"] + " injoignable").lower(),
                    "fiche": {"Hôte": probe["host"], "État": "injoignable",
                              "Erreur": probe["error"]},
                })
                continue

            hw = probe.get("hw") or {}
            cluster = probe.get("cluster") or ""
            seul = cluster.startswith("standalone:")
            nom_cluster = "—" if seul else cluster
            siens = par_hote.get(probe["host"], [])
            compter(compte_cluster, nom_cluster)
            compter(compte_etat, "running")
            noeuds.append({
                "id": id_node, "parent": id_srv, "kind": "node",
                "libelle": "Hyperviseur", "titre": probe.get("node") or probe["host"],
                "lignes": [
                    "nœud seul" if seul else "cluster " + cluster,
                    "PVE {} · {} vCPU · {}".format(
                        hw.get("pve_version") or "?", hw.get("cpu_logical") or "?",
                        go(hw.get("ram_total_mb"))),
                    "{} invité(s)".format(len(siens)),
                ],
                "etat": "running", "badge": None,
                "meta": {"dc": dc, "cluster": nom_cluster, "etat": "running"},
                "recherche": " ".join(filter(None, [
                    probe.get("node"), probe["host"], cluster,
                    hw.get("cpu_model"), hw.get("pve_version")])).lower(),
                "fiche": {
                    "Nœud": probe.get("node"), "Hôte": probe["host"],
                    "Cluster": nom_cluster, "Version PVE": hw.get("pve_version"),
                    "Noyau": hw.get("kernel"), "Processeur": hw.get("cpu_model"),
                    "Sockets / cœurs / threads": "{} / {} / {}".format(
                        hw.get("cpu_sockets") or "?", hw.get("cpu_cores") or "?",
                        hw.get("cpu_logical") or "?"),
                    "Fréquence": "{} MHz".format(hw["cpu_mhz"])
                                 if hw.get("cpu_mhz") else None,
                    "Mémoire": "{} utilisés sur {}".format(
                        go(hw.get("ram_used_mb")), go(hw.get("ram_total_mb"))),
                    "Disques": "{} × {} ({} Go)".format(
                        hw.get("disk_count") or "?", hw.get("disk_desc") or "?",
                        hw.get("disk_total_gb") or "?"),
                    "Invités": len(siens),
                },
            })

            for g in sorted(siens, key=lambda x: x.get("vmid") or 0):
                etat_g = "running" if g.get("status") == "running" else "stopped"
                type_g = (g.get("type") or "").lower()
                ip = (g.get("ipv4") or "").split(" ")[0]
                compter(compte_type, type_g)
                compter(compte_etat, etat_g)
                compter(compte_acces, etat_acces(ip, "ansible", g.get("os"),
                                                 g.get("tags"),
                                                 g.get("name")))
                noeuds.append({
                    "id": "vm:{}:{}".format(probe["host"], g.get("vmid")),
                    "parent": id_node, "kind": "vm",
                    "libelle": "Machine virtuelle" if type_g == "qemu"
                               else "Conteneur",
                    "titre": "{} · {}".format(g.get("vmid"), g.get("name") or ""),
                    "lignes": ["{} · {} vCPU · {}{}".format(
                        type_g.upper(), g.get("cpus") or "?", go(g.get("ram_mb")),
                        " · " + ip if ip else "")],
                    "etat": etat_g,
                    "badge": (g.get("tags") or "").replace(";", " ").strip() or None,
                    "meta": {"dc": dc, "cluster": nom_cluster,
                             "type": type_g, "etat": etat_g,
                             "acces": etat_acces(ip, "ansible", g.get("os"),
                                                 g.get("tags"),
                                                 g.get("name"))},
                    "mesures": {"cpus": int(g.get("cpus") or 0),
                                "ram_mb": int(g.get("ram_mb") or 0),
                                "disk_gb": float(g.get("disk_gb") or 0)},
                    "recherche": " ".join(filter(None, [
                        str(g.get("vmid")), g.get("name"), g.get("os"), ip,
                        g.get("tags"), type_g])).lower(),
                    "fiche": {
                        "VMID": g.get("vmid"), "Nom": g.get("name"),
                        "Type": type_g.upper(), "État": g.get("status"),
                        "Système": g.get("os"), "vCPU": g.get("cpus"),
                        "Mémoire": go(g.get("ram_mb")),
                        "Disque": "{} Go".format(g.get("disk_gb"))
                                  if g.get("disk_gb") else None,
                        "IPv4": g.get("ipv4"), "Uptime": g.get("uptime"),
                        "Étiquettes": g.get("tags"),
                        "Nœud": probe.get("node"),
                        "Serveur": srv.get("reverse"),
                        "Accès bastion": {"ok": "déclaré", "manquant": "ABSENT",
                                          "inconnu": "IP inconnue"}.get(
                            etat_acces(ip, "ansible", g.get("os"),
                                       g.get("tags"), g.get("name"))),
                    },
                })

    # Un acces sans machine ne peut se rattacher a rien : il lui faut sa
    # propre branche, sinon il resterait invisible dans un arbre du parc.
    if acces:
        ips = set()
        for srv in serveurs:
            if srv.get("ip"):
                ips.add(srv["ip"])
        for g in invites:
            ip = (g.get("ipv4") or "").split(" ")[0]
            if ip:
                ips.add(ip)
        orphelins = []
        for a in acces:
            net = bdiff.reseau(a["adresse"])
            if net is None:
                continue
            if net.num_addresses > 1:
                import ipaddress as _ip
                couvre_qqn = any(
                    _ip.ip_address(x) in net for x in ips if bdiff.reseau(x))
                if couvre_qqn:
                    continue
                motif = "plage sans aucune machine connue"
            elif str(net.network_address) in ips:
                continue
            else:
                motif = "machine absente de l'inventaire"
            orphelins.append((a, motif))

        if orphelins:
            noeuds.append({
                "id": "orphelins", "parent": None, "kind": "dc",
                "libelle": "Accès bastion", "titre": "ORPHELINS",
                "lignes": ["{} accès sans machine".format(len(orphelins))],
                "etat": "unreachable", "badge": None,
                "meta": {"acces": "orphelin"}, "hors_parc": True,
                "recherche": "orphelin acces",
                "fiche": {"Accès sans machine": len(orphelins)},
            })
            for i, (a, motif) in enumerate(orphelins):
                compter(compte_acces, "orphelin")
                noeuds.append({
                    "id": "orph:{}".format(i), "parent": "orphelins",
                    "kind": "srv", "libelle": "Accès orphelin",
                    "titre": a["adresse"],
                    "lignes": ["compte {} · port {}".format(a["user"], a["port"]),
                               motif],
                    "etat": "unreachable", "badge": None,
                    "meta": {"acces": "orphelin"}, "hors_parc": True,
                    "recherche": " ".join([a["adresse"], a["user"], "orphelin"]),
                    "fiche": {"Adresse": a["adresse"], "Compte": a["user"],
                              "Port": a["port"], "Origine": a.get("origine"),
                              "Motif": motif,
                              "Commande": "bssh --osh groupDelServer --host {} "
                                          "--user {} --port {}".format(
                                              a["adresse"], a["user"], a["port"])},
                })

    filtres = []
    if len(compte_dc) > 1:
        filtres.append({"cle": "dc", "libelle": "Datacenter", "valeurs": [
            {"val": k, "libelle": k.upper(), "n": v}
            for k, v in sorted(compte_dc.items())]})
    if len(compte_cluster) > 1:
        filtres.append({"cle": "cluster", "libelle": "Cluster", "valeurs": [
            {"val": k, "libelle": k, "n": v}
            for k, v in sorted(compte_cluster.items())]})
    if compte_type:
        filtres.append({"cle": "type", "libelle": "Type", "valeurs": [
            {"val": k, "libelle": k.upper(), "n": v}
            for k, v in sorted(compte_type.items())]})
    if compte_etat:
        libelles = {"running": "en marche", "stopped": "arrêté",
                    "unreachable": "injoignable"}
        filtres.append({"cle": "etat", "libelle": "État", "valeurs": [
            {"val": k, "libelle": libelles.get(k, k), "n": v,
             "couleur": COULEUR_ETAT.get(k)}
            for k, v in sorted(compte_etat.items())]})

    if compte_acces:
        libelles = {"ok": "accès déclaré", "manquant": "sans accès",
                    "inconnu": "IP inconnue", "orphelin": "orphelin",
                    "hors-perimetre": "hors périmètre"}
        couleurs = {"ok": "var(--vert)", "manquant": "var(--ambre)",
                    "inconnu": "var(--gris)", "orphelin": "var(--rouge)",
                    "hors-perimetre": "var(--doux)"}
        filtres.append({"cle": "acces", "libelle": "Bastion", "valeurs": [
            {"val": k, "libelle": libelles.get(k, k), "n": v,
             "couleur": couleurs.get(k)}
            for k, v in sorted(compte_acces.items())]})

    return noeuds, filtres


def statistiques(data, acces=None, conf=None):
    conf = conf or {"ansible": {}}
    invites = data.get("guests") or []
    probes = data.get("probes") or []
    stats = [
        {"l": "Dédiés", "v": len(data.get("servers") or [])},
        {"l": "Hyperviseurs", "v": sum(1 for p in probes if not p.get("error"))},
        {"l": "Invités", "v": len(invites)},
        {"l": "En marche",
         "v": sum(1 for g in invites if g.get("status") == "running")},
        {"l": "vCPU alloués",
         "v": sum(int(g.get("cpus") or 0) for g in invites)},
        {"l": "RAM allouée", "v": "{:g} Go".format(
            round(sum(int(g.get("ram_mb") or 0) for g in invites) / 1024, 1))},
    ]
    injoignables = sum(1 for p in probes if p.get("error"))
    if injoignables:
        stats.append({"l": "Injoignables", "v": injoignables})
    if acces is not None:
        manquants = 0
        for srv in data.get("servers") or []:
            if srv.get("ip") and not any(
                    bdiff.couvre(a, srv["ip"], "root", 22) for a in acces):
                manquants += 1
        for g in data.get("guests") or []:
            ip = (g.get("ipv4") or "").split(" ")[0]
            if not ip or bdiff.hors_perimetre(conf, g.get("os"), g.get("tags"),
                                              g.get("name"), ip):
                continue
            if not any(bdiff.couvre(a, ip, "ansible", 22) for a in acces):
                manquants += 1
        stats.append({"l": "Sans accès", "v": manquants})
    return stats


def rendre(data, titre, acces=None, conf=None):
    noeuds, filtres = construire(data, acces, conf)
    charge = {
        "noeuds": noeuds,
        "filtres": filtres,
        "stats": statistiques(data, acces, conf),
        "horodatage": datetime.fromtimestamp(
            data.get("generated_at") or 0).strftime("%d/%m/%Y à %H:%M"),
        # cle de stockage : les positions suivent le parc, pas le snapshot
        "cle": "infra",
    }
    gabarit = GABARIT.read_text(encoding="utf-8")
    json_charge = json.dumps(charge, ensure_ascii=False, separators=(",", ":"))
    # </script> dans une donnee fermerait la balise : seul cas a neutraliser
    json_charge = json_charge.replace("</", "<\\/")
    return (gabarit.replace("__TITRE__", titre)
                   .replace("__DATA__", json_charge))


def ecrire_atomique(cible, contenu):
    """Ecrit a cote puis renomme.

    Un serveur web qui sert ce fichier pendant qu'on le reecrit renverrait une
    page tronquee. Le renommage, lui, est atomique sur le meme systeme de
    fichiers : un visiteur voit l'ancienne page ou la nouvelle, jamais un
    melange des deux.
    """
    tampon = cible.with_name(cible.name + ".tmp")
    tampon.write_text(contenu, encoding="utf-8")
    try:
        # conserve proprietaire et droits de la page precedente
        if cible.exists():
            os.chmod(tampon, cible.stat().st_mode & 0o7777)
        os.replace(tampon, cible)
    except OSError:
        tampon.unlink(missing_ok=True)
        raise


def main():
    ap = argparse.ArgumentParser(
        description="Schema interactif de l'infrastructure (HTML autonome).",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--config", default=os.environ.get("INFRA_CONFIG", "infra.yml"))
    ap.add_argument("--refresh", action="store_true",
                    help="collecte maintenant au lieu de lire le dernier snapshot")
    ap.add_argument("--out", default="exports/topologie.html")
    ap.add_argument("--titre", default="Infrastructure OVH / Proxmox")
    ap.add_argument("--bastion-acces", metavar="FICHIER",
                    help="sortie JSON de selfListAccesses : superpose l'etat "
                         "des acces bastion sur le schema")
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()

    if not GABARIT.is_file():
        print("gabarit introuvable : " + str(GABARIT), file=sys.stderr)
        return 1

    conf = orch.load_config(args.config)
    if args.refresh:
        data = orch.gather(conf, args.workers, conf["pve"].get("detail", True))
    else:
        conn = inventory_db.connect(conf)
        data = inventory_db.load_latest(conn)
        conn.close()
        if data is None:
            print("aucun snapshot en base : lance une collecte "
                  "(--refresh, ou infra_inventory.py --refresh --store-db)",
                  file=sys.stderr)
            return 1
        print("snapshot #{} : {} serveur(s), {} invite(s)".format(
            data["run_id"], len(data["servers"]), len(data["guests"])),
            file=sys.stderr)

    acces = None
    if args.bastion_acces:
        try:
            acces = bdiff.lire_acces(args.bastion_acces)
            print("acces bastion : {} entree(s)".format(len(acces)),
                  file=sys.stderr)
        except bdiff.BastionErreur as exc:
            print("Erreur : " + str(exc), file=sys.stderr)
            return 1

    sortie = Path(args.out)
    sortie.parent.mkdir(parents=True, exist_ok=True)
    ecrire_atomique(sortie, rendre(data, args.titre, acces, conf))
    print("ecrit : {} ({:.0f} Ko)".format(sortie, sortie.stat().st_size / 1024),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
