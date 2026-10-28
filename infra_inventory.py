#!/usr/bin/env python3
"""
Inventaire d'infrastructure OVH -> Proxmox, au format inventaire dynamique Ansible.

Chaine complete :
    API OVH          liste les serveurs dedies (reverse, offre, datacenter, expiration)
      -> API PVE     interroge l'hyperviseur de chaque dedie
      -> Ansible     sort un inventaire JSON avec groupes et hostvars

Les clusters sont dedupliques : plusieurs dedies membres du meme cluster PVE ne
sont interroges qu'une fois (/cluster/resources renvoie deja tout le cluster).

Usage Ansible :
    ansible-inventory -i infra_inventory.py --list
    ansible-playbook -i infra_inventory.py site.yml

Le resultat est mis en cache (TTL configurable) : Ansible relance l'inventaire a
chaque execution, on ne veut pas 30 interrogations d'API a chaque fois.

Demarrage :
    python infra_inventory.py --init-config      cree infra.yml commente
    python infra_inventory.py --collect-ca       rapatrie les CA PVE par SSH
    python infra_inventory.py --table            inventaire lisible a l'ecran
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from fnmatch import fnmatch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

from ovh_inventory import OvhClient, OvhError, ENDPOINTS  # noqa: E402
from ovh_inventory import load_credentials as load_ovh_credentials  # noqa: E402
import inventory_db  # noqa: E402
from proxmox_inventory import (PveClient, PveError, pin_certificate,  # noqa: E402
                               collect as collect_guests)

DEFAULT_CONFIG = "infra.yml"

CONFIG_TEMPLATE = """\
# Inventaire d'infrastructure OVH -> Proxmox
ovh:
  endpoint: ovh-eu
  # cles API : laisser vide pour utiliser ~/.ovh.conf ou les variables
  # OVH_APPLICATION_KEY / OVH_APPLICATION_SECRET / OVH_CONSUMER_KEY
  config: ~/.ovh.conf

pve:
  port: 8006
  # CA concatenees de tous les clusters (cf. --collect-ca)
  ca_bundle: pve-ca-bundle.pem
  # token utilise par defaut ; "env:NOM" lit une variable d'environnement
  # sans CA sous la main : epingle le certificat de chaque noeud
  pin_certs: false
  pin_dir: pinned
  default_token: "env:PVE_TOKEN"
  # token specifique par hote, quand il differe du defaut
  tokens: {}
  #  ns1234.ip-1-2-3.eu: "inventory@pve!readonly=xxxxxxxx-..."
  # serveurs OVH qui ne sont pas des hyperviseurs
  skip: []
  #  - ns9999.ip-9-9-9.eu
  # interroge l'agent invite pour l'OS et les IP (plus lent)
  detail: true

cache:
  path: .infra_cache.json
  ttl: 900

ansible:
  # variables ajoutees a tous les invites
  defaults: {}
  #  ansible_user: ansible
