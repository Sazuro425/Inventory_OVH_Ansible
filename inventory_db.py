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
import json
import os
from pathlib import Path

import secrets_infra

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
        depuis_systemd = secrets_infra.lire_secret(env.lower())
        if depuis_systemd:
            return depuis_systemd
        val = os.environ.get(env, d.get(key, defaut))
        return secrets_infra.resoudre(val)

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
            "droits insuffisants pour {} : le compte de collecte a besoin de "
            "SELECT, INSERT, UPDATE, DELETE sur la base ; le compte web, des "
            "seuls droits poses par sql/setup_db_web.sql — a rejouer apres "
            "chaque mise a jour du schema.".format(quoi))
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


# ---------------------------------------------------------------------------
# Volet commercial : les ventes, et la rentabilite qui en decoule.
#
# Ces fonctions ecrivent, contrairement a tout le reste du module qui ne fait
# que deposer des instantanes. C'est la seule donnee de la base qu'un humain
# saisit, donc la seule ou la validation compte vraiment : un prix negatif ou
# un contrat vide fausserait le total du parc sans rien declencher.
# ---------------------------------------------------------------------------

PERIODES = ("mois", "an")
DEVISES = ("EUR", "USD", "GBP", "CAD", "CHF")


class VenteInvalide(ValueError):
    """Saisie refusee. Le message est destine a l'utilisateur."""


class SaisieInterdite(VenteInvalide):
    """Saisie bien formee, mais pas permise a cet utilisateur (403, pas 422)."""


def _date_saisie(valeur, quoi):
    """Une date saisie : vide, ou lisible.

    _date_or_none() avale une date illisible, ce qui convient a l'API OVH.
    Pour une saisie, ce serait faire d'une fin de contrat mal tapee un
    contrat sans fin — donc facture pour toujours dans le calcul.
    """
    if valeur in (None, ""):
        return None
    d = _date_or_none(valeur)
    if d is None:
        raise VenteInvalide(
            "date de {} illisible : AAAA-MM-JJ attendu".format(quoi))
    return d


def _texte(v, quoi, maxi, obligatoire=False):
    """Un champ texte saisi : nettoye, borne, sans caractere de controle.

    Un retour a la ligne dans un nom de site finit dans les journaux, ou il
    permet de fabriquer une fausse ligne ; il n'a rien a faire la.
    """
    t = "" if v is None else str(v).strip()
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in t):
        raise VenteInvalide("{} contient un caractere de controle".format(quoi))
    if obligatoire and not t:
        raise VenteInvalide("{} est obligatoire".format(quoi))
    if len(t) > maxi:
        raise VenteInvalide("{} trop long ({} caracteres au plus)".format(
            quoi, maxi))
    return t or None


