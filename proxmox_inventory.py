#!/usr/bin/env python3
"""
Inventaire des VM et conteneurs d'un hote Proxmox VE.

Complement de ovh_inventory.py : celui-ci liste le materiel chez OVH, celui-la
descend dans l'hyperviseur installe dessus (reverse du dedie comme --host).

Auth : token API Proxmox, en lecture seule. Sur le noeud PVE :
    pveum user add inventory@pve
    pveum aclmod / --user inventory@pve --role PVEAuditor
    pveum user token add inventory@pve readonly --privsep 0
Le token s'ecrit : inventory@pve!readonly=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

Certificat : Proxmox signe son certificat avec la CA interne du cluster, qui
n'est dans aucun magasin public. Il faut donc lui fournir cette CA, ce qui
donne une verification complete (chaine + nom d'hote), sans rien desactiver :
  --ca-bundle /chemin/pve-root-ca.pem   deja recupere
  --ca-from-ssh root@HOTE               le script va le chercher par SSH

Exemples :
    python proxmox_inventory.py --host pve.exemple.net --ca-from-ssh root@pve.exemple.net
    python proxmox_inventory.py --host pve.exemple.net --ca-bundle pve-root-ca.pem \
        --format csv --out vms.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import ssl
import subprocess
import sys
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

COLUMNS = [
    "node", "type", "vmid", "name", "status", "cpus", "ram_mb", "disk_gb",
    "uptime", "os", "ipv4", "tags",
]


class PveError(RuntimeError):
    def __init__(self, message, fatal=False):
        super().__init__(message)
        self.fatal = fatal


class PinnedAdapter(HTTPAdapter):
    """Utilise le certificat du noeud lui-meme comme ancre de confiance.

    Rien n'est desactive : la chaine et le nom d'hote restent verifies.
    VERIFY_X509_PARTIAL_CHAIN autorise simplement une ancre qui n'est pas une
    racine, ce qui permet d'epingler le certificat feuille de PVE.
    """

    def __init__(self, pem, **kw):
        self.ctx = ssl.create_default_context(cadata=pem)
        self.ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        super().__init__(**kw)

    def init_poolmanager(self, *a, **kw):
        kw["ssl_context"] = self.ctx
        return super().init_poolmanager(*a, **kw)

    def proxy_manager_for(self, *a, **kw):
        kw["ssl_context"] = self.ctx
        return super().proxy_manager_for(*a, **kw)


def fingerprint_of(pem):
    der = ssl.PEM_cert_to_DER_cert(pem)
    fp = hashlib.sha256(der).hexdigest().upper()
    return ":".join(fp[i:i + 2] for i in range(0, len(fp), 2))


def pin_certificate(host, port, store=None, expect=None):
    """Recupere le certificat du noeud et le conserve comme ancre.

    Le certificat vient du reseau : c'est de la confiance a la premiere
    connexion. Elle ne vaut que pour cette premiere fois — ensuite toute
    substitution est refusee. Pour la lever completement, comparer l'empreinte
    affichee avec celle du noeud ('pvenode cert info'), ou passer --expect-sha256.
    """
    if store is None:
        # meme emplacement que l'orchestrateur, pour un seul jeu de certificats
        Path("pinned").mkdir(exist_ok=True)
        store = Path("pinned") / (host + ".pem")
    store = Path(store)
    if store.is_file():
        pem = store.read_text(encoding="utf-8")
        return pem, fingerprint_of(pem), False
    try:
        pem = ssl.get_server_certificate((host, port))
    except OSError as exc:
        raise PveError(
            "impossible de lire le certificat de {}:{} ({})".format(host, port, exc),
            fatal=True) from exc
    fp = fingerprint_of(pem)
    if expect:
        attendu = expect.replace("SHA256:", "").replace(":", "").upper()
        if attendu != fp.replace(":", ""):
            raise PveError(
                "empreinte refusee pour {}\n  attendue : {}\n  presentee: {}".format(
                    host, expect, fp),
                fatal=True)
    store.write_text(pem, encoding="utf-8")
    return pem, fp, True


class PveClient:
    def __init__(self, host, token, port=8006, ca_bundle=None, pinned_pem=None,
                 timeout=30):
        self.base = "https://{}:{}/api2/json".format(host, port)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["Authorization"] = "PVEAPIToken=" + token
        if pinned_pem:
            self.session.mount("https://", PinnedAdapter(pinned_pem))
        elif ca_bundle:
            self.session.verify = ca_bundle
        else:
            raise PveError(
                "aucune ancre de confiance : Proxmox signe son certificat avec la "
                "CA interne du cluster, absente des magasins publics.\n"
                "  --ca-bundle pve-root-ca.pem   la CA du cluster (le plus durable)\n"
                "  --ca-from-ssh root@HOTE       va la chercher par SSH\n"
                "  --pin-cert                    epingle le certificat du noeud",
                fatal=True,
            )

    def get(self, path, params=None):
        try:
            r = self.session.get(self.base + path, params=params, timeout=self.timeout)
        except requests.exceptions.SSLError as exc:
            raise PveError(
                "certificat refuse sur " + path + " : " + str(exc)
                + "\nL'empreinte attendue ne correspond pas, ou le CA fourni n'est "
                "pas celui du cluster. Ne passe pas outre : verifie l'empreinte sur "
                "le noeud avec 'pvenode cert info'.",
                fatal=True,
            ) from exc
        except requests.RequestException as exc:
            raise PveError(
                "injoignable : " + self.base + " (" + str(exc) + ")", fatal=True
            ) from exc
        if r.status_code in (401, 403):
            raise PveError(
                "{} sur {} : token invalide ou privileges insuffisants "
                "(role PVEAuditor attendu)".format(r.status_code, path),
                fatal=r.status_code == 401,
            )
        if r.status_code >= 400:
            raise PveError("{} sur {} : {}".format(r.status_code, path, r.text[:200]))
        return (r.json() or {}).get("data")

    def get_safe(self, path, default=None, params=None):
        try:
            return self.get(path, params=params)
        except PveError as exc:
            if exc.fatal:
                raise
            return default


def fetch_ca_over_ssh(target, dest="pve-root-ca.pem"):
    """Rapatrie la CA du cluster par SSH (l'authenticite vient de SSH, pas du web)."""
    try:
        out = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", target, "cat /etc/pve/pve-root-ca.pem"],
            capture_output=True, timeout=60,
        )
    except FileNotFoundError as exc:
        raise PveError("client ssh introuvable sur ce poste", fatal=True) from exc
    except subprocess.TimeoutExpired as exc:
        raise PveError("timeout SSH vers " + target, fatal=True) from exc
    pem = out.stdout.decode("utf-8", "replace")
    if out.returncode != 0 or "BEGIN CERTIFICATE" not in pem:
        raise PveError(
            "echec de la recuperation par SSH ({}) : {}".format(
                out.returncode, out.stderr.decode("utf-8", "replace").strip()[:200]),
            fatal=True,
        )
    Path(dest).write_text(pem, encoding="utf-8")
    return dest