"""


def log(msg):
    print(msg, file=sys.stderr)


def load_config(path):
    p = Path(path).expanduser()
    if not p.is_file():
        raise SystemExit(
            "config introuvable : {}\nCree-la avec : python {} --init-config".format(
                p, Path(__file__).name))
    conf = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    conf.setdefault("ovh", {})
    conf.setdefault("pve", {})
    conf.setdefault("cache", {})
    conf.setdefault("ansible", {})
    conf.setdefault("db", {})
    return conf


def resolve_secret(value):
    """Permet "env:NOM" pour ne pas ecrire un token en clair dans la config."""
    if isinstance(value, str) and value.startswith("env:"):
        return os.environ.get(value[4:])
    return value


def token_for(conf, host):
    tokens = conf["pve"].get("tokens") or {}
    return resolve_secret(tokens.get(host) or conf["pve"].get("default_token"))


# --------------------------------------------------------------------------
# OVH : les machines physiques
# --------------------------------------------------------------------------

def fetch_ovh_servers(conf, workers):
    args = type("A", (), {
        "config": conf["ovh"].get("config"),
        "endpoint": conf["ovh"].get("endpoint", "ovh-eu"),
    })()
    creds = load_ovh_credentials(args)
    cli = OvhClient(**creds)
    names = cli.get_safe("/dedicated/server", []) or []
    log("OVH : {} serveur(s) dedie(s)".format(len(names)))

    def one(name):
        base = "/dedicated/server/" + name
        d = cli.get_safe(base, {}) or {}
        svc = cli.get_safe(base + "/serviceInfos", {}) or {}
        hw = cli.get_safe(base + "/specifications/hardware", {}) or {}

        groupes = hw.get("diskGroups") or []
        nb_disques = sum(g.get("numberOfDisks") or 0 for g in groupes)
        desc_disques = ", ".join(
            "{}x{}{} {}".format(
                g.get("numberOfDisks", "?"),
                (g.get("diskSize") or {}).get("value", "?"),
                (g.get("diskSize") or {}).get("unit", ""),
                g.get("diskType", "")).strip()
            for g in groupes)
        sockets = hw.get("numberOfProcessors") or (1 if hw.get("processorName") else None)
        coeurs = hw.get("coresPerProcessor")
        threads = hw.get("threadsPerProcessor")
        ram = (hw.get("memorySize") or {}).get("value")

        return {
            "ovh_name": name,
            "reverse": d.get("reverse") or name,
            "ip": d.get("ip") or "",
            "offer": d.get("commercialRange") or "",
            "datacenter": d.get("datacenter") or "",
            "os": d.get("os") or "",
            "state": d.get("state") or "",
            "expiration": svc.get("expiration") or "",
            "cpu_model": hw.get("processorName") or "",
            "cpu_sockets": sockets,
            # totaux, pas "par processeur" : plus parlant dans un inventaire
            "cpu_cores": (coeurs * sockets) if (coeurs and sockets) else coeurs,
            "cpu_threads": (threads * sockets) if (threads and sockets) else threads,
            "ram_mb": ram,
            "disk_count": nb_disques or None,
            "disk_desc": desc_disques,
        }

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, names))


# --------------------------------------------------------------------------
# Proxmox : identification des clusters puis collecte
# --------------------------------------------------------------------------

def pve_client(conf, host):
    token = token_for(conf, host)
    if not token:
        raise PveError("aucun token pour " + host, fatal=True)
    port = conf["pve"].get("port", 8006)
    pinned = None
    if conf["pve"].get("pin_certs"):
        # un certificat epingle par noeud, conserve entre deux executions
        store = Path(conf["pve"].get("pin_dir", "pinned"))
        store.mkdir(exist_ok=True)
        pinned, _, _ = pin_certificate(host, port, store / (host + ".pem"))
    return PveClient(
        host, token, port=port,
        ca_bundle=conf["pve"].get("ca_bundle"),
        pinned_pem=pinned,
    )


def node_hardware(cli, node):
    """Materiel d'un noeud PVE : CPU, RAM, disques physiques, versions."""
    st = cli.get_safe("/nodes/{}/status".format(node), {}) or {}
    cpu = st.get("cpuinfo") or {}
    mem = st.get("memory") or {}
    disques = cli.get_safe("/nodes/{}/disks/list".format(node), []) or []

    total_octets = sum(d.get("size") or 0 for d in disques)
    modeles = {}
    for d in disques:
        cle = "{} {}".format(d.get("type", ""), d.get("model", "")).strip()
        modeles[cle] = modeles.get(cle, 0) + 1
    desc = ", ".join("{}x {}".format(n, m) for m, n in sorted(modeles.items()))

    try:
        mhz = int(float(cpu.get("mhz"))) if cpu.get("mhz") else None
    except (TypeError, ValueError):
        mhz = None

    return {
        "cpu_model": cpu.get("model") or "",
        "cpu_sockets": cpu.get("sockets"),
        "cpu_cores": cpu.get("cores"),
        "cpu_logical": cpu.get("cpus"),
        "cpu_mhz": mhz,
        "ram_total_mb": int(mem["total"] / 1024 ** 2) if mem.get("total") else None,
        "ram_used_mb": int(mem["used"] / 1024 ** 2) if mem.get("used") else None,
        "disk_count": len(disques) or None,
        "disk_total_gb": round(total_octets / 1024 ** 3, 1) if total_octets else None,
        "disk_desc": desc,
        # "pve-manager/9.2.21/4f6e0ac8" : seule la version nous interesse
        "pve_version": (st.get("pveversion") or "").split("/")[1]
                       if "/" in (st.get("pveversion") or "") else st.get("pveversion"),
        "kernel": (st.get("kversion") or "")[:120],
    }