def _prix(v, quoi):
    if v in (None, ""):
        return 0.0
    try:
        prix = float(str(v).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        raise VenteInvalide("{} doit etre un nombre".format(quoi)) from None
    if prix != prix or prix < 0:          # NaN, ou negatif
        raise VenteInvalide("{} ne peut pas etre negatif".format(quoi))
    if prix > 99999999:
        raise VenteInvalide("{} depasse ce que la colonne accepte".format(quoi))
    return round(prix, 2)


def _js(ligne):
    if ligne is None:
        return None
    return json.dumps(
        {k: v.isoformat() if isinstance(v, (dt.date, dt.datetime))
         else _json_ok(v) for k, v in ligne.items()},
        ensure_ascii=False, sort_keys=True)


def journaliser(cur, par, action, cible, avant=None, apres=None):
    """Trace une saisie, dans la transaction de la saisie elle-meme.

    Meme curseur, meme commit : une saisie sans trace, ou une trace sans
    saisie, sont impossibles.
    """
    cur.execute(
        "INSERT INTO journal_saisie (le, par, action, cible, avant, apres) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (dt.datetime.now(), par, action, cible[:300], _js(avant), _js(apres)))


def verifier_vente(v):
    """Normalise et controle une vente. Renvoie le dict propre a ecrire."""
    nom = _texte(v.get("vm_name"), "le nom de la VM", 190, obligatoire=True)
    contrat = _texte(v.get("contrat"), "le numero de contrat", 100,
                     obligatoire=True)
    if v.get("prix_vente") in (None, ""):
        raise VenteInvalide("le prix de vente est obligatoire")
    prix = _prix(v.get("prix_vente"), "le prix de vente")

    periode = (v.get("periode") or "mois").strip().lower()
    if periode not in PERIODES:
        raise VenteInvalide("periode inconnue : attendu " + " ou ".join(PERIODES))

    devise = (v.get("devise") or "EUR").strip().upper()
    if devise not in DEVISES:
        raise VenteInvalide("devise inconnue : " + devise)

    debut = _date_saisie(v.get("debut"), "debut")
    fin = _date_saisie(v.get("fin"), "fin")
    if debut and fin and fin < debut:
        raise VenteInvalide("la date de fin precede la date de debut")

    return {
        "vm_name": nom,
        "cluster_name": _texte(v.get("cluster_name"), "le cluster", 190),
        "contrat": contrat,
        "client": _texte(v.get("client"), "le client", 190),
        "prix_vente": prix,
        "devise": devise,
        "periode": periode,
        "debut": debut,
        "fin": fin,
    }


def enregistrer_vente(conn, vente, par):
    """Cree la vente, ou met a jour celle qui porte deja ce numero de contrat.

    Le numero de contrat est la clef : resaisir le meme contrat corrige la
    ligne au lieu d'en ajouter une seconde. C'est ce qu'un commercial attend
    quand il se rend compte qu'il s'est trompe de prix.
    """
    v = verifier_vente(vente)
    maintenant = dt.datetime.now()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM commercial WHERE contrat = %s "
                        "FOR UPDATE", (v["contrat"],))
            avant = cur.fetchone()
            cur.execute(
                "INSERT INTO commercial (vm_name, cluster_name, contrat, "
                "client, prix_vente, devise, periode, debut, fin, actif, "
                "cree_le, cree_par) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s) "
                "ON DUPLICATE KEY UPDATE "
                "  vm_name = VALUES(vm_name), "
                "  cluster_name = VALUES(cluster_name), "
                "  client = VALUES(client), "
                "  prix_vente = VALUES(prix_vente), "
                "  devise = VALUES(devise), "
                "  periode = VALUES(periode), "
                "  debut = VALUES(debut), "
                "  fin = VALUES(fin), "
                "  actif = 1, "
                "  maj_le = %s, maj_par = %s",
                (v["vm_name"], v["cluster_name"], v["contrat"], v["client"],
                 v["prix_vente"], v["devise"], v["periode"], v["debut"],
                 v["fin"], maintenant, par, maintenant, par))
            # rowcount : 1 = insertion, 2 = mise a jour (convention MySQL)
            cree = cur.rowcount == 1
            journaliser(cur, par, "vente", "contrat " + v["contrat"],
                        avant, v)
    except pymysql.Error as exc:
        conn.rollback()
        if (exc.args[0] if exc.args else 0) == 1146:
            raise DbError(
                "la table commercial n'existe pas : rejoue sql/schema.sql"
            ) from exc
        raise _traduit(exc, "l'enregistrement de la vente") from exc
    conn.commit()
    return dict(v, cree=cree)


def desactiver_vente(conn, contrat, par):
    """Resilie : la ligne reste, actif passe a 0. Jamais de DELETE."""
    contrat = (contrat or "").strip()
    if not contrat:
        raise VenteInvalide("numero de contrat absent")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE commercial SET actif = 0, maj_le = %s, maj_par = %s "
                "WHERE contrat = %s AND actif = 1",
                (dt.datetime.now(), par, contrat))
            touche = cur.rowcount
            if touche:
                journaliser(cur, par, "resiliation", "contrat " + contrat,
                            {"actif": 1}, {"actif": 0})
    except pymysql.Error as exc:
        conn.rollback()
        raise _traduit(exc, "la resiliation") from exc
    conn.commit()
    return touche > 0


