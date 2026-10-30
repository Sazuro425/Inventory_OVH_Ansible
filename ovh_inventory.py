#!/usr/bin/env python3
"""
Inventaire des machines OVHcloud via l'API publique.

Couvre : serveurs dedies, VPS, instances Public Cloud et Hosted Private Cloud
(VMware).

Auth : cles API OVH (https://api.ovh.com/createToken/) passees par variables
d'environnement ou par ~/.ovh.conf. Aucune dependance au SDK ovh : la
signature est calculee a la main (requests seulement).

Droits a demander sur le token (lecture pure) :
    GET /dedicated/server*
    GET /vps*
    GET /me               (optionnel : juste pour afficher le compte utilise)
    GET /cloud/project*   (si tu as des projets Public Cloud)
    GET /dedicatedCloud*  (si tu as du Hosted Private Cloud)

Un droit manquant ou un produit que tu n'as pas ne bloque pas l'inventaire :
la route est signalee sur stderr et le reste continue.

Exemples :
    python ovh_inventory.py
    python ovh_inventory.py --format csv --out inventaire.csv
    python ovh_inventory.py --kinds all --format json
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from configparser import ConfigParser
from pathlib import Path

import requests

import secrets_infra

ENDPOINTS = {
    # api.ovh.com est l'alias qui repond correctement ; eu.api.ovh.com sert un
    # certificat auto-signe depuis certains reseaux (voir --endpoint ovh-eu-alt).
    "ovh-eu": "https://api.ovh.com/1.0",
    "ovh-eu-alt": "https://eu.api.ovh.com/1.0",
    "ovh-ca": "https://ca.api.ovh.com/1.0",
    "ovh-us": "https://api.us.ovhcloud.com/1.0",
    "kimsufi-eu": "https://eu.api.kimsufi.com/1.0",
    "kimsufi-ca": "https://ca.api.kimsufi.com/1.0",
    "soyoustart-eu": "https://eu.api.soyoustart.com/1.0",
    "soyoustart-ca": "https://ca.api.soyoustart.com/1.0",
}

COLUMNS = [
    "type", "id", "name", "state", "offer", "datacenter",
    "os", "cpu", "ram_mb", "disk", "ipv4", "ipv6", "project", "expiration",
]


class OvhError(RuntimeError):
    """fatal=True : inutile de continuer (TLS, reseau, auth globale)."""

    def __init__(self, message, fatal=False):
        super().__init__(message)
        self.fatal = fatal


class OvhClient:
    """Client minimaliste : signature OVH v1 (SHA1) + retry/backoff."""

    def __init__(self, endpoint, app_key, app_secret, consumer_key, timeout=30,
                 ca_bundle=None):
        if endpoint not in ENDPOINTS:
            raise OvhError(
                "endpoint inconnu : {} (attendu : {})".format(
                    endpoint, ", ".join(ENDPOINTS)
                )
            )
        self.base = ENDPOINTS[endpoint]
        self.app_key = app_key
        self.app_secret = app_secret
        self.consumer_key = consumer_key
        self.timeout = timeout
        self.session = requests.Session()
        if ca_bundle:
            self.session.verify = ca_bundle
        self._delta = None

    def _time_delta(self):
        """Delta avec l'horloge OVH, calcule une seule fois (avant le parallelisme)."""
        if self._delta is None:
            try:
                r = self.session.get(self.base + "/auth/time", timeout=self.timeout)
                r.raise_for_status()
            except requests.exceptions.SSLError as exc:
                raise OvhError(
                    "handshake TLS refuse vers " + self.base + " : " + str(exc)
                    + "\nLe certificat presente n'est pas celui d'OVHcloud : une "
                    "inspection SSL (firewall, antivirus, proxy) se trouve sur le "
                    "chemin.\nN'envoie pas tes cles API tant que ce n'est pas resolu. "
                    "Si l'inspection est legitime, passe le CA de ton entreprise avec "
                    "--ca-bundle chemin/vers/ca.pem.",
                    fatal=True,
                ) from exc
            except requests.RequestException as exc:
                raise OvhError(
                    "impossible de joindre " + self.base + " : " + str(exc),
                    fatal=True) from exc
            self._delta = int(r.text) - int(time.time())
        return self._delta

    def get(self, path, retries=4):
        url = self.base + path
        last = None
        for attempt in range(retries):
            ts = str(int(time.time()) + self._time_delta())
            raw = "+".join([self.app_secret, self.consumer_key, "GET", url, "", ts])
            sig = "$1$" + hashlib.sha1(raw.encode("utf-8")).hexdigest()
            headers = {
                "X-Ovh-Application": self.app_key,
                "X-Ovh-Consumer": self.consumer_key,
                "X-Ovh-Timestamp": ts,
                "X-Ovh-Signature": sig,
                "Content-Type": "application/json",
            }
            try:
                r = self.session.get(url, headers=headers, timeout=self.timeout)
            except requests.exceptions.SSLError as exc:
                raise OvhError(
                    "handshake TLS refuse sur " + path + " : " + str(exc),
                    fatal=True) from exc
            except requests.RequestException as exc:
                last = exc
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = OvhError("{} sur {} : {}".format(r.status_code, path, r.text[:200]))
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (401, 403):
                # OVH renvoie 403 aussi bien pour "cles invalides" que pour
                # "route non autorisee". Confondre les deux ferait passer de
                # mauvaises cles pour un parc vide.
                corps = r.text[:300]
                mauvaises_cles = any(
                    marqueur in corps for marqueur in (
                        "INVALID_KEY", "INVALID_CREDENTIAL", "INVALID_SIGNATURE",
                        "NOT_CREDENTIAL", "key is invalid", "credential is not valid",
                    ))
                if mauvaises_cles:
                    raise OvhError(
                        "identifiants refuses sur {} : {}\nVerifie les trois cles "
                        "(application key / secret / consumer key) et l'endpoint "
                        "({}).".format(path, corps, self.base),
                        fatal=True)
                raise OvhError(
                    "{} sur {} : le token n'a pas le droit GET sur cette route "
                    "({})".format(r.status_code, path, corps))
            if r.status_code >= 400:
                raise OvhError("{} sur {} : {}".format(r.status_code, path, r.text[:300]))
            return r.json()
        raise OvhError("echec apres {} tentatives sur {} : {}".format(retries, path, last))

    def get_safe(self, path, default=None):
        """GET qui ne fait pas echouer tout l'inventaire pour une ressource isolee."""
        try:
            return self.get(path)
        except OvhError as exc:
            if exc.fatal:
                raise
            print("  ! " + str(exc), file=sys.stderr)
            return default


