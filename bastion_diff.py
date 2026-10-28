#!/usr/bin/env python3
"""
Comparaison entre l'inventaire et les acces declares dans The Bastion.

Deux listes doivent coincider : ce que le parc contient, et ce que le bastion
autorise. Elles divergent toujours avec le temps, dans les deux sens :

  - une machine de l'inventaire absente du bastion est injoignable ; on s'en
    apercoit au pire moment, pendant une intervention ;
  - un acces qui subsiste pour une machine disparue est une entree morte. Si
    l'IP est reattribuee a une autre machine, l'acces la suit.

Ce module lit les acces du compte courant (selfListAccesses) et les confronte
a l'inventaire. Il ne modifie rien : il constate et propose les commandes.

    python bastion_diff.py                         interroge le bastion
    python bastion_diff.py --acces sortie.json     depuis une sortie capturee

La capture se fait avec :
    bssh --osh selfListAccesses --json-greppable > sortie.json
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import infra_inventory as orch  # noqa: E402
import inventory_db  # noqa: E402


class BastionErreur(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Lecture des acces
# --------------------------------------------------------------------------

def extraire_json(sortie):
    """Isole la charge utile JSON d'une sortie osh.

    --json-greppable prefixe une ligne par JSON_OUTPUT= ; --json encadre par
    JSON_START / JSON_END. On accepte les deux, et un JSON nu.
    """
    for ligne in sortie.splitlines():
        if ligne.startswith("JSON_OUTPUT="):
            return json.loads(ligne[len("JSON_OUTPUT="):])
    bloc = re.search(r"JSON_START\s*(.*?)\s*JSON_END", sortie, re.S)
    if bloc:
        return json.loads(bloc.group(1))
    sortie = sortie.strip()
    if sortie.startswith("{"):
        return json.loads(sortie)
    raise BastionErreur(
        "aucune charge JSON trouvee dans la sortie.\n"
        "Capture-la avec : bssh --osh selfListAccesses --json-greppable")


def acces_depuis_charge(charge):
    """Aplatit la reponse osh en triplets (reseau, user, port, origine).

    La forme exacte de 'value' varie selon la version du bastion ; plutot que
    de supposer un chemin precis, on parcourt la structure et on retient tout
    objet portant une adresse.
    """
    if isinstance(charge, dict) and charge.get("error_code") not in (None, "OK", "ok"):
        raise BastionErreur("le bastion a repondu une erreur : {} {}".format(
            charge.get("error_code"), charge.get("error_message") or ""))

    valeur = charge.get("value", charge) if isinstance(charge, dict) else charge
    trouves = []

    def visiter(noeud, contexte=None):
        if isinstance(noeud, dict):
            adresse = next((noeud[c] for c in ("ip", "host", "hostname", "cidr")
                            if noeud.get(c)), None)
            if adresse:
                trouves.append({
                    "adresse": str(adresse),
                    "user": str(noeud.get("user") or noeud.get("username") or "*"),
                    "port": str(noeud.get("port") or "*"),
                    "origine": noeud.get("group") or contexte or "personnel",
                })
            for cle, val in noeud.items():
                visiter(val, noeud.get("group") or contexte)
        elif isinstance(noeud, list):
            for item in noeud:
                visiter(item, contexte)

    visiter(valeur)
    if not trouves:
        raise BastionErreur(
            "aucun acces reconnu dans la reponse.\n"
            "Envoie-moi la sortie de 'bssh --osh selfListAccesses "
            "--json-greppable' pour ajuster la lecture.")
    return trouves


def lire_acces(source=None, commande=None):
    if source:
        chemin = Path(source)
        if not chemin.is_file():
            raise BastionErreur("fichier introuvable : " + str(chemin))
        return acces_depuis_charge(extraire_json(
            chemin.read_text(encoding="utf-8", errors="replace")))

    cmd = commande or ["bssh", "--osh", "selfListAccesses", "--json-greppable"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
    except FileNotFoundError as exc:
        raise BastionErreur(
            "commande introuvable : {}\nUtilise --acces avec une sortie "
            "capturee, ou --commande pour pointer ton client bastion.".format(
                cmd[0])) from exc
    except subprocess.TimeoutExpired as exc:
        raise BastionErreur("timeout en interrogeant le bastion") from exc
    sortie = r.stdout.decode("utf-8", "replace")
    if r.returncode != 0 and "JSON_OUTPUT=" not in sortie:
        raise BastionErreur("le client bastion a echoue ({}) : {}".format(
            r.returncode, r.stderr.decode("utf-8", "replace").strip()[:300]))
    return acces_depuis_charge(extraire_json(sortie))


# --------------------------------------------------------------------------
# Comparaison
# --------------------------------------------------------------------------

def reseau(adresse):
    """'10.0.0.0/24' ou '10.0.0.5' -> objet reseau ; None si c'est un nom."""
    try:
        return ipaddress.ip_network(adresse, strict=False)
    except ValueError:
        return None


