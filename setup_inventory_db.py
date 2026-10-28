#!/usr/bin/env python3
"""
Prepare la base d'inventaire sur MariaDB : base, compte applicatif, tables.

Idempotent : relancable sans rien casser (CREATE ... IF NOT EXISTS,
CREATE OR REPLACE VIEW). Ne supprime jamais de donnees.

Le mot de passe root n'est jamais ecrit sur disque : saisi au clavier, ou lu
dans MARIADB_ROOT_PASSWORD. Le mot de passe applicatif est genere si tu n'en
fournis pas.

    python setup_inventory_db.py                   # cree tout, mot de passe genere
    python setup_inventory_db.py --check           # n'ecrit rien, verifie l'etat
    python setup_inventory_db.py --grant-host '172.28.%.%'   # acces depuis WSL

Sur la VM Linux cible, la meme commande fonctionne telle quelle.
"""

from __future__ import annotations

import argparse
import getpass
import glob
import os
import secrets
import shutil
import string
import subprocess
import sys
from pathlib import Path

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:
    print("PyMySQL manquant.\n"
          "  Debian/Ubuntu : sudo apt install python3-pymysql\n"
          "  sinon         : pip install PyMySQL", file=sys.stderr)
    raise SystemExit(2)

ALPHABET = string.ascii_letters + string.digits + "-_."


def log(msg):
    print(msg, file=sys.stderr)


def genere_mot_de_passe(n=28):
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def mot_de_passe_root(args):
    if args.root_password:
        return args.root_password
    depuis_env = os.environ.get("MARIADB_ROOT_PASSWORD")
    if depuis_env is not None:
        return depuis_env
    if not sys.stdin.isatty():
        raise SystemExit(
            "pas de terminal pour la saisie : definis MARIADB_ROOT_PASSWORD "
            "ou passe --root-password")
    return getpass.getpass("Mot de passe {} : ".format(args.root_user))


def trouve_client():
    """Client mariadb/mysql en ligne de commande, PATH ou emplacements usuels."""
    for nom in ("mariadb", "mysql"):
        chemin = shutil.which(nom)
        if chemin:
            return chemin
    motifs = [
        r"C:\Program Files\MariaDB*\bin\mariadb.exe",
        r"C:\Program Files\MySQL\MySQL Server*\bin\mysql.exe",
        "/usr/bin/mariadb", "/usr/bin/mysql",
    ]
    for motif in motifs:
        for trouve in sorted(glob.glob(motif), reverse=True):
            if Path(trouve).is_file():
                return trouve
    return None


def sql_via_client(client, args, sql):
    """Execute du SQL avec le client officiel.

    Necessaire quand le compte administrateur utilise un plugin d'authen-
    tification que PyMySQL ne gere pas : sur MariaDB Windows, root passe
    souvent par l'authentification Windows (auth_gssapi).
    """
    cmd = [client, "-u", args.root_user, "-h", args.host,
           "-P", str(args.port), "--batch"]
    if args.root_password:
        cmd.append("--password=" + args.root_password)
    try:
        r = subprocess.run(cmd, input=sql.encode("utf-8"),
                           capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit("echec de l'appel au client : " + str(exc))
    if r.returncode != 0:
        raise SystemExit("client en erreur ({}) : {}".format(
            r.returncode, r.stderr.decode("utf-8", "replace").strip()[:400]))
    return r.stdout.decode("utf-8", "replace")


def connecte(host, port, user, password, database=None):
    return pymysql.connect(
        host=host, port=port, user=user, password=password, database=database,
        charset="utf8mb4", cursorclass=DictCursor, autocommit=True)


def instructions_sql(chemin):
    """Decoupe un fichier .sql en instructions (pas de procedure stockee ici)."""
    brut = Path(chemin).read_text(encoding="utf-8")
    sans_commentaires = "\n".join(
        l for l in brut.splitlines() if not l.strip().startswith("--"))
    for bloc in sans_commentaires.split(";"):
        bloc = bloc.strip()
        # le USE du fichier viserait toujours "inventory" : c'est l'appelant
        # qui choisit la base (--db-name)
        if bloc and not bloc.upper().startswith("USE "):
            yield bloc


def etat(cur, db_name, db_user):
    cur.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
                "WHERE SCHEMA_NAME = %s", (db_name,))
    base = bool(cur.fetchone())
    cur.execute("SELECT user, host FROM mysql.user WHERE user = %s", (db_user,))
    comptes = [r["host"] for r in cur.fetchall()]
    tables, vues = [], []
    if base:
        cur.execute("SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME", (db_name,))
        for r in cur.fetchall():
            (vues if r["TABLE_TYPE"] == "VIEW" else tables).append(r["TABLE_NAME"])
    return {"base": base, "comptes": comptes, "tables": tables, "vues": vues}


def affiche_etat(e, db_name, db_user):
    log("  base {:<12} : {}".format(db_name, "presente" if e["base"] else "absente"))
    log("  compte {:<10} : {}".format(
        db_user, ", ".join(e["comptes"]) if e["comptes"] else "inexistant"))
    log("  tables          : {}".format(
        ", ".join(e["tables"]) if e["tables"] else "aucune"))
    log("  vues            : {}".format(
        ", ".join(e["vues"]) if e["vues"] else "aucune"))