def load_credentials(args):
    conf = {}
    # expanduser : un "~/.ovh.conf" venant de --config ou d'infra.yml serait
    # sinon pris pour un chemin relatif au repertoire courant
    conf_path = (Path(args.config).expanduser() if args.config
                 else Path.home() / ".ovh.conf")
    if conf_path.is_file():
        parser = ConfigParser()
        parser.read(conf_path, encoding="utf-8")
        section = args.endpoint if parser.has_section(args.endpoint) else "default"
        if parser.has_section(section):
            conf = dict(parser.items(section))

    def pick(env, key):
        # systemd d'abord (le secret n'est alors pas dans l'environnement),
        # puis la variable, puis le fichier de configuration
        return (secrets_infra.lire_secret(env.lower(), env)
                or conf.get(key))

    creds = {
        "endpoint": args.endpoint or pick("OVH_ENDPOINT", "endpoint") or "ovh-eu",
        "app_key": pick("OVH_APPLICATION_KEY", "application_key"),
        "app_secret": pick("OVH_APPLICATION_SECRET", "application_secret"),
        "consumer_key": pick("OVH_CONSUMER_KEY", "consumer_key"),
    }
    missing = [k for k in ("app_key", "app_secret", "consumer_key") if not creds[k]]
    if missing:
        raise OvhError(
            "cles API manquantes : "
            + ", ".join(missing)
            + "\nDefinis OVH_APPLICATION_KEY / OVH_APPLICATION_SECRET / "
            "OVH_CONSUMER_KEY, ou cree "
            + str(conf_path)
            + " (section [ovh-eu]).\nGeneration des cles : "
            "https://api.ovh.com/createToken/"
        )
    return creds


def row(**kw):
    base = dict.fromkeys(COLUMNS, "")
    base.update({k: v for k, v in kw.items() if v is not None})
    return base