def probe_cluster(conf, server):
    """Identifie le cluster d'un hote : son nom, ses membres, son etat."""
    host = server["reverse"]
    out = {"server": server, "host": host, "cluster": None,
           "node": None, "members": [], "error": None, "hw": {}}
    try:
        cli = pve_client(conf, host)
        status = cli.get("/cluster/status") or []
    except (PveError, OvhError) as exc:
        out["error"] = str(exc).splitlines()[0]
        return out
    for item in status:
        if item.get("type") == "cluster":
            out["cluster"] = item.get("name")
        elif item.get("type") == "node":
            out["members"].append({
                "name": item.get("name"),
                "ip": item.get("ip") or "",
                "online": item.get("online", 1),
            })
            if item.get("local"):
                out["node"] = item.get("name")
    # un noeud seul ne renvoie pas d'entree "cluster"
    if not out["cluster"]:
        out["cluster"] = "standalone:" + (out["node"] or host)
    if out["node"]:
        out["hw"] = node_hardware(cli, out["node"])
    return out


def collect_cluster(conf, probe, detail):
    host = probe["host"]
    try:
        cli = pve_client(conf, host)
        guests = collect_guests(cli, detail=detail, include_templates=False)
    except PveError as exc:
        probe["error"] = str(exc).splitlines()[0]
        return []
    for g in guests:
        g["cluster"] = probe["cluster"]
        g["via_host"] = host
    return guests


def gather(conf, workers, detail):
    servers = fetch_ovh_servers(conf, workers)
    skip = set(conf["pve"].get("skip") or [])
    targets = [s for s in servers
               if s["reverse"] not in skip and s["ovh_name"] not in skip]
    # "seulement" l'emporte sur skip : il sert a eprouver la collecte sur deux
    # ou trois hotes avant de la lancer sur tout le parc.
    seulement = [m for m in (conf["pve"].get("seulement") or []) if m]
    if seulement:
        def vise(s):
            return any(fnmatch(s["reverse"], m) or fnmatch(s["ovh_name"], m)
                       for m in seulement)
        avant = len(targets)
        targets = [s for s in targets if vise(s)]
        log("PVE : ciblage partiel actif, {} hote(s) retenu(s) sur {}".format(
            len(targets), avant))
    log("PVE : {} hote(s) a sonder ({} ignore(s))".format(
        len(targets), len(servers) - len(targets)))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        probes = list(pool.map(lambda s: probe_cluster(conf, s), targets))

    # un seul representant par cluster : /cluster/resources renvoie deja tout
    representatives, seen = [], {}
    for pr in probes:
        if pr["error"]:
            continue
        if pr["cluster"] in seen:
            seen[pr["cluster"]].append(pr["host"])
            continue
        seen[pr["cluster"]] = []
        representatives.append(pr)
    doublons = sum(len(v) for v in seen.values())
    log("PVE : {} cluster(s) distinct(s), {} hote(s) membres d'un cluster deja "
        "couvert".format(len(representatives), doublons))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        batches = list(pool.map(lambda p: collect_cluster(conf, p, detail),
                                representatives))

    guests = [g for batch in batches for g in batch]
    injoignables = [p for p in probes if p["error"]]
    for p in injoignables:
        log("  ! {} : {}".format(p["host"], p["error"]))
    log("Total : {} invite(s) sur {} cluster(s) ; {} hote(s) injoignable(s)".format(
        len(guests), len(representatives), len(injoignables)))
    return {"servers": servers, "probes": probes, "guests": guests,
            "generated_at": int(time.time())}


# --------------------------------------------------------------------------
# Rendu inventaire Ansible
# --------------------------------------------------------------------------

def add(groups, name, host):
    groups.setdefault(name, {"hosts": []})
    if host not in groups[name]["hosts"]:
        groups[name]["hosts"].append(host)


def safe_group(value):
    out = "".join(c if c.isalnum() else "_" for c in str(value))
    return out.strip("_").lower() or "inconnu"


