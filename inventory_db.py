#!/usr/bin/env python3
"""
Couche MariaDB de l'inventaire : ecrit un snapshot complet par collecte,
relit le dernier snapshot exploitable.

Le reste de la chaine ne connait que trois fonctions : connect(), store_run()
et load_latest(). Rien d'autre ne touche au SQL.

Connexion : section "db" de infra.yml, ou variables d'environnement
INVENTORY_DB_HOST / _PORT / _USER / _PASSWORD / _NAME.
"""

from __future__ import annotations

import datetime as dt
import decimal
import os
from pathlib import Path

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:  # message utile plutot qu'un ImportError brut
    pymysql = None
    DictCursor = None


class DbError(RuntimeError):
    pass


def db_settings(conf=None):
    d = dict((conf or {}).get("db") or {})

    def pick(key, env, defaut=None):
        val = os.environ.get(env, d.get(key, defaut))
        if isinstance(val, str) and val.startswith("env:"):
            val = os.environ.get(val[4:])
        return val

    return {
        "host": pick("host", "INVENTORY_DB_HOST", "127.0.0.1"),
        "port": int(pick("port", "INVENTORY_DB_PORT", 3306) or 3306),
        "user": pick("user", "INVENTORY_DB_USER", "inventory"),
        "password": pick("password", "INVENTORY_DB_PASSWORD", "") or "",
        "database": pick("name", "INVENTORY_DB_NAME", "inventory"),
    }


def _ouvre(s, database=None):
    return pymysql.connect(
        host=s["host"], port=s["port"], user=s["user"], password=s["password"],
        database=database, charset="utf8mb4", cursorclass=DictCursor,
        autocommit=False)


def connect(conf=None, auto_schema=None):
    """Connexion a la base. Si auto_schema, cree base et tables au besoin.

    L'auto-creation demande des droits DDL au compte applicatif (voir
    sql/setup_db.sql). Sans ces droits, le message dit quoi accorder.
    """
    if pymysql is None:
        raise DbError(
            "PyMySQL n'est pas installe.\n"
            "  sudo apt install python3-pymysql   (Debian/Ubuntu)\n"
            "  pip install PyMySQL")
    s = db_settings(conf)
    if auto_schema is None:
        auto_schema = bool((conf or {}).get("db", {}).get("auto_schema", True))
    try:
        return _ouvre(s, s["database"])
    except pymysql.Error as exc:
        inconnue = exc.args and exc.args[0] == 1049  # Unknown database
        if not (inconnue and auto_schema):
            raise DbError("connexion a {}@{}:{}/{} impossible : {}".format(
                s["user"], s["host"], s["port"], s["database"], exc)) from exc
    except Exception as exc:  # erreurs socket, DNS...
        raise DbError("connexion a {}@{}:{}/{} impossible : {}".format(
            s["user"], s["host"], s["port"], s["database"], exc)) from exc

    # la base n'existe pas : on la cree, puis on s'y connecte
    try:
        sans_base = _ouvre(s, None)
    except Exception as exc:
        raise DbError("connexion au serveur {}:{} impossible : {}".format(
            s["host"], s["port"], exc)) from exc
    try:
        with sans_base.cursor() as cur:
            cur.execute("CREATE DATABASE IF NOT EXISTS `{}` CHARACTER SET "
                        "utf8mb4 COLLATE utf8mb4_unicode_ci".format(s["database"]))
        sans_base.commit()
    except pymysql.Error as exc:
        sans_base.close()
        raise DbError(
            "la base {} n'existe pas et le compte {} ne peut pas la creer "
            "({}).\nAccorde-lui les droits, ou cree-la a part :\n"
            "    python setup_inventory_db.py --db-password '<mdp>'".format(
                s["database"], s["user"], exc)) from exc
    sans_base.close()
    return _ouvre(s, s["database"])


def chemin_schema():
    """sql/schema.sql, cherche a cote de ce fichier puis dans le repertoire."""
    for candidat in (Path(__file__).resolve().parent / "sql" / "schema.sql",
                     Path("sql/schema.sql"), Path("schema.sql")):
        if candidat.is_file():
            return candidat
    return None


def ensure_schema(conn, force=False):
    """Applique sql/schema.sql : creation, et migration des colonnes ajoutees.

    Le fichier est entierement idempotent (CREATE ... IF NOT EXISTS,
    ADD COLUMN IF NOT EXISTS, CREATE OR REPLACE VIEW), donc le rejouer met a
    jour une base existante. force=True sert quand une colonne manque.
    """
    if not force:
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES LIKE 'collect_run'")
            if cur.fetchone():
                return False

    chemin = chemin_schema()
    if chemin is None:
        raise DbError("tables absentes et sql/schema.sql introuvable")

    brut = chemin.read_text(encoding="utf-8")
    sans_commentaires = "\n".join(
        l for l in brut.splitlines() if not l.strip().startswith("--"))
    instructions = [b.strip() for b in sans_commentaires.split(";") if b.strip()]
    # le USE du fichier vise 'inventory' : la connexion est deja sur la bonne base
    instructions = [i for i in instructions if not i.upper().startswith("USE ")]

    try:
        with conn.cursor() as cur:
            for sql in instructions:
                cur.execute(sql)
        conn.commit()
    except pymysql.Error as exc:
        conn.rollback()
        code = exc.args[0] if exc.args else 0
        if code in (1142, 1143, 1044):  # droits DDL manquants (CREATE/ALTER)
            raise DbError(
                "le compte applicatif n'a pas le droit de creer les tables "
                "({}).\nSoit tu lui accordes les droits DDL sur sa base :\n"
                "    GRANT CREATE, CREATE VIEW, DROP, INDEX, REFERENCES, ALTER\n"
                "      ON inventory.* TO 'inventory'@'localhost';\n"
                "soit tu appliques le schema avec un compte admin :\n"
                "    python setup_inventory_db.py --db-password '<mdp>'".format(exc)
            ) from exc
        raise DbError("creation du schema impossible : {}".format(exc)) from exc
    return True


def _json_ok(value):
    """MariaDB renvoie les DECIMAL en Decimal, que json.dumps refuse."""
    if isinstance(value, decimal.Decimal):
        return float(value)
    return value


def _date_or_none(value):
    """L'API OVH renvoie des dates ISO ; la colonne attend un DATE ou NULL."""
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _num_or_none(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _traduit(exc, quoi):
    """Transforme une erreur SQL en message exploitable."""
    code = exc.args[0] if exc.args else 0
    if code == 1146:  # table inconnue
        return DbError(
            "les tables n'existent pas encore.\n"
            "Applique le schema :  mariadb -u inventory -p inventory "
            "< sql/schema.sql\n"
            "ou lance : python setup_inventory_db.py --db-password '<mdp>'")
    if code in (1044, 1142):  # droits insuffisants
        return DbError(
            "droits insuffisants pour {} : le compte applicatif a besoin de "
            "SELECT, INSERT, UPDATE, DELETE sur la base.".format(quoi))
    return DbError("{} impossible : {}".format(quoi, exc))


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
    except pymysql.Error as exc:
        raise _traduit(exc, "la lecture du snapshot") from exc

    return {
        "servers": servers, "probes": probes, "guests": guests,
        "generated_at": int(run["started_at"].timestamp()),
        "run_id": run["id"], "run_status": run["status"],
    }


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