def lister_ventes(conn, vm=None, actives=True):
    sql = "SELECT * FROM commercial"
    where, args = [], []
    if vm:
        where.append("vm_name = %s")
        args.append(vm)
    if actives:
        where.append("actif = 1")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY vm_name, contrat"
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return [{k: _json_ok(x) for k, x in r.items()} for r in cur.fetchall()]
    except pymysql.Error as exc:
        if (exc.args[0] if exc.args else 0) == 1146:
            return []
        raise _traduit(exc, "la lecture des ventes") from exc


def lire_saisies(conn):
    """Ce que les humains ont saisi : ventes actives, elements, corrections.

    Une table absente (schema pas encore rejoue) rend une liste vide plutot
    qu'une erreur : le calcul reste possible, simplement sans ces saisies.
    """
    requetes = {
        "ventes": "SELECT * FROM v_vente_active",
        "elements": "SELECT * FROM element WHERE actif = 1",
        "surcharges": "SELECT * FROM surcharge WHERE valeur IS NOT NULL",
    }
    out = {}
    with conn.cursor() as cur:
        for cle, sql in requetes.items():
            try:
                cur.execute(sql)
                out[cle] = [{k: _json_ok(v) for k, v in r.items()}
                            for r in cur.fetchall()]
            except pymysql.Error as exc:
                if (exc.args[0] if exc.args else 0) not in (1146, 1356):
                    raise _traduit(exc, "la lecture des saisies") from exc
                out[cle] = []
    return out


def rentabilite(conn):
    """L'arbre de rentabilite du dernier snapshot, saisies comprises.

    Le calcul est dans rentabilite.py, et nulle part ailleurs : ce module ne
    fait que lui passer les lignes.
    """
    import rentabilite as calcul
    data = load_latest(conn)
    if data is None:
        raise DbError("aucun snapshot en base : lance une collecte")
    saisies = lire_saisies(conn)
    res = calcul.calculer(data, saisies["ventes"], saisies["elements"],
                          saisies["surcharges"])
    res["snapshot"] = data["run_id"]
    res["genere_le"] = data["generated_at"]
    return res


# ---------------------------------------------------------------------------
# Elements declares et corrections
# ---------------------------------------------------------------------------

PARENTS = ("cluster", "serveur", "vm", "element")
CIBLES = ("cluster", "serveur", "vm")
CHAMPS = ("prix_achat", "prix_vente", "cluster")
CHAMPS_ELEMENT = ("parent_type", "parent_ref", "nom", "genre", "contrat",
                  "client", "prix_vente", "prix_achat", "devise", "periode",
                  "note")


def verifier_element(e):
    """Normalise et controle un element declare. Renvoie le dict a ecrire."""
    parent_type = (e.get("parent_type") or "").strip()
    if parent_type not in PARENTS:
        raise VenteInvalide("type de parent inconnu : attendu "
                            + ", ".join(PARENTS))
    periode = (e.get("periode") or "mois").strip().lower()
    if periode not in PERIODES:
        raise VenteInvalide("periode inconnue : attendu " + " ou ".join(PERIODES))
    devise = (e.get("devise") or "EUR").strip().upper()
    if devise not in DEVISES:
        raise VenteInvalide("devise inconnue : " + devise)
    ident = e.get("id")
    if ident in (None, ""):
        ident = None
    else:
        try:
            ident = int(ident)
        except (TypeError, ValueError):
            raise VenteInvalide("identifiant d'element illisible") from None
    propre = {
        "id": ident,
        "parent_type": parent_type,
        "parent_ref": _texte(e.get("parent_ref"), "le parent", 190,
                             obligatoire=True),
        "nom": _texte(e.get("nom"), "le nom", 190, obligatoire=True),
        "genre": _texte(e.get("genre"), "le genre", 40),
        "contrat": _texte(e.get("contrat"), "le contrat", 100),
        "client": _texte(e.get("client"), "le client", 190),
        "prix_vente": _prix(e.get("prix_vente"), "le prix de vente"),
        "prix_achat": _prix(e.get("prix_achat"), "le prix d'achat"),
        "devise": devise,
        "periode": periode,
        "note": _texte(e.get("note"), "la note", 500),
    }
    if parent_type == "element":
        if not propre["parent_ref"].isdigit():
            raise VenteInvalide("un element parent se designe par son numero")
        if ident is not None and str(ident) == propre["parent_ref"]:
            raise VenteInvalide("un element ne peut pas etre son propre parent")
    return propre