def fetch_dedicated(cli, workers):
    names = cli.get_safe("/dedicated/server", []) or []
    print("  serveurs dedies : {}".format(len(names)), file=sys.stderr)

    def one(name):
        d = cli.get_safe("/dedicated/server/" + name, {}) or {}
        spec = cli.get_safe(
            "/dedicated/server/" + name + "/specifications/hardware", {}) or {}
        svc = cli.get_safe("/dedicated/server/" + name + "/serviceInfos", {}) or {}
        ips = cli.get_safe("/dedicated/server/" + name + "/ips", []) or []
        cpu = spec.get("processorName") or ""
        cores = spec.get("coresPerProcessor")
        threads = spec.get("threadsPerProcessor")
        if cores:
            cpu += " ({}c/{}t)".format(cores, threads) if threads else " ({}c)".format(cores)
        disk = ", ".join(
            "{}x{}{} {}".format(
                g.get("numberOfDisks", "?"),
                (g.get("diskSize") or {}).get("value", "?"),
                (g.get("diskSize") or {}).get("unit", ""),
                g.get("diskType", ""),
            ).strip()
            for g in (spec.get("diskGroups") or [])
        )
        return row(
            type="dedicated",
            id=d.get("name", name),
            name=d.get("reverse") or d.get("displayName") or name,
            state=d.get("state"),
            offer=d.get("commercialRange"),
            datacenter=d.get("datacenter"),
            os=d.get("os"),
            cpu=cpu.strip(),
            ram_mb=(spec.get("memorySize") or {}).get("value"),
            disk=disk,
            ipv4=" ".join(i for i in ips if ":" not in i) or d.get("ip", ""),
            ipv6=" ".join(i for i in ips if ":" in i),
            expiration=svc.get("expiration"),
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, names))


def fetch_vps(cli, workers):
    names = cli.get_safe("/vps", []) or []
    print("  VPS : {}".format(len(names)), file=sys.stderr)

    def one(name):
        d = cli.get_safe("/vps/" + name, {}) or {}
        svc = cli.get_safe("/vps/" + name + "/serviceInfos", {}) or {}
        ips = cli.get_safe("/vps/" + name + "/ips", []) or []
        model = d.get("model") or {}
        return row(
            type="vps",
            id=d.get("name", name),
            name=d.get("displayName") or name,
            state=d.get("state"),
            offer=model.get("name") or d.get("offerType"),
            datacenter=d.get("zone") or d.get("cluster"),
            cpu="{} vCore".format(model.get("vcore", "?")),
            ram_mb=model.get("memory"),
            disk="{} GB".format(model.get("disk", "?")),
            ipv4=" ".join(i for i in ips if ":" not in i),
            ipv6=" ".join(i for i in ips if ":" in i),
            expiration=svc.get("expiration"),
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, names))


def fetch_cloud(cli, workers):
    projects = cli.get_safe("/cloud/project", []) or []
    rows = []
    for pid in projects:
        proj = cli.get_safe("/cloud/project/" + pid, {}) or {}
        label = proj.get("description") or pid
        instances = cli.get_safe("/cloud/project/" + pid + "/instance", []) or []
        print("  Public Cloud [{}] : {} instance(s)".format(label, len(instances)),
              file=sys.stderr)
        for inst in instances:
            flavor = inst.get("flavor") or {}
            image = inst.get("image") or {}
            addrs = inst.get("ipAddresses") or []
            rows.append(row(
                type="cloud-instance",
                id=inst.get("id"),
                name=inst.get("name"),
                state=inst.get("status"),
                offer=flavor.get("name"),
                datacenter=inst.get("region"),
                os=image.get("name"),
                cpu=flavor.get("vcpus"),
                ram_mb=flavor.get("ram"),
                disk="{} GB".format(flavor.get("disk", "?")),
                ipv4=" ".join(a["ip"] for a in addrs if a.get("version") == 4),
                ipv6=" ".join(a["ip"] for a in addrs if a.get("version") == 6),
                project=label,
            ))
    return rows


