#!/usr/bin/env python3
"""
Tests d'integration contre un vrai MariaDB.

    INVENTAIRE_TEST_MARIADB=detruire python3 tests_mariadb.py

**Detruit et recree la base `inventory` et les comptes `inventory` et
`inventory_web` du serveur local.** A lancer sur un MariaDB jetable (WSL,
conteneur, CI), jamais sur la VM de production : la variable ci-dessus est
la pour qu'on ne le fasse pas par megarde.

Les autres tests remplacent la base par des fonctions en memoire ; ceux-ci
deroulent ce que fait le deploiement — setup_db.sql, schema.sql (deux fois,
il doit se rejouer), setup_db_web.sql — puis exercent chaque fonction sous le
compte qui l'utilise en production. Un GRANT oublie, une colonne mal nommee,
un ordre SQL que MariaDB refuse : c'est ici que ca se voit.

Le client `mariadb` doit pouvoir se connecter en root sans mot de passe
(socket unix, cas d'une installation Debian), pour rejouer les scripts SQL.
"""

from __future__ import annotations

import datetime as dt
import os
import secrets
import subprocess
import sys
import traceback
from pathlib import Path

RACINE = Path(__file__).resolve().parent
sys.path.insert(0, str(RACINE))

import auth_locale as al  # noqa: E402
import depot_scelle as ds  # noqa: E402
import generer_demo  # noqa: E402
import infra_inventory as orch  # noqa: E402
import inventory_db as db  # noqa: E402
import secret_store  # noqa: E402
import serveur_web as web  # noqa: E402

CAS = []


def test(libelle):
    def decorateur(fn):
        CAS.append((libelle, fn))
        return fn
    return decorateur


def egal(obtenu, attendu, quoi=""):
    if obtenu != attendu:
        raise AssertionError("{} : attendu {!r}, obtenu {!r}".format(
            quoi or "valeur", attendu, obtenu))


def refuse_sql(fn, code=1142):
    """fn doit echouer sur un refus de droit MariaDB (1142 par defaut)."""
    try:
        fn()
    except Exception as exc:
        # l'erreur pymysql, ou la DbError qui l'enveloppe
        codes = [getattr(e, "args", (None,))[0] for e in (exc, exc.__cause__)
                 if e is not None]
        if code in codes or str(code) in str(exc):
            return
        raise AssertionError("refus {} attendu, obtenu : {!r}".format(
            code, exc)) from exc
    raise AssertionError("aurait du etre refuse ({})".format(code))


# --------------------------------------------------------------------------
# Mise en place : ce que fait le deploiement, dans le meme ordre
# --------------------------------------------------------------------------

MDP_COLLECTE = secrets.token_hex(16)
MDP_WEB = secrets.token_hex(16)


