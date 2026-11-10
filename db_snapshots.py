"""
Les instantanes du parc : la collecte en ecrit un par passage, tout le reste
lit le dernier. Et le menage : vieux runs, jetons clients perimes.
"""

from __future__ import annotations

import datetime as dt

from db_base import (
    _date_or_none,
    _json_ok,
    _num_or_none,
    _traduit,
    ensure_schema,
    pymysql,
)


def store_run(conn, data, status=None, message=None, _deja_tente=False):
    """Ecrit la photo complete du parc et renvoie l'identifiant du run."""
    servers = data.get("servers") or []
    probes = data.get("probes") or []
    guests = data.get("guests") or []
    injoignables = [p for p in probes if p.get("error")]
    if status is None:
        status = "partial" if injoignables else "ok"
    started = dt.datetime.fromtimestamp(data.get("generated_at") or 0)

    try:
      with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO collect_run (started_at, finished_at, status, "
            "nb_servers, nb_nodes, nb_guests, nb_unreachable, message) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (started, dt.datetime.now(), status, len(servers), len(probes),
             len(guests), len(injoignables), (message or None)))
        run_id = cur.lastrowid

        if servers:
            cur.executemany(
                "INSERT INTO server_snapshot (run_id, ovh_name, reverse_name, "
                "ip, offer, datacenter, os, state, expiration, cpu_model, "
                "cpu_sockets, cpu_cores, cpu_threads, ram_mb, disk_count, "
                "disk_desc) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s, %s)",
                [(run_id, s.get("ovh_name"), s.get("reverse"), s.get("ip") or None,
                  s.get("offer") or None, s.get("datacenter") or None,
                  s.get("os") or None, s.get("state") or None,
                  _date_or_none(s.get("expiration")),
                  s.get("cpu_model") or None, s.get("cpu_sockets"),
                  s.get("cpu_cores"), s.get("cpu_threads"), s.get("ram_mb"),
                  s.get("disk_count"), s.get("disk_desc") or None)
                 for s in servers])

        if probes:
            cur.executemany(
                "INSERT INTO node_snapshot (run_id, host, pve_node, "
                "cluster_name, reachable, error, cpu_model, cpu_sockets, "
                "cpu_cores, cpu_logical, cpu_mhz, ram_total_mb, ram_used_mb, "
                "disk_count, disk_total_gb, disk_desc, pve_version, kernel) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s)",
                [(run_id, p.get("host"), p.get("node"), p.get("cluster"),
                  0 if p.get("error") else 1, (p.get("error") or None))
                 + tuple((p.get("hw") or {}).get(k) or None for k in (
                     "cpu_model", "cpu_sockets", "cpu_cores", "cpu_logical",
                     "cpu_mhz", "ram_total_mb", "ram_used_mb", "disk_count",
                     "disk_total_gb", "disk_desc", "pve_version", "kernel"))
                 for p in probes])

        if guests:
            cur.executemany(
                "INSERT INTO guest_snapshot (run_id, cluster_name, pve_node, "
                "via_host, vmid, guest_type, name, status, cpus, ram_mb, "
                "disk_gb, uptime, os, ipv4, tags) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [(run_id, g.get("cluster"), g.get("node"), g.get("via_host"),
                  g.get("vmid"), g.get("type"), g.get("name") or None,
                  g.get("status") or None, _num_or_none(g.get("cpus")),
                  _num_or_none(g.get("ram_mb")), _num_or_none(g.get("disk_gb")),
                  g.get("uptime") or None, g.get("os") or None,
                  g.get("ipv4") or None, g.get("tags") or None) for g in guests])

        couts = data.get("costs") or []
        if couts:
            cur.executemany(
                "INSERT INTO server_cost (run_id, ovh_name, service_id, "
                "price_value, price_currency, price_period, price_month, "
                "next_billing, erreur) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [(run_id, c.get("ovh_name"), c.get("service_id"),
                  _num_or_none(c.get("price_value")),
                  c.get("price_currency") or None,
                  c.get("price_period") or None,
                  _num_or_none(c.get("price_month")),
                  _date_or_none(c.get("next_billing")),
                  c.get("erreur") or None) for c in couts])
    except pymysql.Error as exc:
        conn.rollback()
        code = exc.args[0] if exc.args else 0
        # 1146 : tables absentes. 1054 : colonne inconnue, donc schema plus
        # ancien que le code. Dans les deux cas on applique le schema et on
        # rejoue une seule fois.
        if code in (1146, 1054) and not _deja_tente:
            ensure_schema(conn, force=(code == 1054))
            return store_run(conn, data, status, message, _deja_tente=True)
        raise _traduit(exc, "l'ecriture du snapshot") from exc
    conn.commit()
    return run_id