def fetch_private_cloud(cli, workers):
    services = cli.get_safe("/dedicatedCloud", []) or []
    rows = []
    for svc in services:
        for dc_id in cli.get_safe("/dedicatedCloud/{}/datacenter".format(svc), []) or []:
            prefix = "/dedicatedCloud/{}/datacenter/{}".format(svc, dc_id)
            dc = cli.get_safe(prefix, {}) or {}
            hosts = cli.get_safe(prefix + "/host", []) or []
            print("  Hosted Private Cloud [{}/{}] : {} host(s)".format(
                svc, dc.get("name", dc_id), len(hosts)), file=sys.stderr)
            for h_id in hosts:
                h = cli.get_safe("{}/host/{}".format(prefix, h_id), {}) or {}
                rows.append(row(
                    type="private-cloud-host",
                    id="{}/{}/{}".format(svc, dc_id, h_id),
                    name=h.get("name"),
                    state=h.get("state"),
                    offer=h.get("profile"),
                    datacenter=dc.get("name") or str(dc_id),
                    project=svc,
                ))
    return rows


FETCHERS = {
    "dedicated": fetch_dedicated,
    "vps": fetch_vps,
    "cloud": fetch_cloud,
    "private-cloud": fetch_private_cloud,
}


def render_table(rows, columns):
    if not rows:
        return "(aucune machine)"
    # un "|" dans une valeur (ex: offre "RISE-S | Ryzen 7") casse la lecture
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
        description="Inventaire des machines OVHcloud (dedies, VPS, Public Cloud, HPC).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--endpoint", default=os.environ.get("OVH_ENDPOINT", "ovh-eu"),
                    choices=sorted(ENDPOINTS), help="endpoint API (defaut: ovh-eu)")
    ap.add_argument("--config", help="chemin d'un .ovh.conf (defaut: ~/.ovh.conf)")
    ap.add_argument("--kinds", default="dedicated,vps",
                    help="types a inventorier, ou 'all' : "
                         + ",".join(FETCHERS) + " (defaut: dedicated,vps)")
    ap.add_argument("--format", default="table", choices=["table", "csv", "json"],
                    help="format de sortie (defaut: table)")
    ap.add_argument("--out", help="fichier de sortie (defaut: stdout)")
    ap.add_argument("--columns", help="colonnes a afficher, separees par des virgules")
    ap.add_argument("--ca-bundle", default=os.environ.get("REQUESTS_CA_BUNDLE"),
                    help="CA racine a utiliser (proxy d'entreprise avec inspection "
                         "SSL legitime) ; defaut: REQUESTS_CA_BUNDLE")
    ap.add_argument("--workers", type=int, default=8,
                    help="appels API en parallele (defaut: 8)")
    args = ap.parse_args()

    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    if "all" in kinds:
        kinds = list(FETCHERS)
    inconnus = [k for k in kinds if k not in FETCHERS]
    if inconnus:
        ap.error("type(s) inconnu(s) : " + ", ".join(inconnus))

    columns = [c.strip() for c in args.columns.split(",")] if args.columns else COLUMNS
    inconnues = [c for c in columns if c not in COLUMNS]
    if inconnues:
        ap.error("colonne(s) inconnue(s) : " + ", ".join(inconnues))

    try:
        creds = load_credentials(args)
        cli = OvhClient(ca_bundle=args.ca_bundle, **creds)
        me = cli.get_safe("/me", {}) or {}
        if me:
            print("Compte : {} ({}) sur {}".format(
                me.get("nichandle"), me.get("email"), creds["endpoint"]),
                file=sys.stderr)
        else:
            print("Compte : inconnu (pas de droit GET /me) sur "
                  + creds["endpoint"], file=sys.stderr)

        rows = []
        for kind in kinds:
            print("Inventaire " + kind + "...", file=sys.stderr)
            rows.extend(FETCHERS[kind](cli, args.workers))
    except OvhError as exc:
        print("Erreur : " + str(exc), file=sys.stderr)
        return 1

    rows.sort(key=lambda r: (r["type"], str(r.get("name") or r.get("id"))))

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

    counts = {}
    for r in rows:
        counts[r["type"]] = counts.get(r["type"], 0) + 1
    resume = ", ".join("{}: {}".format(k, v) for k, v in sorted(counts.items())) or "rien"
    print("\nTotal : {} machine(s) ({})".format(len(rows), resume), file=sys.stderr)
    if args.out:
        print("Ecrit dans " + args.out, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