def couvre(acces, ip, user, port):
    """Un acces couvre-t-il ce triplet ? Gere les plages et les jokers.

    Sans cela, un parc declare en sous-reseaux apparaitrait entierement
    manquant : une entree 10.10.20.0/24 ne vaut pas '10.10.20.10' en egalite
    de chaines.
    """
    if acces["user"] not in ("*", user) and not _glob(acces["user"], user):
        return False
    if acces["port"] not in ("*", str(port)):
        return False
    net = reseau(acces["adresse"])
    if net is None:
        return acces["adresse"] == ip
    try:
        return ipaddress.ip_address(ip) in net
    except ValueError:
        return False


def _glob(motif, valeur):
    if "*" not in motif and "?" not in motif:
        return False
    regex = "^" + re.escape(motif).replace(r"\*", ".*").replace(r"\?", ".") + "$"
    return re.match(regex, valeur) is not None


def hors_perimetre(conf, os_machine=None, tags=None, hote=None, ip=None):
    """Cette machine a-t-elle vocation a passer par le bastion ?

    Distinguer "pas encore declare" de "pas concerne" est ce qui fait la
    difference entre un rapport qu'on lit et un rapport qu'on ignore.
    """
    regle = ((conf["ansible"].get("bastion") or {}).get("hors_perimetre") or {})
    for motif in regle.get("os") or []:
        if os_machine and _glob(motif.lower(), str(os_machine).lower()):
            return "os " + motif
        if os_machine and motif.lower() == str(os_machine).lower():
            return "os " + motif
    etiquettes = {t.strip().lower()
                  for t in re.split(r"[;,]", tags or "") if t.strip()}
    for t in regle.get("tags") or []:
        if t.lower() in etiquettes:
            return "etiquette " + t
    for h in regle.get("hotes") or []:
        if h and h in filter(None, (hote, ip)):
            return "liste explicite"
    return None


def comparer(data, acces, conf):
    comptes = conf["ansible"].get("comptes") or {}
    attendus = []
    exclus = []

    probes = {p["host"]: p for p in (data.get("probes") or [])}
    for srv in data.get("servers") or []:
        pr = probes.get(srv.get("reverse"))
        if pr is None or pr.get("error"):
            continue
        ip = srv.get("ip")
        if not ip:
            continue
        quoi = "hyperviseur " + (pr.get("node") or srv.get("reverse"))
        motif = hors_perimetre(conf, srv.get("os"), None, srv.get("reverse"), ip)
        if motif:
            exclus.append({"ip": ip, "quoi": quoi, "motif": motif})
        else:
            attendus.append({"ip": ip, "user": comptes.get("node", "root"),
                             "port": 22, "quoi": quoi})

    sans_ip = []
    for g in data.get("guests") or []:
        ip = (g.get("ipv4") or "").split(" ")[0]
        etiquette = "{} ({})".format(g.get("name") or "?", g.get("vmid"))
        # le perimetre se teste avant l'IP : une machine qui n'a pas vocation
        # a passer par le bastion n'y serait pas davantage avec une IP connue
        motif = hors_perimetre(conf, g.get("os"), g.get("tags"),
                               g.get("name"), ip)
        if motif:
            exclus.append({"ip": ip or "-", "quoi": "invite " + etiquette,
                           "motif": motif})
            continue
        if not ip:
            sans_ip.append(etiquette)
            continue
        attendus.append({"ip": ip, "user": comptes.get("vm", "ansible"),
                         "port": 22, "quoi": "invite " + etiquette})

    manquants = [a for a in attendus
                 if not any(couvre(x, a["ip"], a["user"], a["port"]) for x in acces)]

    # accès du bastion que plus aucune machine de l'inventaire ne justifie
    ips_connues = {a["ip"] for a in attendus} | {a["ip"] for a in exclus}
    orphelins = []
    for x in acces:
        net = reseau(x["adresse"])
        if net is None:
            continue
        if net.num_addresses > 1:
            # une plage reste justifiee si elle couvre au moins une machine
            if any(ipaddress.ip_address(ip) in net for ip in ips_connues
                   if reseau(ip)):
                continue
            orphelins.append(dict(x, motif="plage sans aucune machine connue"))
        elif str(net.network_address) not in ips_connues:
            orphelins.append(dict(x, motif="machine absente de l'inventaire"))

    return {"attendus": attendus, "manquants": manquants, "exclus": exclus,
            "orphelins": orphelins, "sans_ip": sans_ip, "acces": acces}