def human_uptime(seconds):
    if not seconds:
        return ""
    seconds = int(seconds)
    d, rest = divmod(seconds, 86400)
    h, rest = divmod(rest, 3600)
    m = rest // 60
    if d:
        return "{}j {}h".format(d, h)
    return "{}h {}min".format(h, m) if h else "{}min".format(m)


def guest_ips(cli, node, kind, vmid):
    """IPv4 d'un invite : agent QEMU pour les VM, config reseau pour les LXC."""
    ips = []
    if kind == "qemu":
        data = cli.get_safe(
            "/nodes/{}/qemu/{}/agent/network-get-interfaces".format(node, vmid), {})
        for iface in (data or {}).get("result", []) or []:
            if iface.get("name", "").startswith("lo"):
                continue
            for addr in iface.get("ip-addresses") or []:
                ip = addr.get("ip-address")
                if addr.get("ip-address-type") == "ipv4" and ip and ip != "127.0.0.1":
                    ips.append(ip)
    else:
        data = cli.get_safe("/nodes/{}/lxc/{}/interfaces".format(node, vmid), [])
        for iface in data or []:
            ip = (iface.get("inet") or "").split("/")[0]
            if ip and not ip.startswith("127."):
                ips.append(ip)
        if not ips:
            conf = cli.get_safe("/nodes/{}/lxc/{}/config".format(node, vmid), {}) or {}
            for key, val in conf.items():
                if key.startswith("net") and isinstance(val, str):
                    for part in val.split(","):
                        if part.startswith("ip=") and part[3:] != "dhcp":
                            ips.append(part[3:].split("/")[0])
    return ips