def enregistrer_element(conn, element, par):
    """Cree un element, ou corrige celui dont l'id est donne."""
    e = verifier_element(element)
    maintenant = dt.datetime.now()
    valeurs = [e[c] for c in CHAMPS_ELEMENT]
    try:
        with conn.cursor() as cur:
            if e["id"] is None:
                cur.execute(
                    "INSERT INTO element ({}, actif, cree_le, cree_par) "
                    "VALUES ({}, 1, %s, %s)".format(
                        ", ".join(CHAMPS_ELEMENT),
                        ", ".join(["%s"] * len(CHAMPS_ELEMENT))),
                    valeurs + [maintenant, par])
                e["id"] = cur.lastrowid
                avant, cree = None, True
            else:
                cur.execute("SELECT * FROM element WHERE id = %s AND actif = 1 "
                            "FOR UPDATE", (e["id"],))
                avant = cur.fetchone()
                if not avant:
                    raise VenteInvalide("aucun element actif sous ce numero")
                cur.execute(
                    "UPDATE element SET {}, maj_le = %s, maj_par = %s "
                    "WHERE id = %s".format(
                        ", ".join(c + " = %s" for c in CHAMPS_ELEMENT)),
                    valeurs + [maintenant, par, e["id"]])
                cree = False
            journaliser(cur, par, "element", "element {}".format(e["id"]),
                        avant, e)
    except VenteInvalide:
        conn.rollback()
        raise
    except pymysql.Error as exc:
        conn.rollback()
        raise _traduit(exc, "l'enregistrement de l'element") from exc
    conn.commit()
    return dict(e, cree=cree)


def retirer_element(conn, ident, par):
    """Desactive un element. Ses enfants deviennent orphelins : ils restent
    comptes, et le calcul les signale, plutot que de disparaitre en silence."""
    try:
        ident = int(ident)
    except (TypeError, ValueError):
        raise VenteInvalide("identifiant d'element illisible") from None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE element SET actif = 0, maj_le = %s, maj_par = %s "
                "WHERE id = %s AND actif = 1", (dt.datetime.now(), par, ident))
            touche = cur.rowcount
            if touche:
                journaliser(cur, par, "retrait", "element {}".format(ident),
                            {"actif": 1}, {"actif": 0})
    except pymysql.Error as exc:
        conn.rollback()
        raise _traduit(exc, "le retrait de l'element") from exc
    conn.commit()
    return touche > 0


def verifier_surcharge(s):
    cible_type = (s.get("cible_type") or "").strip()
    champ = (s.get("champ") or "").strip()
    if cible_type not in CIBLES:
        raise VenteInvalide("cible inconnue : attendu " + ", ".join(CIBLES))
    if champ not in CHAMPS:
        raise VenteInvalide("champ inconnu : attendu " + ", ".join(CHAMPS))
    if champ == "cluster" and cible_type != "serveur":
        raise VenteInvalide("seul un serveur change de cluster")
    valeur = s.get("valeur")
    if valeur in (None, ""):
        valeur = None                       # leve la correction
    elif champ == "cluster":
        valeur = _texte(valeur, "le nom du cluster", 190)
        if valeur.startswith(("seul:", "standalone:", "inconnu:")):
            raise VenteInvalide("ce prefixe est reserve au calcul")
    else:
        valeur = "{:.2f}".format(_prix(valeur, "la valeur"))
    return {
        "cible_type": cible_type,
        "cible_ref": _texte(s.get("cible_ref"), "la cible", 190,
                            obligatoire=True),
        "champ": champ,
        "valeur": valeur,
        # une correction sans motif est une correction que personne ne saura
        # justifier dans six mois
        "motif": _texte(s.get("motif"), "le motif", 300, obligatoire=True),
    }