def load_latest(conn):
    """Relit le dernier snapshot exploitable, au format attendu par la chaine."""
    try:
      with conn.cursor() as cur:
        cur.execute("SELECT * FROM v_last_run")
        run = cur.fetchone()
        if not run:
            return None

        cur.execute("SELECT * FROM server_snapshot WHERE run_id = %s", (run["id"],))
        servers = [{
            "ovh_name": r["ovh_name"], "reverse": r["reverse_name"] or "",
            "ip": r["ip"] or "", "offer": r["offer"] or "",
            "datacenter": r["datacenter"] or "", "os": r["os"] or "",
            "state": r["state"] or "",
            "expiration": r["expiration"].isoformat() if r["expiration"] else "",
            "cpu_model": r.get("cpu_model") or "",
            "cpu_sockets": r.get("cpu_sockets"), "cpu_cores": r.get("cpu_cores"),
            "cpu_threads": r.get("cpu_threads"), "ram_mb": r.get("ram_mb"),
            "disk_count": r.get("disk_count"),
            "disk_desc": r.get("disk_desc") or "",
        } for r in cur.fetchall()]

        cur.execute("SELECT * FROM node_snapshot WHERE run_id = %s", (run["id"],))
        probes = [{
            "host": r["host"], "node": r["pve_node"], "cluster": r["cluster_name"],
            "members": [], "error": r["error"], "server": {},
            "hw": {k: _json_ok(r.get(k)) for k in (
                "cpu_model", "cpu_sockets", "cpu_cores", "cpu_logical",
                "cpu_mhz", "ram_total_mb", "ram_used_mb", "disk_count",
                "disk_total_gb", "disk_desc", "pve_version", "kernel")},
        } for r in cur.fetchall()]

        cur.execute("SELECT * FROM guest_snapshot WHERE run_id = %s", (run["id"],))
        guests = [{
            "node": r["pve_node"], "type": r["guest_type"], "vmid": r["vmid"],
            "name": r["name"] or "", "status": r["status"] or "",
            "cpus": r["cpus"] if r["cpus"] is not None else "",
            "ram_mb": r["ram_mb"] if r["ram_mb"] is not None else "",
            "disk_gb": float(r["disk_gb"]) if r["disk_gb"] is not None else "",
            "uptime": r["uptime"] or "", "os": r["os"] or "",
            "ipv4": r["ipv4"] or "", "tags": r["tags"] or "",
            "cluster": r["cluster_name"], "via_host": r["via_host"],
        } for r in cur.fetchall()]

        # server_cost est arrivee apres les autres : une base qui n'a pas
        # encore rejoue le schema doit rendre l'inventaire quand meme.
        try:
            cur.execute("SELECT * FROM server_cost WHERE run_id = %s",
                        (run["id"],))
            couts = [{
                "ovh_name": r["ovh_name"], "service_id": r["service_id"],
                "price_value": float(r["price_value"])
                    if r["price_value"] is not None else None,
                "price_currency": r["price_currency"] or "",
                "price_period": r["price_period"] or "",
                "price_month": float(r["price_month"])
                    if r["price_month"] is not None else None,
                "next_billing": r["next_billing"].isoformat()
                    if r["next_billing"] else "",
                "erreur": r["erreur"] or "",
            } for r in cur.fetchall()]
        except pymysql.Error as exc:
            if (exc.args[0] if exc.args else 0) != 1146:
                raise
            couts = []
    except pymysql.Error as exc:
        raise _traduit(exc, "la lecture du snapshot") from exc

    return {
        "servers": servers, "probes": probes, "guests": guests,
        "costs": couts,
        "generated_at": int(run["started_at"].timestamp()),
        "run_id": run["id"], "run_status": run["status"],
    }


def purger_jetons_clients(conn, delai=dt.timedelta(days=1)):
    """Efface les jetons Ansible perimes depuis plus d'un jour.

    Fait par la collecte : le service web, qui les emet, n'a pas DELETE.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM client_token WHERE expire_le < %s",
                    (dt.datetime.now() - delai,))
        n = cur.rowcount
    conn.commit()
    return n


def purge(conn, keep):
    """Ne garde que les N derniers runs (les snapshots suivent en cascade)."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM collect_run ORDER BY id DESC LIMIT %s", (keep,))
        gardes = [r["id"] for r in cur.fetchall()]
        if not gardes:
            return 0
        trous = ",".join(["%s"] * len(gardes))
        cur.execute("DELETE FROM collect_run WHERE id NOT IN (" + trous + ")", gardes)
        supprimes = cur.rowcount
    conn.commit()
    return supprimes