def vars_connexion(conf, niveau):
    """Variables de connexion d'un hote : compte cible et bastion.

    bastion_user est volontairement absent. Le wrapper The Bastion donne la
    priorite a l'inventaire sur l'environnement : y figer un compte ecraserait
    le BASTION_USER de chaque operateur, alors que les acces sont nominatifs.
    """
    out = {}
    compte = (conf["ansible"].get("comptes") or {}).get(niveau)
    if compte:
        out["ansible_user"] = compte
    bastion = conf["ansible"].get("bastion") or {}
    if bastion.get("actif") and bastion.get("host"):
        out["bastion_host"] = bastion["host"]
        out["bastion_port"] = bastion.get("port", 22)
    return out


def build_ansible(data, conf):
    groups = {}
    hostvars = {}
    defaults = conf["ansible"].get("defaults") or {}

    by_host = {p["host"]: p for p in data["probes"]}

    # les hyperviseurs eux-memes
    for srv in data["servers"]:
        host = srv["reverse"]
        probe = by_host.get(host)
        if probe is None:
            continue
        hv = dict(defaults)
        hv.update(vars_connexion(conf, "node"))
        hv.update({
            "ansible_host": srv["ip"] or host,
            "ovh_name": srv["ovh_name"],
            "ovh_offer": srv["offer"],
            "ovh_datacenter": srv["datacenter"],
            "ovh_expiration": srv["expiration"],
            "ovh_state": srv["state"],
            "pve_node": probe["node"],
            "pve_cluster": probe["cluster"],
            # materiel vu par OVH (la facture) ...
            "ovh_cpu_model": srv.get("cpu_model"),
            "ovh_cpu_sockets": srv.get("cpu_sockets"),
            "ovh_cpu_cores": srv.get("cpu_cores"),
            "ovh_cpu_threads": srv.get("cpu_threads"),
            "ovh_ram_mb": srv.get("ram_mb"),
            "ovh_disk_count": srv.get("disk_count"),
            "ovh_disks": srv.get("disk_desc"),
        })
        # ... et materiel vu par l'hyperviseur (la realite)
        for cle, val in (probe.get("hw") or {}).items():
            if val not in (None, ""):
                hv["hw_" + cle] = val
        if probe["error"]:
            hv["pve_error"] = probe["error"]
            add(groups, "pve_unreachable", host)
        else:
            add(groups, "pve_nodes", host)
            add(groups, "cluster_" + safe_group(probe["cluster"]), host)
        if srv["datacenter"]:
            add(groups, "dc_" + safe_group(srv["datacenter"]), host)
        hostvars[host] = hv

    # les invites
    srv_by_reverse = {s["reverse"]: s for s in data["servers"]}
    for g in data["guests"]:
        base = g.get("name") or "{}-{}".format(g["type"], g["vmid"])
        # un nom d'hote Ansible doit etre unique : deux clusters peuvent heberger
        # un "web01" avec le meme vmid, d'ou le cluster en dernier recours
        name = base
        if name in hostvars:
            name = "{}-{}".format(base, g["vmid"])
        if name in hostvars:
            name = "{}-{}-{}".format(base, safe_group(g.get("cluster")), g["vmid"])
        ip = (g.get("ipv4") or "").split(" ")[0]
        parent = srv_by_reverse.get(g.get("via_host"), {})
        hv = dict(defaults)
        hv.update(vars_connexion(conf, "vm"))
        hv.update({
            "pve_vmid": g["vmid"],
            "pve_type": g["type"],
            "pve_node": g["node"],
            "pve_cluster": g.get("cluster"),
            "pve_status": g["status"],
            "pve_cpus": g.get("cpus"),
            "pve_ram_mb": g.get("ram_mb"),
            "pve_disk_gb": g.get("disk_gb"),
            "pve_tags": g.get("tags"),
            "pve_os": g.get("os"),
            "pve_host": g.get("via_host"),
            "ovh_server": parent.get("ovh_name"),
            "ovh_datacenter": parent.get("datacenter"),
            "ovh_offer": parent.get("offer"),
            "host_cpu_model": parent.get("cpu_model"),
            "host_cpu_cores": parent.get("cpu_cores"),
            "host_disks": parent.get("disk_desc"),
        })
        if ip:
            hv["ansible_host"] = ip
        else:
            add(groups, "guests_sans_ip", name)
        hostvars[name] = hv

        add(groups, "pve_guests", name)
        add(groups, "type_" + safe_group(g["type"]), name)
        add(groups, "status_" + safe_group(g["status"]), name)
        add(groups, "node_" + safe_group(g["node"]), name)
        add(groups, "cluster_" + safe_group(g.get("cluster")), name)
        if parent.get("datacenter"):
            add(groups, "dc_" + safe_group(parent["datacenter"]), name)
        for tag in (g.get("tags") or "").replace(",", ";").split(";"):
            if tag.strip():
                add(groups, "tag_" + safe_group(tag.strip()), name)

    inventory = {"_meta": {"hostvars": hostvars}}
    inventory.update(groups)
    inventory["all"] = {"children": sorted(
        [g for g in groups] + ["ungrouped"])}
    return inventory