def rapport(res, conf):
    groupe = (conf["ansible"].get("bastion") or {}).get("groupe") or "<groupe>"
    out = []
    a, m, o = len(res["attendus"]), len(res["manquants"]), len(res["orphelins"])
    out.append("Inventaire : {} machine(s) joignable(s) attendue(s)".format(a))
    out.append("Bastion    : {} acces declare(s)".format(len(res["acces"])))
    out.append("")

    if m:
        out.append("--- {} machine(s) de l'inventaire sans acces bastion ---".format(m))
        for x in res["manquants"]:
            out.append("  {:<16} {:<10} {}".format(x["ip"], x["user"], x["quoi"]))
        out.append("")
        out.append("  Commandes pour les declarer :")
        for x in res["manquants"]:
            out.append("    bssh --osh groupAddServer --group {} --host {} "
                       "--user {} --port 22".format(groupe, x["ip"], x["user"]))
        out.append("")
    else:
        out.append("--- Aucune machine sans acces : rien a declarer ---")
        out.append("")

    if o:
        out.append("--- {} acces bastion sans machine correspondante ---".format(o))
        for x in res["orphelins"]:
            out.append("  {:<18} {:<10} {}".format(
                x["adresse"], x["user"], x["motif"]))
        out.append("")
        out.append("  A verifier avant retrait : une IP reattribuee ferait")
        out.append("  suivre l'acces vers une autre machine.")
        for x in res["orphelins"]:
            out.append("    bssh --osh groupDelServer --group {} --host {} "
                       "--user {} --port {}".format(
                           groupe, x["adresse"], x["user"], x["port"]))
        out.append("")
    else:
        out.append("--- Aucun acces orphelin ---")
        out.append("")

    if res["exclus"]:
        out.append("--- {} machine(s) hors perimetre bastion ---".format(
            len(res["exclus"])))
        for x in res["exclus"][:10]:
            out.append("  {:<16} {:<30} ({})".format(
                x["ip"], x["quoi"][:30], x["motif"]))
        if len(res["exclus"]) > 10:
            out.append("  ... et {} autre(s)".format(len(res["exclus"]) - 10))
        out.append("")

    if res["sans_ip"]:
        out.append("--- {} invite(s) sans IP connue, hors comparaison ---".format(
            len(res["sans_ip"])))
        for x in res["sans_ip"][:10]:
            out.append("  " + x)
        if len(res["sans_ip"]) > 10:
            out.append("  ... et {} autre(s)".format(len(res["sans_ip"]) - 10))
        out.append("  (agent QEMU absent ou machine arretee)")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(
        description="Compare l'inventaire et les acces declares dans The Bastion.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--config", default=os.environ.get("INFRA_CONFIG", "infra.yml"))
    ap.add_argument("--acces", help="sortie JSON de selfListAccesses deja capturee")
    ap.add_argument("--commande",
                    help="client bastion a appeler (defaut: bssh --osh "
                         "selfListAccesses --json-greppable)")
    ap.add_argument("--refresh", action="store_true",
                    help="collecte l'inventaire au lieu de lire la base")
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()

    conf = orch.load_config(args.config)
    try:
        acces = lire_acces(args.acces,
                           args.commande.split() if args.commande else None)
    except BastionErreur as exc:
        print("Erreur : " + str(exc), file=sys.stderr)
        return 1

    if args.refresh:
        data = orch.gather(conf, args.workers, conf["pve"].get("detail", True))
    else:
        conn = inventory_db.connect(conf)
        data = inventory_db.load_latest(conn)
        conn.close()
        if data is None:
            print("aucun snapshot en base", file=sys.stderr)
            return 1

    res = comparer(data, acces, conf)
    print(rapport(res, conf))
    # code 1 s'il y a une derive, pour un usage en controle periodique
    return 1 if (res["manquants"] or res["orphelins"]) else 0


if __name__ == "__main__":
    sys.exit(main())