def echappe(valeur):
    """Litteral SQL simple : le SQL part dans le client, pas en parametre lie."""
    return "'" + str(valeur).replace("\\", "\\\\").replace("'", "''") + "'"


def setup_par_client(args, grant_hosts):
    """Chemin pour un compte admin que PyMySQL ne peut pas authentifier."""
    client = trouve_client() if args.client == "auto" else args.client
    if not client:
        log("Erreur : aucun client mariadb/mysql trouve ; indique son chemin "
            "avec --client C:\\chemin\\mariadb.exe")
        return 1
    log("client : " + client)

    sortie = sql_via_client(client, args, (
        "SELECT COUNT(*) AS n FROM mysql.user WHERE user = {};"
        .format(echappe(args.db_user))))
    deja = sortie.strip().splitlines()[-1].strip() not in ("0", "n", "")

    if args.check:
        sortie = sql_via_client(client, args, (
            "SELECT COUNT(*) FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = {};".format(echappe(args.db_name))))
        nb = sortie.strip().splitlines()[-1].strip()
        log("  compte {} : {}".format(
            args.db_user, "present" if deja else "inexistant"))
        log("  objets dans {} : {}".format(args.db_name, nb))
        complet = deja and nb.isdigit() and int(nb) >= 4
        log("\n=> " + ("tout est en place" if complet else "setup incomplet"))
        return 0 if complet else 1

    if deja and not args.db_password:
        log("\nLe compte {} existe deja. Relance avec --db-password pour "
            "reutiliser son mot de passe,\nou pour le remplacer.".format(
                args.db_user))
        return 1

    mdp = args.db_password or genere_mot_de_passe()
    morceaux = [
        "CREATE DATABASE IF NOT EXISTS `{}` CHARACTER SET utf8mb4 "
        "COLLATE utf8mb4_unicode_ci;".format(args.db_name)]
    for origine in grant_hosts:
        cible = "{}@{}".format(echappe(args.db_user), echappe(origine))
        # plugin explicite : le defaut du serveur peut etre gssapi, que les
        # clients Python ne negocient pas
        auth = "IDENTIFIED VIA mysql_native_password USING PASSWORD({})".format(
            echappe(mdp))
        morceaux.append("CREATE USER IF NOT EXISTS {} {};".format(cible, auth))
        morceaux.append("ALTER USER {} {};".format(cible, auth))
        morceaux.append(
            "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, CREATE VIEW, DROP, "
            "INDEX, REFERENCES, ALTER ON `{}`.* TO {};".format(
                args.db_name, cible))
    morceaux.append("FLUSH PRIVILEGES;")

    if not args.no_schema:
        chemin = Path(args.schema)
        if not chemin.is_file():
            log("Erreur : schema introuvable : " + str(chemin.resolve()))
            return 1
        morceaux.append("USE `{}`;".format(args.db_name))
        morceaux.extend(s + ";" for s in instructions_sql(chemin))

    sql_via_client(client, args, "\n".join(morceaux))
    log("base, compte et schema appliques")

    # verification avec le compte applicatif, cette fois via PyMySQL
    try:
        app = connecte(args.host, args.port, args.db_user, mdp, args.db_name)
        with app.cursor() as cur:
            apres = etat(cur, args.db_name, args.db_user)
        app.close()
    except pymysql.Error as exc:
        log("Erreur : le compte applicatif ne se connecte pas ({})".format(exc))
        return 1
    log("\nEtat apres :")
    affiche_etat(apres, args.db_name, args.db_user)

    if not args.db_password:
        log("\nMot de passe du compte applicatif (note-le, il n'est pas stocke) :")
        print(mdp)
        log("\n  export INVENTORY_DB_PASSWORD='<ci-dessus>'        # bash")
        log("  $env:INVENTORY_DB_PASSWORD='<ci-dessus>'          # PowerShell")
    log("\nEnsuite : python infra_inventory.py --refresh --store-db")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Prepare la base d'inventaire sur MariaDB.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--host", default=os.environ.get("INVENTORY_DB_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("INVENTORY_DB_PORT", 3306)))
    ap.add_argument("--root-user", default="root")
    ap.add_argument("--root-password",
                    help="a eviter en ligne de commande ; sinon saisie au clavier "
                         "ou MARIADB_ROOT_PASSWORD")
    ap.add_argument("--db-name", default="inventory")
    ap.add_argument("--db-user", default="inventory")
    ap.add_argument("--db-password",
                    help="mot de passe du compte applicatif (genere si absent)")
    ap.add_argument("--grant-host", action="append", default=None,
                    help="origine autorisee pour le compte (repetable ; "
                         "defaut: localhost)")
    ap.add_argument("--schema", default="sql/schema.sql")
    ap.add_argument("--no-schema", action="store_true",
                    help="cree seulement la base et le compte")
    ap.add_argument("--check", action="store_true",
                    help="n'ecrit rien : affiche l'etat et sort")
    ap.add_argument("--client", nargs="?", const="auto", default=None,
                    metavar="CHEMIN",
                    help="passe par le client mariadb/mysql au lieu de PyMySQL, "
                         "pour un compte admin en authentification Windows "
                         "(sans valeur : recherche automatique)")
    args = ap.parse_args()

    grant_hosts = args.grant_host or ["localhost"]

    if args.client:
        return setup_par_client(args, grant_hosts)

    # --- etat courant -----------------------------------------------------
    try:
        root = connecte(args.host, args.port, args.root_user,
                        mot_de_passe_root(args))
    except pymysql.Error as exc:
        conseil = ""
        if "gssapi" in str(exc).lower() or "2059" in str(exc):
            client = trouve_client()
            conseil = (
                "\nLe compte {} utilise l'authentification Windows, que PyMySQL "
                "ne gere pas.\nRelance avec --client".format(args.root_user)
                + (" (detecte : {})".format(client) if client else ""))
        log("Erreur : connexion root impossible sur {}:{} ({}){}".format(
            args.host, args.port, exc, conseil))
        return 1

    with root.cursor() as cur:
        avant = etat(cur, args.db_name, args.db_user)
    log("Etat avant :")
    affiche_etat(avant, args.db_name, args.db_user)

    if args.check:
        root.close()
        complet = avant["base"] and avant["comptes"] and len(avant["tables"]) >= 4
        log("\n=> " + ("tout est en place" if complet else "setup incomplet"))
        return 0 if complet else 1

    # --- base et compte ---------------------------------------------------
    mdp = args.db_password or genere_mot_de_passe()
    genere = args.db_password is None

    with root.cursor() as cur:
        cur.execute(
            "CREATE DATABASE IF NOT EXISTS `{}` CHARACTER SET utf8mb4 "
            "COLLATE utf8mb4_unicode_ci".format(args.db_name))
        log("\nbase {} : prete".format(args.db_name))

        for origine in grant_hosts:
            existe = origine in avant["comptes"]
            if existe and genere:
                # ne pas changer le mot de passe d'un compte deja utilise
                # ailleurs sans que ce soit demande explicitement
                log("compte {}@{} : deja present, mot de passe inchange".format(
                    args.db_user, origine))
            else:
                auth = ("IDENTIFIED VIA mysql_native_password "
                        "USING PASSWORD(%s)")
                cur.execute(
                    "CREATE USER IF NOT EXISTS %s@%s " + auth,
                    (args.db_user, origine, mdp))
                if existe:
                    cur.execute("ALTER USER %s@%s " + auth,
                                (args.db_user, origine, mdp))
                log("compte {}@{} : {}".format(
                    args.db_user, origine,
                    "mot de passe mis a jour" if existe else "cree"))
            cur.execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, CREATE VIEW, DROP, "
                "INDEX, REFERENCES, ALTER ON `{}`.* TO %s@%s".format(
                    args.db_name), (args.db_user, origine))
        cur.execute("FLUSH PRIVILEGES")

    deja_la = any(o in avant["comptes"] for o in grant_hosts)
    if deja_la and genere:
        root.close()
        log("\nLe compte existait deja et son mot de passe n'a pas ete touche.")
        log("Relance avec --db-password '<le mot de passe actuel>' pour appliquer")
        log("le schema, ou avec un nouveau mot de passe pour le remplacer.")
        return 1

    # --- schema, applique avec le compte applicatif ------------------------
    if not args.no_schema:
        chemin = Path(args.schema)
        if not chemin.is_file():
            log("\nErreur : schema introuvable : " + str(chemin.resolve()))
            root.close()
            return 1
        try:
            app = connecte(args.host, args.port, args.db_user, mdp, args.db_name)
        except pymysql.Error as exc:
            log("\nErreur : le compte applicatif ne peut pas se connecter ({})".format(exc))
            root.close()
            return 1
        # le compte applicatif n'a pas CREATE : c'est root qui pose les tables
        app.close()
        with root.cursor() as cur:
            cur.execute("USE `{}`".format(args.db_name))
            for i, sql in enumerate(instructions_sql(chemin), 1):
                try:
                    cur.execute(sql)
                except pymysql.Error as exc:
                    log("\nErreur sur l'instruction {} : {}".format(i, exc))
                    log(sql[:200])
                    root.close()
                    return 1
        log("schema applique depuis {}".format(chemin))

    with root.cursor() as cur:
        apres = etat(cur, args.db_name, args.db_user)
    root.close()
    log("\nEtat apres :")
    affiche_etat(apres, args.db_name, args.db_user)

    if genere:
        log("\nMot de passe du compte applicatif (note-le, il n'est pas stocke) :")
        print(mdp)
        log("\nA mettre dans l'environnement avant de lancer l'inventaire :")
        log("  export INVENTORY_DB_PASSWORD='<ci-dessus>'        # bash")
        log("  $env:INVENTORY_DB_PASSWORD='<ci-dessus>'          # PowerShell")
    log("\nEnsuite : python infra_inventory.py --refresh --store-db")
    return 0


if __name__ == "__main__":
    sys.exit(main())