def commandes_bastion(data, conf):
    """Commandes osh pour declarer l'inventaire dans un groupe The Bastion.

    Un groupe porte les acces : les serveurs qu'il contient, et les comptes
    qui en sont membres. Declarer 30 hyperviseurs et leurs VM a la main est
    une source d'oubli ; mieux vaut les deriver de l'inventaire.
    """
    bastion = conf["ansible"].get("bastion") or {}
    groupe = bastion.get("groupe")
    if not groupe:
        raise SystemExit("ansible.bastion.groupe non defini dans la config")
    comptes = conf["ansible"].get("comptes") or {}

    lignes = [
        "# Commandes a jouer depuis ton compte bastion (acces nominatif).",
        "# Le groupe doit exister :",
        "#   bssh --osh groupCreate --group {} --owner <toi> \\".format(groupe),
        "#        --algo ed25519 --size 256",
        "# Les membres s'ajoutent a part, c'est ce qui limite qui peut passer :",
        "#   bssh --osh groupAddMember --group {} --account <collegue>".format(groupe),
        "",
    ]

    probes = {p["host"]: p for p in (data.get("probes") or [])}
    vus = set()

    lignes.append("# --- hyperviseurs " + "-" * 50)
    for srv in sorted(data.get("servers") or [], key=lambda s: s.get("reverse") or ""):
        pr = probes.get(srv.get("reverse"))
        if pr is None or pr.get("error"):
            continue
        cible = srv.get("ip") or srv.get("reverse")
        if not cible or cible in vus:
            continue
        vus.add(cible)
        lignes.append(
            "bssh --osh groupAddServer --group {} --host {} --user {} --port 22 "
            "--comment '{}'".format(
                groupe, cible, comptes.get("node", "root"),
                (pr.get("node") or srv.get("reverse") or "").replace("'", "")))

    sans_ip = 0
    lignes.append("")
    lignes.append("# --- invites " + "-" * 55)
    for g in sorted(data.get("guests") or [],
                    key=lambda x: (x.get("via_host") or "", x.get("vmid") or 0)):
        ip = (g.get("ipv4") or "").split(" ")[0]
        if not ip:
            sans_ip += 1
            continue
        if ip in vus:
            continue
        vus.add(ip)
        lignes.append(
            "bssh --osh groupAddServer --group {} --host {} --user {} --port 22 "
            "--comment '{} ({})'".format(
                groupe, ip, comptes.get("vm", "ansible"),
                (g.get("name") or "").replace("'", ""), g.get("vmid")))

    if sans_ip:
        lignes += ["",
                   "# {} invite(s) sans IP connue, non declarables : agent QEMU "
                   "absent ou machine arretee.".format(sans_ip)]
    lignes += ["",
               "# Retrait d'un serveur devenu obsolete :",
               "#   bssh --osh groupDelServer --group {} --host <ip> "
               "--user <user> --port 22".format(groupe)]
    return "\n".join(lignes)