def poser_surcharge(conn, surcharge, par):
    """Pose ou leve une correction. Lever, c'est poser une valeur nulle."""
    s = verifier_surcharge(surcharge)
    cible = "{} {} {}".format(s["cible_type"], s["cible_ref"], s["champ"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT valeur, motif FROM surcharge WHERE cible_type = %s "
                "AND cible_ref = %s AND champ = %s FOR UPDATE",
                (s["cible_type"], s["cible_ref"], s["champ"]))
            avant = cur.fetchone()
            cur.execute(
                "INSERT INTO surcharge (cible_type, cible_ref, champ, valeur, "
                "motif, maj_le, maj_par) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE valeur = VALUES(valeur), "
                "motif = VALUES(motif), maj_le = VALUES(maj_le), "
                "maj_par = VALUES(maj_par)",
                (s["cible_type"], s["cible_ref"], s["champ"], s["valeur"],
                 s["motif"], dt.datetime.now(), par))
            journaliser(cur, par,
                        "correction" if s["valeur"] is not None else "levee",
                        cible, avant, {"valeur": s["valeur"],
                                       "motif": s["motif"]})
    except pymysql.Error as exc:
        conn.rollback()
        raise _traduit(exc, "la correction") from exc
    conn.commit()
    return s


# ---------------------------------------------------------------------------
# Vues de rentabilite enregistrees
# ---------------------------------------------------------------------------

MODES_VUE = ("arbre", "tableau", "graphiques")
NIVEAUX_VUE = ("cluster", "serveur", "vm", "element", "tout")
LISTES_VUE = ("clusters", "datacenters", "clients", "genres")


def verifier_vue(v):
    """Controle une vue enregistree. Renvoie (id, nom, partagee, definition).

    La definition est relue et rejouee par la page : seuls les champs connus,
    avec leur type, y entrent. Un champ inattendu est refuse plutot
    qu'ignore, pour qu'une faute de frappe dans un client d'API se voie.
    """
    nom = _texte(v.get("nom"), "le nom de la vue", 80, obligatoire=True)
    ident = v.get("id")
    if ident in (None, ""):
        ident = None
    else:
        try:
            ident = int(ident)
        except (TypeError, ValueError):
            raise VenteInvalide("identifiant de vue illisible") from None
    d = v.get("definition")
    if not isinstance(d, dict):
        raise VenteInvalide("la definition d'une vue est un objet")
    connus = {"mode", "niveau", "texte", "perte", "alertes", "tri", "sens",
              *LISTES_VUE}
    inconnus = sorted(set(d) - connus)
    if inconnus:
        raise VenteInvalide("champ de vue inconnu : " + ", ".join(inconnus))
    propre = {}
    if d.get("mode") is not None:
        if d["mode"] not in MODES_VUE:
            raise VenteInvalide("mode inconnu : attendu " + ", ".join(MODES_VUE))
        propre["mode"] = d["mode"]
    if d.get("niveau") is not None:
        if d["niveau"] not in NIVEAUX_VUE:
            raise VenteInvalide("niveau inconnu")
        propre["niveau"] = d["niveau"]
    texte = _texte(d.get("texte"), "le texte du filtre", 190)
    if texte:
        propre["texte"] = texte
    for k in ("perte", "alertes"):
        if d.get(k) not in (None, True, False):
            raise VenteInvalide("{} attend vrai ou faux".format(k))
        if d.get(k):
            propre[k] = True
    if d.get("tri") is not None:
        tri = str(d["tri"])
        if len(tri) > 40 or not tri.replace("_", "").isalnum():
            raise VenteInvalide("colonne de tri illisible")
        propre["tri"] = tri
        propre["sens"] = -1 if d.get("sens") == -1 else 1
    for k in LISTES_VUE:
        lst = d.get(k)
        if lst in (None, []):
            continue
        if not isinstance(lst, list) or len(lst) > 300:
            raise VenteInvalide("{} attend une liste (300 au plus)".format(k))
        propre[k] = sorted({_texte(x, k, 190, obligatoire=True) for x in lst})
    texte_json = json.dumps(propre, ensure_ascii=False, sort_keys=True)
    if len(texte_json) > 16000:
        raise VenteInvalide("definition de vue trop volumineuse")
    return ident, nom, bool(v.get("partagee", True)), propre


def _vue_lisible(r):
    try:
        definition = json.loads(r["definition"] or "{}")
    except ValueError:
        definition = {}
    return {"id": r["id"], "nom": r["nom"], "partagee": bool(r["partagee"]),
            "definition": definition, "cree_par": r["cree_par"],
            "maj_le": r["maj_le"].isoformat() if r.get("maj_le") else None}


def lister_vues(conn, utilisateur):
    """Les vues partagees, et celles de l'utilisateur."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM vue_rentabilite WHERE actif = 1 "
                        "AND (partagee = 1 OR cree_par = %s) ORDER BY nom",
                        (utilisateur,))
            return [_vue_lisible(r) for r in cur.fetchall()]
    except pymysql.Error as exc:
        if (exc.args[0] if exc.args else 0) == 1146:
            return []
        raise _traduit(exc, "la lecture des vues") from exc


def _vue_modifiable(cur, ident, par, admin):
    cur.execute("SELECT * FROM vue_rentabilite WHERE id = %s AND actif = 1 "
                "FOR UPDATE", (ident,))
    avant = cur.fetchone()
    if not avant:
        raise VenteInvalide("aucune vue active sous ce numero")
    if avant["cree_par"] != par and not admin:
        raise SaisieInterdite("seul l'auteur de la vue ({}) ou un admin la "
                            "modifie".format(avant["cree_par"]))
    return avant


def enregistrer_vue(conn, vue, par, admin=False):
    ident, nom, partagee, definition = verifier_vue(vue)
    texte = json.dumps(definition, ensure_ascii=False, sort_keys=True)
    maintenant = dt.datetime.now()
    try:
        with conn.cursor() as cur:
            if ident is None:
                cur.execute(
                    "INSERT INTO vue_rentabilite (nom, definition, partagee, "
                    "actif, cree_le, cree_par) VALUES (%s, %s, %s, 1, %s, %s)",
                    (nom, texte, int(partagee), maintenant, par))
                ident, avant = cur.lastrowid, None
            else:
                avant = _vue_modifiable(cur, ident, par, admin)
                cur.execute(
                    "UPDATE vue_rentabilite SET nom = %s, definition = %s, "
                    "partagee = %s, maj_le = %s, maj_par = %s WHERE id = %s",
                    (nom, texte, int(partagee), maintenant, par, ident))
            journaliser(cur, par, "vue", "vue {}".format(ident),
                        avant and {"nom": avant["nom"],
                                   "definition": avant["definition"]},
                        {"nom": nom, "definition": texte})
    except VenteInvalide:
        conn.rollback()
        raise
    except pymysql.Error as exc:
        conn.rollback()
        raise _traduit(exc, "l'enregistrement de la vue") from exc
    conn.commit()
    return {"id": ident, "nom": nom, "partagee": partagee,
            "definition": definition, "cree_par": avant["cree_par"]
            if avant else par, "cree": avant is None}


def retirer_vue(conn, ident, par, admin=False):
    try:
        ident = int(ident)
    except (TypeError, ValueError):
        raise VenteInvalide("identifiant de vue illisible") from None
    try:
        with conn.cursor() as cur:
            _vue_modifiable(cur, ident, par, admin)
            cur.execute("UPDATE vue_rentabilite SET actif = 0, maj_le = %s, "
                        "maj_par = %s WHERE id = %s",
                        (dt.datetime.now(), par, ident))
            journaliser(cur, par, "retrait", "vue {}".format(ident),
                        {"actif": 1}, {"actif": 0})
    except VenteInvalide:
        conn.rollback()
        raise
    except pymysql.Error as exc:
        conn.rollback()
        raise _traduit(exc, "le retrait de la vue") from exc
    conn.commit()
    return True


def lire_journal(conn, limite=200):
    """Les dernieres saisies, la plus recente d'abord."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM journal_saisie ORDER BY id DESC "
                        "LIMIT %s", (max(1, min(int(limite), 1000)),))
            return [{k: (v.isoformat() if isinstance(v, dt.datetime)
                         else _json_ok(v)) for k, v in r.items()}
                    for r in cur.fetchall()]
    except pymysql.Error as exc:
        if (exc.args[0] if exc.args else 0) == 1146:
            return []
        raise _traduit(exc, "la lecture du journal") from exc
