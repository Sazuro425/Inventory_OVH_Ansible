"""
Socle de la base de l'inventaire : connexion, schema et migrations, erreurs,
controle des saisies, journal.

Les autres modules db_* s'appuient sur lui ; le reste du projet passe par
inventory_db, qui les reexporte tous.
"""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import json
import os
import re
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

    instructions = instructions_sql(chemin.read_text(encoding="utf-8"))

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
    appliquer_migrations(conn, dire=lambda *a: None)
    return True


def instructions_sql(texte):
    """Un fichier SQL en ordres a executer un par un.

    Decoupage au point-virgule, lignes de commentaire retirees : suffisant
    pour nos fichiers, qui n'ont ni procedure stockee ni point-virgule dans
    une chaine. Le USE est ignore, la connexion est deja sur la bonne base.
    """
    sans_commentaires = "\n".join(
        l for l in texte.splitlines() if not l.strip().startswith("--"))
    instructions = [b.strip() for b in sans_commentaires.split(";") if b.strip()]
    return [i for i in instructions if not i.upper().startswith("USE ")]


# ---------------------------------------------------------------------------
# Migrations : sql/migrations/NNNN_nom.sql, appliquees une fois, dans l'ordre
# ---------------------------------------------------------------------------

DOSSIER_MIGRATIONS = Path(__file__).resolve().parent / "sql" / "migrations"
_NOM_MIGRATION = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


def migrations_disponibles(dossier=None):
    """[(version, nom, chemin)], triees. La version 1 est schema.sql."""
    dossier = Path(dossier or DOSSIER_MIGRATIONS)
    out, vues = [], {}
    if not dossier.is_dir():
        return out
    for f in sorted(dossier.iterdir()):
        if f.suffix != ".sql":
            continue
        m = _NOM_MIGRATION.match(f.name)
        if not m:
            raise DbError("migration mal nommee : {} (attendu NNNN_nom.sql)"
                          .format(f.name))
        v = int(m.group(1))
        if v < 2:
            raise DbError("{} : la version 1 est schema.sql, les migrations "
                          "commencent a 0002".format(f.name))
        if v in vues:
            raise DbError("deux migrations en version {} : {} et {}".format(
                v, vues[v], f.name))
        vues[v] = f.name
        out.append((v, m.group(2), f))
    return out


def version_attendue(dossier=None):
    lst = migrations_disponibles(dossier)
    return lst[-1][0] if lst else 1