def render_table(data):
    lines = []
    srv_by_reverse = {s["reverse"]: s for s in data["servers"]}
    by_cluster = {}
    for g in data["guests"]:
        by_cluster.setdefault(g.get("cluster"), []).append(g)
    for probe in sorted(data["probes"], key=lambda p: p["host"]):
        srv = srv_by_reverse.get(probe["host"], {})
        head = "{}  [{} | {}]".format(
            probe["host"], srv.get("offer", "?"), srv.get("datacenter", "?"))
        if probe["error"]:
            lines.append(head + "  -> INJOIGNABLE : " + probe["error"])
            continue
        guests = by_cluster.get(probe["cluster"], [])
        own = [g for g in guests if g["node"] == probe["node"]]
        hw = probe.get("hw") or {}
        materiel = ""
        if hw.get("cpu_model"):
            materiel = "\n    {} | {}c/{}t | {} Go RAM | {} disque(s) {}".format(
                hw["cpu_model"], hw.get("cpu_cores") or "?",
                hw.get("cpu_logical") or "?",
                round((hw.get("ram_total_mb") or 0) / 1024) or "?",
                hw.get("disk_count") or "?", hw.get("disk_desc") or "")
        lines.append("{}  cluster={} noeud={}  ({} invite(s)){}".format(
            head, probe["cluster"], probe["node"], len(own), materiel))
        for g in sorted(own, key=lambda x: x["vmid"]):
            lines.append("    {:>6}  {:<24} {:<8} {:<9} {}".format(
                g["vmid"], (g.get("name") or "")[:24], g["type"],
                g["status"], g.get("ipv4") or ""))
    return "\n".join(lines) or "(rien)"


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

def cache_read(conf, ttl):
    path = Path(conf["cache"].get("path", ".infra_cache.json"))
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    age = time.time() - payload.get("generated_at", 0)
    if age > ttl:
        return None
    log("cache : {}s d'age, reutilise (TTL {}s)".format(int(age), ttl))
    return payload


def cache_write(conf, data):
    path = Path(conf["cache"].get("path", ".infra_cache.json"))
    try:
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError as exc:
        log("cache non ecrit : " + str(exc))


# --------------------------------------------------------------------------
# CA : rapatriement par SSH
# --------------------------------------------------------------------------