def guest_os(cli, node, kind, vmid):
    conf = cli.get_safe("/nodes/{}/{}/{}/config".format(node, kind, vmid), {}) or {}
    if kind == "lxc":
        return conf.get("ostype") or ""
    osinfo = cli.get_safe(
        "/nodes/{}/qemu/{}/agent/get-osinfo".format(node, vmid), {}) or {}
    pretty = ((osinfo.get("result") or {}).get("pretty-name")
              or (osinfo.get("result") or {}).get("name"))
    return pretty or conf.get("ostype") or ""


def collect(cli, detail=True, include_templates=False):
    resources = cli.get("/cluster/resources", params={"type": "vm"}) or []
    rows = []
    for res in resources:
        if res.get("template") and not include_templates:
            continue
        kind = res.get("type")
        node = res.get("node")
        vmid = res.get("vmid")
        maxdisk = res.get("maxdisk") or 0
        maxmem = res.get("maxmem") or 0
        row = {
            "node": node,
            "type": kind,
            "vmid": vmid,
            "name": res.get("name") or "",
            "status": res.get("status") or "",
            "cpus": res.get("maxcpu") or "",
            "ram_mb": int(maxmem / 1024 / 1024) if maxmem else "",
            "disk_gb": round(maxdisk / 1024 ** 3, 1) if maxdisk else "",
            "uptime": human_uptime(res.get("uptime")),
            "os": "",
            "ipv4": "",
            "tags": res.get("tags") or "",
        }
        if detail and res.get("status") == "running":
            row["ipv4"] = " ".join(guest_ips(cli, node, kind, vmid))
        if detail:
            row["os"] = guest_os(cli, node, kind, vmid)
        rows.append(row)
    rows.sort(key=lambda r: (r["node"], r["type"], r["vmid"]))
    return rows


def node_summary(cli):
    lines = []
    for n in cli.get_safe("/nodes", []) or []:
        mem = n.get("maxmem") or 0
        used = n.get("mem") or 0
        lines.append(
            "  {} : {} | {} CPU | RAM {}/{} Go | uptime {}".format(
                n.get("node"), n.get("status"), n.get("maxcpu") or "?",
                round(used / 1024 ** 3, 1) if used else "?",
                round(mem / 1024 ** 3, 1) if mem else "?",
                human_uptime(n.get("uptime")) or "?",
            )
        )
    return lines