def sql_root(texte, base=None):
    cmd = ["mariadb", "--batch"] + ([base] if base else [])
    r = subprocess.run(cmd, input=texte, text=True, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("mariadb a refuse : " + r.stderr.strip())
    return r.stdout


def script(nom):
    texte = (RACINE / "sql" / nom).read_text(encoding="utf-8")
    return (texte.replace("MOT_DE_PASSE_WEB_A_DEFINIR", MDP_WEB)
                 .replace("MOT_DE_PASSE_A_DEFINIR", MDP_COLLECTE))


def conf(utilisateur, mdp):
    return {"db": {"host": "127.0.0.1", "port": 3306, "user": utilisateur,
                   "password": mdp, "name": "inventory", "auto_schema": False},
            "pve": {}, "web": {}}


CONF_COLLECTE = conf("inventory", MDP_COLLECTE)
CONF_WEB = conf("inventory_web", MDP_WEB)


def collecte():
    return db.connect(CONF_COLLECTE)


def service():
    return db.connect(CONF_WEB)


def mettre_en_place():
    sql_root("DROP DATABASE IF EXISTS inventory;"
             "DROP USER IF EXISTS 'inventory'@'localhost';"
             "DROP USER IF EXISTS 'inventory_web'@'localhost';")
    sql_root(script("setup_db.sql"))
    sql_root(script("schema.sql"), "inventory")
    sql_root(script("schema.sql"), "inventory")      # rejouable
    c = collecte()
    try:
        db.appliquer_migrations(c, dire=lambda *a: None)
    finally:
        c.close()
    sql_root(script("setup_db_web.sql"))
    sql_root(script("droits.sql"))
    sql_root(script("droits.sql"))               # rejouable


# --------------------------------------------------------------------------
# Le parc de demonstration, ecrit par la collecte
# --------------------------------------------------------------------------

DATA = generer_demo.generer(12, 2, 6, 7, 0.45, 0.05, 0.10, 0.06)
DATA["costs"] = [{"ovh_name": s["ovh_name"], "price_month": 90.0,
                  "price_currency": "EUR"} for s in DATA["servers"]]
UNE_VM = DATA["guests"][0]["name"]
UNE_REF = orch.ref_vm(DATA["guests"][0])
UN_SERVEUR = DATA["servers"][0]


@test("schema rejouable, et la separation des comptes tient")
def _():
    droits = sql_root(
        "SELECT table_name, privilege_type FROM information_schema.table_privileges"
        " WHERE grantee LIKE \"'inventory_web'%\"")
    lignes = [l.split("\t") for l in droits.strip().splitlines()[1:]]
    egal([t for t, _ in lignes if t == "secret_store"], [], "secret_store")
    egal(sorted({p for _, p in lignes} - {"SELECT", "INSERT", "UPDATE"}), [],
         "droits du compte web au-dela de SELECT/INSERT/UPDATE")
    schema = sql_root("SELECT privilege_type FROM information_schema."
                      "schema_privileges WHERE grantee LIKE \"'inventory_web'%\"")
    egal(schema.strip().splitlines()[1:], [], "droit au niveau de la base")


@test("collecte : un snapshot s'ecrit et se relit")
def _():
    c = collecte()
    try:
        run = db.store_run(c, DATA)
        lu = db.load_latest(c)
    finally:
        c.close()
    egal(lu["run_id"], run)
    egal(len(lu["servers"]), len(DATA["servers"]), "serveurs")
    egal(len(lu["guests"]), len(DATA["guests"]), "invites")


@test("historique : la collecte inscrit le jour, une fois ; le service le lit")
def _():
    import capacite
    c = collecte()
    try:
        n = db.historiser(c, db.rentabilite(c))
        egal(db.historiser(c, db.rentabilite(c)), n, "second passage du jour")
        hier = dt.date.today() - dt.timedelta(days=1)
        db.historiser(c, db.rentabilite(c), jour=hier)
    finally:
        c.close()
    w = service()
    try:
        lignes = db.lire_historique(w, dt.date.today() - dt.timedelta(days=7))
        with w.cursor() as cur:
            refuse_sql(lambda: cur.execute(
                "INSERT INTO historique (jour, niveau, cle, nom, maj_le) "
                "VALUES (CURDATE(), 'parc', 'x', 'x', NOW())"))
    finally:
        w.close()
    egal(len(lignes), 2 * n, "deux jours, un point par carte et par jour")
    egal(n, len({(l["niveau"], l["cle"]) for l in lignes}), "une ligne par carte")
    serie = capacite.series_mensuelles(lignes, "parc")
    egal(len(serie), 1)
    egal(serie[0]["points"][-1]["nb_vm"], float(len(DATA["guests"])))


@test("service web : la rentabilite se calcule avec ses seuls droits")
def _():
    c = service()
    try:
        r = db.rentabilite(c)
    finally:
        c.close()
    egal(r["globale"]["nb_serveurs"], len(DATA["servers"]))
    egal(len(r["roles"]) >= 10, True, "catalogue de roles pre-rempli")


@test("service web : ventes, enfants, lot, corrections, roles, vues")
def _():
    c = service()
    try:
        # comme la page : la VM par sa reference cluster/vmid
        db.enregistrer_vente(c, {"vm_name": UNE_VM, "vm_ref": UNE_REF,
                                 "contrat": "C-001", "prix_vente": "56",
                                 "periode": "mois"}, "evan")
        db.poser_role(c, {"cible_type": "vm", "cible_ref": UNE_REF,
                          "role": "lamp"}, "evan")
        lot = db.enregistrer_elements(c, {
            "parent_type": "vm", "parent_ref": UNE_REF, "role": "site",
            "noms": ["a.fr", "b.fr", "c.fr", "d.fr"], "prix_vente": "40",
            "periode": "mois"}, "evan")
        e = db.enregistrer_element(c, {"parent_type": "element",
                                       "parent_ref": str(lot[0]["id"]),
                                       "nom": "boite@a.fr", "prix_vente": "5"},
                                   "evan")
        db.enregistrer_element(c, dict(e, prix_vente="6"), "evan")
        db.poser_surcharge(c, {"cible_type": "serveur",
                               "cible_ref": UN_SERVEUR["ovh_name"],
                               "champ": "prix_achat", "valeur": "120",
                               "motif": "tarif negocie"}, "evan")
        nouveau = db.creer_role(c, {"libelle": "Serveur Node.js"}, "evan")
        db.retirer_element(c, lot[3]["id"], "evan")
        tableau_de_bord = {
            "roles": ["lamp"], "mode": "graphiques",
            "colonnes": {"serveur": ["nom", "ram_pct", "vcpu_ratio"]},
            "panneaux": [{"type": "figure", "figure": "ram", "largeur": 2},
                         {"type": "barres", "niveau": "vm", "mesure": "ram",
                          "groupe": "cluster", "top": 12, "largeur": 1}]}
        vue = db.enregistrer_vue(c, {"nom": "LAMP", "definition": tableau_de_bord},
                                 "evan")
        vues = db.lister_vues(c, "evan")
        r = db.rentabilite(c)
        journal = db.lire_journal(c)
    finally:
        c.close()
    egal(nouveau["code"], "serveur-node-js")
    egal([(v["nom"], v["definition"]) for v in vues if v["id"] == vue["id"]],
         [("LAMP", tableau_de_bord)], "le tableau de bord revient tel quel")

    def trouve(cartes):
        for x in cartes:
            if x["type"] == "vm" and x["cle"] == UNE_REF:
                return x
            t = trouve(x["enfants"])
            if t:
                return t
    vm = trouve(r["arbre"])
    egal(vm["infos"]["role"], "lamp")
    egal(vm["pv"], 56.0 + 3 * 40 + 6, "contrat + 3 sites actifs + la boite")
    egal(len(journal) >= 9, True, "chaque saisie journalisee")


@test("service web : le journal ne s'efface ni ne se corrige")
def _():
    c = service()
    try:
        with c.cursor() as cur:
            refuse_sql(lambda: cur.execute("DELETE FROM journal_saisie"))
            refuse_sql(lambda: cur.execute("UPDATE journal_saisie SET par = 'x'"))
            refuse_sql(lambda: cur.execute("SELECT * FROM secret_store"))
    finally:
        c.close()


@test("service web : jeton Ansible emis puis consomme une fois")
def _():
    c = service()
    try:
        jeton, _ = web.emettre_jeton(c, "evan")
        par, motif = web.consommer_jeton(c, jeton, "127.0.0.1")
        rejoue, motif2 = web.consommer_jeton(c, jeton, "127.0.0.1")
    finally:
        c.close()
    egal(par, "evan")
    egal(rejoue, None)
    egal("deja utilise" in motif2, True)
    c = collecte()
    try:
        with c.cursor() as cur:
            cur.execute("UPDATE client_token SET expire_le = %s",
                        (dt.datetime.now() - dt.timedelta(days=2),))
        c.commit()
        egal(db.purger_jetons_clients(c) >= 1, True, "purge par la collecte")
    finally:
        c.close()


@test("comptes locaux : cree par la collecte, utilise par le service")
def _():
    c = collecte()
    try:
        mdp = al.creer_ou_reinitialiser(c, "admin", "admin,commercial", "root")
    finally:
        c.close()
    w = service()
    try:
        # un compte se desactive, il ne s'efface pas : pas de DELETE
        def effacer():
            with w.cursor() as cur:
                cur.execute("DELETE FROM compte_local WHERE login = 'admin'")
        refuse_sql(effacer)
        w.rollback()
        jeton, compte = al.connecter(w, "admin", mdp, "10.0.0.1")
        egal(compte["doit_changer"], True)
        egal(al.session(w, jeton)["roles"], set(), "aucun role avant changement")
        al.changer_mdp(w, "admin", mdp, "cheval agrafe batterie", jeton)
        egal(al.session(w, jeton)["roles"], {"admin", "commercial", "lecture"})
        for i in range(5):
            try:
                al.connecter(w, "admin", "faux" + str(i), "10.0.0.1")
            except al.Refus:
                pass
        try:
            al.connecter(w, "admin", "cheval agrafe batterie", "10.0.0.1")
            raise AssertionError("le compte aurait du etre bloque")
        except al.Refus as exc:
            egal(exc.code, 429)
        al.deconnecter(w, jeton, "admin")
        egal(al.session(w, jeton), None, "session fermee")
    finally:
        w.close()


@test("onglet Comptes : un admin cree, reinitialise, modifie ; garde-fous")
def _():
    w = service()
    try:
        def refuse(fn, motif):
            try:
                fn()
            except db.VenteInvalide as exc:
                egal(motif in str(exc), True, "motif : " + str(exc))
                w.rollback()
                return
            raise AssertionError("aurait du etre refuse : " + motif)

        # un seul admin au depart (cree par le test precedent)
        mdp = al.creer_compte(w, "Lea.Martin", ["lecture"], "admin")
        egal(len(mdp) >= 20, True, "mot de passe genere")
        refuse(lambda: al.creer_compte(w, "lea.martin", ["admin"], "admin"),
               "existe deja")
        noms = {c["login"]: c for c in al.lister_comptes(w)}
        egal(noms["lea.martin"]["roles"], ["lecture"])
        egal("empreinte" in noms["lea.martin"], False, "jamais l'empreinte")

        al.modifier_compte(w, "lea.martin", "admin", roles=["commercial"])
        egal({c["login"]: c for c in al.lister_comptes(w)}["lea.martin"]["roles"],
             ["lecture", "commercial"])
        jeton, _ = al.connecter(w, "lea.martin", mdp, "10.0.0.2")
        al.modifier_compte(w, "lea.martin", "admin", actif=False)
        egal(al.session(w, jeton), None, "desactive : session fermee")

        refuse(lambda: al.modifier_compte(w, "admin", "admin", roles=["lecture"]),
               "toi-meme")
        refuse(lambda: al.modifier_compte(w, "admin", "admin", actif=False),
               "toi-meme")
        # un autre admin ne peut pas retirer le dernier admin actif
        refuse(lambda: al.modifier_compte(w, "admin", "chef", actif=False),
               "dernier admin")
        al.modifier_compte(w, "lea.martin", "admin", roles=["admin"], actif=True)
        al.modifier_compte(w, "admin", "lea.martin", roles=["commercial"])

        nouveau = al.reinitialiser_compte(w, "lea.martin", "admin")
        egal(nouveau != mdp, True, "nouveau mot de passe")
        refuse(lambda: al.reinitialiser_compte(w, "inconnu", "admin"), "inconnu")
        # remettre l'admin d'origine pour les tests suivants
        al.modifier_compte(w, "admin", "lea.martin", roles=["admin", "commercial"])
        actions = [l["action"] for l in db.lire_journal(w, 50)]
        egal("compte local modifie" in actions and "compte local cree" in actions,
             True, "au journal")
        egal(db.verifier_journal(w)["ok"], True, "chaine intacte")
    finally:
        w.close()


@test("depot scelle : depose par le service, ouvert et range par la collecte")
def _():
    os.environ["INVENTAIRE_MASTER_KEY"] = secret_store.generer_cle()
    cle = secret_store.charger_cle_maitresse()
    jeton_pve = "inventory@pve!readonly=0f9c2d1e-7a6b-4c3d-9e8f-1a2b3c4d5e6f"
    hote = UN_SERVEUR["reverse"]
    c = collecte()
    try:
        ds.initialiser(c, cle, "root")
    finally:
        c.close()
    w = service()
    try:
        echeance = dt.date.today() + dt.timedelta(days=365)
        egal(ds.deposer(w, [hote], jeton_pve, "evan", echeance), 1)
        etat = ds.etat_depots(w)[hote]
        egal((etat["etat"], etat["expire_le"]), ("en_attente", echeance.isoformat()))
        refuse_sql(lambda: secret_store.lire(w, ds.NOM_CLE_PRIVEE, cle))
        with w.cursor() as cur:
            refuse_sql(lambda: cur.execute(
                "UPDATE depot_jeton SET etat = 'accepte'"))
    finally:
        w.close()
    c = collecte()
    try:
        acc, ref = ds.traiter(c, cle, lambda h, j: (True, ""),
                              secret_store.nom_secret_pve, dire=lambda *a: None)
        range_ = secret_store.lire(c, secret_store.nom_secret_pve(hote), cle)
    finally:
        c.close()
    egal((acc, ref), (1, 0))
    egal(range_, jeton_pve)
    w = service()
    try:
        egal(ds.etat_depots(w)[hote]["etat"], "accepte")
    finally:
        w.close()


@test("migrations : appliquees une fois, dans l'ordre, par la collecte")
def _():
    import tempfile
    reelles = db.migrations_disponibles()
    c = collecte()
    try:
        egal(db.version_schema(c), db.version_attendue(),
             "schema.sql et les migrations du depot amenent a la version du code")
        with tempfile.TemporaryDirectory() as d:
            for _v, _nom, chemin in reelles:
                (Path(d) / chemin.name).write_text(chemin.read_text(encoding="utf-8"),
                                                   encoding="utf-8")
            prochaine = db.version_attendue() + 1
            (Path(d) / "{:04d}_essai.sql".format(prochaine)).write_text(
                "-- une table de test\n"
                "CREATE TABLE IF NOT EXISTS essai_migration (id INT PRIMARY KEY);\n"
                "INSERT IGNORE INTO essai_migration VALUES (1);\n", encoding="utf-8")
            egal(db.appliquer_migrations(c, d, dire=lambda *a: None), [prochaine])
            egal(db.appliquer_migrations(c, d, dire=lambda *a: None), [],
                 "deja appliquee")
            egal(db.version_schema(c), prochaine)
    finally:
        c.close()
    w = service()
    try:
        egal(db.version_schema(w) >= 1, True, "le service lit la version")
    finally:
        w.close()


@test("migration 0002 : les saisies par nom passent a cluster/vmid")
def _():
    deuxieme = DATA["guests"][1]
    c = collecte()
    try:
        with c.cursor() as cur:
            # des saisies d'avant la migration : la VM par son nom
            cur.execute("INSERT INTO commercial (vm_name, contrat, prix_vente, "
                        "cree_le, cree_par) VALUES (%s, 'L-1', 10, NOW(), 't'),"
                        " ('vm-disparue', 'L-2', 10, NOW(), 't')",
                        (deuxieme["name"],))
            cur.execute("INSERT INTO element (parent_type, parent_ref, nom, "
                        "cree_le, cree_par) VALUES ('vm', %s, 'ancien.fr', NOW(),"
                        " 't')", (deuxieme["name"],))
            cur.execute("INSERT INTO role_carte (cible_type, cible_ref, role, "
                        "maj_le, maj_par) VALUES ('vm', %s, 'web', NOW(), 't')",
                        (deuxieme["name"],))
        c.commit()
        # la migration est rejouable : on la repasse sur ces lignes
        chemin = [m for m in db.migrations_disponibles() if m[0] == 2][0][2]
        with c.cursor() as cur:
            for sql in db.instructions_sql(chemin.read_text(encoding="utf-8")):
                cur.execute(sql)
            c.commit()
            cur.execute("SELECT contrat, vm_ref FROM commercial "
                        "WHERE contrat IN ('L-1', 'L-2') ORDER BY contrat")
            ventes = [(r["contrat"], r["vm_ref"]) for r in cur.fetchall()]
            cur.execute("SELECT parent_ref FROM element WHERE nom = 'ancien.fr'")
            parent = cur.fetchone()["parent_ref"]
            cur.execute("SELECT cible_ref FROM role_carte WHERE role = 'web'")
            cible = cur.fetchone()["cible_ref"]
    finally:
        c.close()
    ref = orch.ref_vm(deuxieme)
    egal(ventes, [("L-1", ref), ("L-2", None)], "introuvable : laisse par nom")
    egal((parent, cible), (ref, ref))


@test("collecte : DELETE refuse partout, sauf ses deux purges")
def _():
    c = collecte()
    try:
        with c.cursor() as cur:
            for table in ("journal_saisie", "commercial", "element",
                          "secret_store", "guest_snapshot"):
                refuse_sql(lambda t=table: cur.execute("DELETE FROM " + t))
        c.rollback()
        db.store_run(c, DATA)                  # un second run, a purger
        egal(db.purge(c, 1) >= 1, True, "purge des vieux runs, en cascade")
        egal(db.load_latest(c) is not None, True, "le dernier run reste")
        egal(db.purger_jetons_clients(c) >= 0, True)
    finally:
        c.close()


@test("journal : la chaine tient sous des ecritures concurrentes")
def _():
    import threading
    erreurs = []

    def ecrire(qui):
        w = service()
        try:
            for i in range(15):
                with w.cursor() as cur:
                    db.journaliser(cur, qui, "essai", "cible {}".format(i))
                w.commit()
        except Exception as exc:  # remonte dans le test
            erreurs.append(exc)
        finally:
            w.close()
    fils = [threading.Thread(target=ecrire, args=("fil{}".format(k),))
            for k in range(4)]
    for f in fils:
        f.start()
    for f in fils:
        f.join()
    egal(erreurs, [])
    c = collecte()
    try:
        v = db.verifier_journal(c)
    finally:
        c.close()
    egal(v["ok"], True, "chaine intacte : {}".format(v["rupture"]))
    egal(v["chainees"] >= 60, True)


@test("journal : une ligne modifiee ou effacee, meme en root, se voit")
def _():
    c = collecte()
    try:
        egal(db.verifier_journal(c)["ok"], True)
        sql_root("UPDATE journal_saisie SET apres = '{}' WHERE n = 3", "inventory")
        c.commit()        # REPEATABLE READ : relire hors de la photo precedente
        v = db.verifier_journal(c)
        egal((v["ok"], v["rupture"]["n"], v["rupture"]["motif"]),
             (False, 3, "contenu modifie"))
        sql_root("DELETE FROM journal_saisie WHERE n = 3", "inventory")
        c.commit()
        v = db.verifier_journal(c)
        egal((v["rupture"]["n"], v["rupture"]["motif"]), (3, "ligne 3 absente"))
    finally:
        c.close()


# --------------------------------------------------------------------------

def main():
    if os.environ.get("INVENTAIRE_TEST_MARIADB") != "detruire":
        print("Ces tests DETRUISENT la base inventory du serveur local.\n"
              "Sur un MariaDB jetable : INVENTAIRE_TEST_MARIADB=detruire "
              "python3 tests_mariadb.py", file=sys.stderr)
        return 2
    for k in [k for k in os.environ if k.startswith("INVENTORY_DB_")]:
        del os.environ[k]                 # la conf de test fait foi
    try:
        mettre_en_place()
    except Exception:
        print("mise en place impossible :")
        traceback.print_exc()
        return 1
    print("base recreee : setup_db.sql, schema.sql x2, setup_db_web.sql, "
          "droits.sql x2")
    reussis, echecs = 0, []
    for libelle, fn in CAS:
        try:
            fn()
        except Exception:
            echecs.append((libelle, traceback.format_exc()))
            print("  ECHEC  " + libelle)
        else:
            reussis += 1
            print("  ok     " + libelle)
    print()
    print("{} reussis, {} echecs".format(reussis, len(echecs)))
    for libelle, trace in echecs:
        print()
        print("--- {} ---".format(libelle))
        print(trace.rstrip())
    return 1 if echecs else 0


if __name__ == "__main__":
    sys.exit(main())