def version_schema(conn):
    """La version de la base, 0 si la table des versions n'existe pas."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version")
            return int(cur.fetchone()["v"])
    except pymysql.Error as exc:
        if (exc.args[0] if exc.args else 0) == 1146:
            return 0
        raise _traduit(exc, "la lecture de la version du schema") from exc


def appliquer_migrations(conn, dossier=None, dire=print):
    """Applique les migrations au-dela de la version de la base. Rend la
    liste des versions appliquees.

    Le DDL de MariaDB n'est pas transactionnel : une migration interrompue a
    mi-chemin laisse ses premiers ordres en place. D'ou la regle : une
    migration s'ecrit rejouable (IF NOT EXISTS, IF EXISTS), et elle n'est
    inscrite qu'une fois tous ses ordres passes.
    """
    actuelle = version_schema(conn)
    if actuelle == 0:
        raise DbError("table schema_version absente : rejoue sql/schema.sql "
                      "d'abord")
    faites = []
    for v, nom, chemin in migrations_disponibles(dossier):
        if v <= actuelle:
            continue
        dire("migration {:04d} {}".format(v, nom))
        ordres = instructions_sql(chemin.read_text(encoding="utf-8"))
        try:
            with conn.cursor() as cur:
                for sql in ordres:
                    cur.execute(sql)
                cur.execute("INSERT INTO schema_version (version, nom, "
                            "applique_le, applique_par) VALUES (%s, %s, %s, "
                            "CURRENT_USER())", (v, nom, dt.datetime.now()))
            conn.commit()
        except pymysql.Error as exc:
            conn.rollback()
            raise _traduit(exc, "la migration {}".format(chemin.name)) from exc
        faites.append(v)
    return faites


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
            "droits insuffisants pour {} : les droits par table des deux "
            "comptes sont dans sql/droits.sql, a rejouer en root apres "
            "chaque mise a jour du schema.".format(quoi))
    return DbError("{} impossible : {}".format(quoi, exc))



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


ORIGINE_CHAINE = "0" * 64


def empreinte_journal(n, precedente, le, par, action, cible, avant, apres):
    """L'empreinte d'une ligne du journal : son contenu et celle d'avant."""
    contenu = json.dumps([n, precedente, le.isoformat(sep=" "), par, action,
                          cible, avant, apres],
                         ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(contenu.encode("utf-8")).hexdigest()


def journaliser(cur, par, action, cible, avant=None, apres=None):
    """Trace une saisie, dans la transaction de la saisie elle-meme.

    Meme curseur, meme commit : une saisie sans trace, ou une trace sans
    saisie, sont impossibles. La ligne s'accroche a la chaine (migration
    0003) : la tete est verrouillee le temps de l'ecriture, si bien que deux
    saisies simultanees se suivent.
    """
    # ce que la base gardera, a la seconde et a la longueur des colonnes :
    # l'empreinte doit porter sur ce qui sera relu
    le = dt.datetime.now().replace(microsecond=0)
    ligne = (str(par)[:190], str(action)[:40], str(cible)[:300],
             _js(avant), _js(apres))
    cur.execute("SELECT n, empreinte FROM journal_tete WHERE id = 1 FOR UPDATE")
    tete = cur.fetchone()
    if tete is None:
        raise DbError("journal_tete vide : rejoue sql/schema.sql")
    n = tete["n"] + 1
    emp = empreinte_journal(n, tete["empreinte"], le, *ligne)
    cur.execute(
        "INSERT INTO journal_saisie (n, precedente, empreinte, le, par, action,"
        " cible, avant, apres) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (n, tete["empreinte"], emp, le) + ligne)
    cur.execute("UPDATE journal_tete SET n = %s, empreinte = %s WHERE id = 1",
                (n, emp))


def tete_journal(conn):
    """(n, empreinte) du dernier maillon : l'ancre que la collecte recopie
    hors de la base, pour qu'une chaine reecrite en entier se voie aussi."""
    with conn.cursor() as cur:
        cur.execute("SELECT n, empreinte FROM journal_tete WHERE id = 1")
        r = cur.fetchone()
    return (r["n"], r["empreinte"]) if r else (0, ORIGINE_CHAINE)


def verifier_journal(conn):
    """Parcourt la chaine. Rend {ok, chainees, hors_chaine, rupture, tete}.

    rupture : None, ou {n, motif} au premier maillon qui ne tient pas —
    ligne modifiee, effacee, inseree, ou tete qui ne colle pas a la fin.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM journal_saisie WHERE n IS NULL")
        hors = cur.fetchone()["c"]
        cur.execute("SELECT n, precedente, empreinte, le, par, action, cible, "
                    "avant, apres FROM journal_saisie WHERE n IS NOT NULL "
                    "ORDER BY n")
        lignes = cur.fetchall()
        cur.execute("SELECT n, empreinte FROM journal_tete WHERE id = 1")
        tete = cur.fetchone()

    def rompu(n, motif):
        return {"ok": False, "chainees": len(lignes), "hors_chaine": hors,
                "rupture": {"n": n, "motif": motif}, "tete": tete and tete["n"]}

    attendu_n, attendu_prec = 1, ORIGINE_CHAINE
    for r in lignes:
        if r["n"] != attendu_n:
            return rompu(attendu_n, "ligne {} absente".format(attendu_n))
        if r["precedente"] != attendu_prec:
            return rompu(r["n"], "maillon decroche de la ligne precedente")
        recalcul = empreinte_journal(r["n"], r["precedente"], r["le"], r["par"],
                                     r["action"], r["cible"], r["avant"],
                                     r["apres"])
        if recalcul != r["empreinte"]:
            return rompu(r["n"], "contenu modifie")
        attendu_n, attendu_prec = r["n"] + 1, r["empreinte"]
    if tete is None or tete["n"] != attendu_n - 1 or tete["empreinte"] != attendu_prec:
        return rompu(attendu_n - 1, "la tete ne correspond pas a la derniere ligne")
    return {"ok": True, "chainees": len(lignes), "hors_chaine": hors,
            "rupture": None, "tete": tete["n"]}


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