def render_table(rows, columns):
    if not rows:
        return "(aucune VM ni conteneur)"
    rows = [{k: str(v).replace("|", "/") for k, v in r.items()} for r in rows]
    widths = {c: len(c) for c in columns}
    for r in rows:
        for c in columns:
            widths[c] = max(widths[c], len(str(r.get(c, ""))))
    out = [
        " | ".join(c.ljust(widths[c]) for c in columns),
        "-+-".join("-" * widths[c] for c in columns),
    ]
    for r in rows:
        out.append(" | ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(
        description="Inventaire des VM et conteneurs d'un hote Proxmox VE.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--host", default=os.environ.get("PVE_HOST"),
                    help="hote ou reverse du noeud PVE (defaut: PVE_HOST)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PVE_PORT", 8006)))
    ap.add_argument("--token", default=os.environ.get("PVE_TOKEN"),
                    help="user@realm!tokenid=secret (defaut: PVE_TOKEN)")
    ap.add_argument("--ca-bundle", default=os.environ.get("PVE_CA_BUNDLE"),
                    help="/etc/pve/pve-root-ca.pem recupere sur le noeud")
    ap.add_argument("--ca-from-ssh", metavar="USER@HOTE",
                    help="recupere /etc/pve/pve-root-ca.pem par SSH et l'utilise")
    ap.add_argument("--pin-cert", action="store_true",
                    help="epingle le certificat du noeud (confiance a la premiere "
                         "connexion, puis toute substitution est refusee)")
    ap.add_argument("--expect-sha256", metavar="EMPREINTE",
                    help="avec --pin-cert : n'accepte le certificat que si son "
                         "empreinte correspond (cf. pvenode cert info)")
    ap.add_argument("--pin-store", help="fichier ou conserver le certificat epingle")
    ap.add_argument("--format", default="table", choices=["table", "csv", "json"])
    ap.add_argument("--out", help="fichier de sortie (defaut: stdout)")
    ap.add_argument("--columns", help="colonnes a afficher, separees par des virgules")
    ap.add_argument("--no-detail", action="store_true",
                    help="n'interroge pas l'agent invite (plus rapide, sans IP ni OS)")
    ap.add_argument("--templates", action="store_true",
                    help="inclut aussi les modeles (templates)")
    args = ap.parse_args()

    if not args.host:
        ap.error("--host manquant (ou variable PVE_HOST)")
    if not args.token:
        ap.error("--token manquant (ou variable PVE_TOKEN)")

    columns = [c.strip() for c in args.columns.split(",")] if args.columns else COLUMNS
    inconnues = [c for c in columns if c not in COLUMNS]
    if inconnues:
        ap.error("colonne(s) inconnue(s) : " + ", ".join(inconnues))

    ca_bundle = args.ca_bundle
    pinned = None
    try:
        if args.ca_from_ssh:
            ca_bundle = fetch_ca_over_ssh(args.ca_from_ssh)
            print("CA du cluster recuperee dans " + ca_bundle, file=sys.stderr)
        if args.pin_cert:
            pinned, fp, neuf = pin_certificate(
                args.host, args.port, args.pin_store, args.expect_sha256)
            print("certificat {} : {}".format(
                "epingle (premiere connexion)" if neuf else "deja epingle", fp),
                file=sys.stderr)
            if neuf and not args.expect_sha256:
                print("  verifie-la sur le noeud : pvenode cert info | grep -i "
                      "fingerprint", file=sys.stderr)
        cli = PveClient(args.host, args.token, port=args.port,
                        ca_bundle=ca_bundle, pinned_pem=pinned)
        version = cli.get_safe("/version", {}) or {}
        print("Proxmox VE {} sur {}".format(
            version.get("version", "?"), args.host), file=sys.stderr)
        for line in node_summary(cli):
            print(line, file=sys.stderr)
        rows = collect(cli, detail=not args.no_detail,
                       include_templates=args.templates)
    except PveError as exc:
        print("Erreur : " + str(exc), file=sys.stderr)
        return 1

    if args.format == "json":
        payload = json.dumps(rows, indent=2, ensure_ascii=False)
        if args.out:
            Path(args.out).write_text(payload + "\n", encoding="utf-8")
        else:
            print(payload)
    elif args.format == "csv":
        fh = open(args.out, "w", newline="", encoding="utf-8-sig") if args.out else sys.stdout
        try:
            w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore", delimiter=";")
            w.writeheader()
            w.writerows(rows)
        finally:
            if args.out:
                fh.close()
    else:
        text = render_table(rows, columns)
        if args.out:
            Path(args.out).write_text(text + "\n", encoding="utf-8")
        else:
            print(text)

    running = sum(1 for r in rows if r["status"] == "running")
    qemu = sum(1 for r in rows if r["type"] == "qemu")
    lxc = sum(1 for r in rows if r["type"] == "lxc")
    print("\nTotal : {} invite(s) : {} VM, {} LXC, {} en marche".format(
        len(rows), qemu, lxc, running), file=sys.stderr)
    if args.out:
        print("Ecrit dans " + args.out, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