def collect_ca(conf, workers, ssh_user="root", hosts=None):
    """Sans --hosts, la liste vient d'OVH (donc des cles API sont necessaires)."""
    if hosts:
        log("CA : {} hote(s) donne(s) en argument".format(len(hosts)))
    else:
        servers = fetch_ovh_servers(conf, workers)
        skip = set(conf["pve"].get("skip") or [])
        hosts = [s["reverse"] for s in servers if s["reverse"] not in skip]
    bundle = Path(conf["pve"].get("ca_bundle", "pve-ca-bundle.pem"))

    def one(host):
        try:
            r = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                 "{}@{}".format(ssh_user, host), "cat /etc/pve/pve-root-ca.pem"],
                capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return host, None, str(exc)[:120]
        pem = r.stdout.decode("utf-8", "replace")
        if r.returncode != 0 or "BEGIN CERTIFICATE" not in pem:
            return host, None, r.stderr.decode("utf-8", "replace").strip()[:120]
        return host, pem.strip(), None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, hosts))

    pems, echecs = [], []
    for host, pem, err in results:
        if pem and pem not in pems:   # un cluster = une seule CA
            pems.append(pem)
        elif err:
            echecs.append((host, err))
    for host, err in echecs:
        log("  ! {} : {}".format(host, err))
    if not pems:
        # ne jamais remplacer un bundle existant par du vide : toutes les
        # collectes suivantes echoueraient sur un certificat non verifiable
        log("aucune CA recuperee, {} laisse intact".format(bundle))
        return 1
    bundle.write_text("\n".join(pems) + "\n", encoding="utf-8")
    log("{} CA distincte(s) ecrite(s) dans {}".format(len(pems), bundle))
    return 1 if echecs else 0


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Inventaire OVH -> Proxmox au format Ansible.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--list", action="store_true",
                    help="inventaire complet au format Ansible (defaut)")
    ap.add_argument("--host", metavar="NOM",
                    help="hostvars d'une machine (Ansible ; tout est deja dans _meta)")
    ap.add_argument("--config", default=os.environ.get("INFRA_CONFIG", DEFAULT_CONFIG))
    ap.add_argument("--init-config", action="store_true",
                    help="ecrit un " + DEFAULT_CONFIG + " commente puis s'arrete")
    ap.add_argument("--collect-ca", action="store_true",
                    help="rapatrie les CA PVE par SSH et construit le bundle")
    ap.add_argument("--ssh-user", default="root")
    ap.add_argument("--hosts",
                    help="avec --collect-ca : hotes separes par des virgules, "
                         "au lieu de passer par l'API OVH")
    ap.add_argument("--seulement", metavar="MOTIFS",
                    help="n'interroge que ces hotes (motifs separes par des "
                         "virgules) : pour eprouver la collecte avant de la "
                         "lancer sur tout le parc")
    ap.add_argument("--refresh", action="store_true", help="ignore le cache")
    ap.add_argument("--store-db", action="store_true",
                    help="enregistre la collecte comme snapshot en base")
    ap.add_argument("--from-db", action="store_true",
                    help="n'interroge rien : relit le dernier snapshot en base")
    ap.add_argument("--ttl", type=int, help="duree de vie du cache en secondes")
    ap.add_argument("--bastion-commands", action="store_true",
                    help="genere les commandes osh groupAddServer pour declarer "
                         "l'inventaire dans le groupe The Bastion")
    ap.add_argument("--table", action="store_true",
                    help="sortie lisible a l'ecran au lieu du JSON Ansible")
    ap.add_argument("--out", help="ecrit la sortie dans un fichier")
    ap.add_argument("--no-detail", action="store_true",
                    help="n'interroge pas les agents invites (plus rapide)")
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()

    if args.init_config:
        dest = Path(args.config)
        if dest.exists():
            raise SystemExit(str(dest) + " existe deja, rien ecrit")
        dest.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        log("ecrit : " + str(dest))
        return 0

    conf = load_config(args.config)
    if getattr(args, "seulement", None):
        conf["pve"]["seulement"] = [m.strip() for m in args.seulement.split(",")]

    if args.collect_ca:
        hosts = [h.strip() for h in args.hosts.split(",")] if args.hosts else None
        return collect_ca(conf, args.workers, args.ssh_user, hosts)

    if args.host:
        print(json.dumps({}))
        return 0

    # --from-db : on ne collecte rien, on relit le dernier snapshot. C'est le
    # mode normal pour Ansible une fois la collecte planifiee a part.
    if args.from_db:
        try:
            conn = inventory_db.connect(conf)
            data = inventory_db.load_latest(conn)
            conn.close()
        except inventory_db.DbError as exc:
            log("Erreur : " + str(exc))
            return 1
        if data is None:
            log("Erreur : aucun snapshot exploitable en base "
                "(lance une collecte avec --store-db)")
            return 1
        age = int(time.time() - data["generated_at"])
        log("snapshot #{} ({}) : {} serveur(s), {} invite(s), {}s d'age".format(
            data["run_id"], data["run_status"], len(data["servers"]),
            len(data["guests"]), age))
    else:
        ttl = args.ttl if args.ttl is not None else conf["cache"].get("ttl", 900)
        data = None if args.refresh else cache_read(conf, ttl)
        if data is None:
            detail = conf["pve"].get("detail", True) and not args.no_detail
            try:
                data = gather(conf, args.workers, detail)
            except (OvhError, PveError) as exc:
                log("Erreur : " + str(exc))
                return 1
            if data["servers"]:
                cache_write(conf, data)
            else:
                # un inventaire vide vient plus souvent d'un probleme d'acces
                # que d'un parc vide : le cacher masquerait l'erreur
                log("aucun serveur trouve : resultat non mis en cache")
            if args.store_db:
                try:
                    conn = inventory_db.connect(conf)
                    run_id = inventory_db.store_run(conn, data)
                    log("snapshot #{} enregistre en base".format(run_id))
                    garde = conf.get("db", {}).get("keep_runs")
                    if garde:
                        n = inventory_db.purge(conn, int(garde))
                        if n:
                            log("{} ancien(s) run(s) purge(s)".format(n))
                    conn.close()
                except inventory_db.DbError as exc:
                    log("base non alimentee : " + str(exc))
                    return 1
        elif args.store_db:
            log("snapshot non enregistre : donnees issues du cache "
                "(ajoute --refresh pour collecter puis stocker)")

    # une collecte planifiee (--store-db seul) ne doit rien ecrire sur stdout :
    # sinon l'inventaire complet finit dans le journal a chaque passage
    if (args.store_db and not args.list and not args.table and not args.out
            and not args.bastion_commands):
        return 0

    if args.bastion_commands:
        text = commandes_bastion(data, conf)
    else:
        text = render_table(data) if args.table else json.dumps(
            build_ansible(data, conf), indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        log("ecrit dans " + args.out)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
