#!/usr/bin/env python3
"""
inventory - administration des secrets de l'inventaire.

    inventory --list                 etat des secrets, sans rien dechiffrer
    inventory --init-key             tire une clef maitresse (premiere fois)
    inventory --ovh                  renouvelle les trois clefs OVH
    inventory --pve HOTE             renouvelle le jeton d'un hyperviseur
    inventory --db                   change le mot de passe de la base
    inventory --rotate-key           rechiffre tout sous une nouvelle clef

Trois regles valent pour toutes les sous-commandes.

**Aucune valeur en argument.** Un `ps aux` exposerait la ligne de commande a
tous les utilisateurs de la machine, et l'historique du shell la garderait.
La saisie passe par getpass, donc sans echo.

**Tester avant d'ecrire.** Un jeton invalide ne doit jamais remplacer un
jeton qui fonctionne. En cas d'echec, rien n'est ecrit et l'ancienne version
reste active.

**Dire ce qui a echoue.** Une signature invalide et un droit manquant sont
deux problemes differents : le premier est fatal, le second laisse la
collecte tourner avec des champs vides. Les clients OVH et PVE portent deja
cette distinction dans l'attribut `fatal` de leurs exceptions.

A lancer sur la machine de collecte, sous le compte qui possede la clef
maitresse. Le compte du service web n'a pas les droits necessaires sur
secret_store, et c'est voulu.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

import infra_inventory as orch
import inventory_db
import secret_store
import secrets_infra
from secret_store import SecretErreur

# Les trois clefs OVH vont ensemble : une signature se calcule avec les trois,
# en changer une seule ne veut rien dire.
CHAMPS_OVH = (
    ("ovh_application_key", "Application key"),
    ("ovh_application_secret", "Application secret"),
    ("ovh_consumer_key", "Consumer key"),
)

# Nom du credential qui porte la clef a venir, le temps d'une rotation.
CRED_NOUVELLE_CLE = "master_key_nouvelle"

LARGEUR = 46


# --------------------------------------------------------------------------
# Affichage
# --------------------------------------------------------------------------

def ligne_test(libelle, ok, detail=""):
    """Une ligne de resultat de test, alignee."""
    points = "." * max(1, LARGEUR - len(libelle))
    print("  {} {} {}  {}".format(libelle, points,
                                  "[OK]" if ok else "[KO]", detail).rstrip())


def erreur(message):
    print("  " + message, file=sys.stderr)


def exiger_terminal():
    """Refuse de continuer si l'entree n'est pas un terminal.

    Sans ce garde-fou, `echo foo | inventory --ovh` ecrirait un secret lu
    depuis un pipe, donc potentiellement depuis un fichier ou un historique.
    getpass ne masque pas dans ce cas : il lit et se contente d'un
    avertissement sur stderr.
    """
    if not sys.stdin.isatty():
        erreur("inventory demande une saisie interactive : "
               "lance-le depuis un terminal.")
        raise SystemExit(2)


def demander_masque(invite, ancien_existe=False, essais=3):
    """Lit une valeur sans echo. Rend None si l'utilisateur laisse vide."""
    for reste in range(essais, 0, -1):
        valeur = getpass.getpass("  {:19}: ".format(invite))
        # Le strip() n'est pas cosmetique : une espace ou une tabulation
        # recopiee avec la clef produit une signature invalide, et le message
        # d'erreur d'OVH ne dit pas que la cause est un caractere invisible.
        valeur = valeur.strip()
        if valeur:
            return valeur
        if ancien_existe:
            return None          # conserver la valeur en place
        if reste > 1:
            erreur("valeur obligatoire ({} essai(s) restant(s))".format(
                reste - 1))
    raise SystemExit("abandon : {} non renseigne".format(invite))


def utilisateur():
    """Qui lance la commande, pour la colonne cree_par.

    SUDO_USER d'abord : sous sudo, LOGNAME vaut root et la trace perdrait la
    seule information utile, celle de la personne.
    """
    for var in ("SUDO_USER", "LOGNAME", "USER", "USERNAME"):
        valeur = os.environ.get(var)
        if valeur:
            return valeur
    return "inconnu"


def etat_versions(conn):
    """{nom: version active} pour afficher ce qui est en place."""
    return {l["nom"]: l["version"] for l in secret_store.lister(conn)}


def afficher_place(etat, noms):
    """Rappelle ce qui est deja pose, avant de demander une saisie."""
    connus = [n for n in noms if n in etat]
    if connus:
        for nom in connus:
            print("  en place : {} v{}".format(nom, etat[nom]))
        print("  Laisser vide pour conserver la valeur en place.")
    else:
        print("  aucune valeur en place pour ce secret.")
    print()


# --------------------------------------------------------------------------
# Tests - aucun n'ecrit quoi que ce soit
# --------------------------------------------------------------------------

def tester_ovh(valeurs, endpoint="ovh-eu"):
    """Verifie les trois clefs OVH. Rend (ok, motif)."""
    import ovh_inventory

    try:
        cli = ovh_inventory.OvhClient(
            endpoint,
            valeurs["ovh_application_key"],
            valeurs["ovh_application_secret"],
            valeurs["ovh_consumer_key"])
    except ovh_inventory.OvhError as exc:
        return False, str(exc)

    # /me ne demande aucun droit particulier : s'il passe, les trois clefs se
    # correspondent bien. C'est le test de la signature, et lui seul.
    try:
        moi = cli.get("/me")
    except ovh_inventory.OvhError as exc:
        ligne_test("Signature  GET /me", False)
        return False, str(exc)
    ligne_test("Signature  GET /me", True,
               "compte " + str(moi.get("nichandle", "?")))

    # Les chemins metier valident les droits de la clef consommateur. Separes
    # du test precedent a dessein : confondus, ils enverraient regenerer des
    # clefs correctes alors que la cause est un droit absent.
    try:
        serveurs = cli.get("/dedicated/server")
    except ovh_inventory.OvhError as exc:
        ligne_test("Droit  GET /dedicated/server", False)
        return False, "droit manquant sur /dedicated/server ({})".format(exc)
    ligne_test("Droit  GET /dedicated/server", True,
               "{} serveur(s)".format(len(serveurs)))

    if serveurs:
        chemin = "/dedicated/server/{}/serviceInfos".format(serveurs[0])
        try:
            cli.get(chemin)
        except ovh_inventory.OvhError as exc:
            ligne_test("Droit  GET .../serviceInfos", False)
            return False, "droit manquant sur serviceInfos ({})".format(exc)
        ligne_test("Droit  GET .../serviceInfos", True, "expirations lisibles")

    return True, ""


def tester_pve(hote, jeton, conf):
    """Verifie un jeton PVE sur un hyperviseur. Rend (ok, motif)."""
    import proxmox_inventory as pve
    from pathlib import Path

    port = conf["pve"].get("port", 8006)
    pinned = None
    if conf["pve"].get("pin_certs"):
        # Teste dans les memes conditions que la collecte : un jeton valide
        # sans epinglage et utilise ensuite avec epinglage ne prouve rien.
        store = Path(conf["pve"].get("pin_dir", "pinned"))
        store.mkdir(exist_ok=True)
        try:
            pinned, _, _ = pve.pin_certificate(hote, port,
                                               store / (hote + ".pem"))
        except Exception as exc:
            ligne_test("Certificat  epinglage", False)
            return False, "certificat injoignable : {}".format(exc)
        ligne_test("Certificat  epinglage", True, "ancre en place")

    cli = pve.PveClient(hote, jeton, port=port,
                        ca_bundle=conf["pve"].get("ca_bundle"),
                        pinned_pem=pinned)

    try:
        version = cli.get("/version")
    except pve.PveError as exc:
        ligne_test("Jeton  GET /version", False)
        return False, str(exc)
    ligne_test("Jeton  GET /version", True,
               "PVE " + str(version.get("version", "?")))

    # /cluster/status dit aussi si l'hote est seul ou dans un cluster, ce qui
    # determine la deduplication a la collecte.
    try:
        etat = cli.get("/cluster/status")
    except pve.PveError as exc:
        ligne_test("Droit  GET /cluster/status", False)
        return False, "droit manquant sur /cluster/status ({})".format(exc)
    noms = [e.get("name") for e in (etat or [])
            if e.get("type") == "cluster" and e.get("name")]
    ligne_test("Droit  GET /cluster/status", True,
               ("cluster " + noms[0]) if noms else "noeud isole")

    return True, ""


def tester_db(utilisateur_db, mot_de_passe, conf):
    """Verifie un couple compte / mot de passe. Rend (ok, motif)."""
    settings = inventory_db.db_settings(conf)
    settings["user"] = utilisateur_db
    settings["password"] = mot_de_passe
    # Une connexion neuve, jamais celle de travail : tout l'interet est de
    # savoir si *ce* couple fonctionne, pas celui deja en place.
    try:
        conn = inventory_db._ouvre(settings, settings["database"])
        with conn.cursor() as cur:
            cur.execute("SELECT 1 AS un")
            cur.fetchone()
        conn.close()
    except Exception as exc:
        ligne_test("Connexion  SELECT 1", False)
        return False, str(exc)
    ligne_test("Connexion  SELECT 1", True,
               "{}@{}".format(utilisateur_db, settings["host"]))
    return True, ""


# --------------------------------------------------------------------------
# Sous-commandes
# --------------------------------------------------------------------------

def commande_list(args, conf, conn):
    """Affiche l'etat des secrets, sans dechiffrer.

    Aucune clef maitresse n'est chargee ici : l'etat reste consultable meme
    si le TPM est inaccessible, ce qui est precisement le moment ou on a
    besoin de savoir ce qui est en place.
    """
    lignes = secret_store.lister(conn)
    if not lignes:
        print("  aucun secret en base.")
        print("  en poser un : inventory --ovh, ou inventory --pve HOTE")
        return 0

    print("  {:32} {:>5}  {:>6}  {:19}  {}".format(
        "SECRET", "VER", "CLEF", "POSE LE", "PAR"))
    for l in lignes:
        print("  {:32} {:>5}  {:>6}  {:19}  {}".format(
            l["nom"], "v{}".format(l["version"]),
            "v{}".format(l["cle_version"]),
            str(l["cree_le"]), l["cree_par"]))

    versions = {l["cle_version"] for l in lignes}
    if len(versions) > 1:
        print()
        erreur("rotation inachevee : {} versions de clef en service "
               "({}). Relance inventory --rotate-key --appliquer.".format(
                   len(versions), ", ".join("v{}".format(v)
                                            for v in sorted(versions))))
    return 0


def commande_init_key(args, conf):
    """Tire une clef maitresse et explique comment la sceller."""
    deja = secrets_infra.lire_secret("master_key", "INVENTAIRE_MASTER_KEY")
    if deja and not args.force:
        erreur("une clef maitresse est deja en place.")
        erreur("l'ecraser rendrait TOUS les secrets en base illisibles.")
        erreur("pour changer de clef : inventory --rotate-key")
        erreur("pour passer outre malgre tout : --force")
        return 1

    # La clef seule sur stdout, les instructions sur stderr : c'est ce qui
    # permet le tube ci-dessous sans que la clef touche le disque en clair.
    print(secret_store.generer_cle())

    erreur("")
    erreur("Clef tiree sur la sortie standard. A sceller sans la stocker :")
    erreur("")
    erreur("    inventory --init-key | sudo systemd-creds encrypt \\")
    erreur("        --name=master_key - \\")
    erreur("        /etc/inventaire-infra/creds/master_key.cred")
    erreur("")
    erreur("Puis decommenter dans inventaire-collecte.service :")
    erreur("    LoadCredentialEncrypted=master_key:"
           "/etc/inventaire-infra/creds/master_key.cred")
    return 0


def commande_ovh(args, conf, conn, cle):
    """Renouvelle les trois clefs OVH."""
    exiger_terminal()
    etat = etat_versions(conn)
    noms = [nom for nom, _ in CHAMPS_OVH]
    afficher_place(etat, noms)

    valeurs, inchanges = {}, []
    for nom, invite in CHAMPS_OVH:
        saisie = demander_masque(invite, ancien_existe=(nom in etat))
        if saisie is None:
            # Relire l'ancienne valeur suppose que la clef maitresse
            # fonctionne. Si elle ne fonctionne pas, le dire franchement
            # plutot que de tester un jeu de clefs a moitie faux.
            valeurs[nom] = secret_store.lire(conn, nom, cle)
            inchanges.append(nom)
        else:
            valeurs[nom] = saisie

    if len(inchanges) == len(noms):
        print()
        print("  rien a changer.")
        return 0

    print()
    ok, motif = tester_ovh(valeurs, conf["ovh"].get("endpoint", "ovh-eu"))
    if not ok:
        print()
        erreur(motif)
        erreur("Rien n'a ete ecrit. La version en place reste active.")
        return 1

    # Les trois clefs s'ecrivent ensemble : une signature se calcule avec les
    # trois, et n'en changer qu'une laisserait la base dans un etat qui ne
    # signe rien.
    print()
    for nom, _ in CHAMPS_OVH:
        if nom in inchanges:
            print("  {} v{} inchange".format(nom, etat[nom]))
            continue
        v = secret_store.ecrire(conn, nom, valeurs[nom], utilisateur(), cle)
        print("  {} v{} ecrit".format(nom, v))
    return 0


def nom_secret_pve(hote):
    """Nom du secret pour un hote. Doit coller a ce qu'on met dans infra.yml.

    Garder la meme transformation des deux cotes, sinon la reference `db:`
    de la configuration ne resout rien.
    """
    if not hote:
        return "pve_token"
    propre = "".join(c if c.isalnum() else "_" for c in hote.lower())
    return "pve_token_" + propre.strip("_")


def commande_pve(args, conf, conn, cle):
    """Renouvelle le jeton d'un hyperviseur."""
    exiger_terminal()
    hote = (args.pve or "").strip()
    nom = nom_secret_pve(hote)
    etat = etat_versions(conn)
    afficher_place(etat, [nom])

    saisie = demander_masque("Jeton PVE", ancien_existe=(nom in etat))
    if saisie is None:
        print()
        print("  rien a changer.")
        return 0

    if not hote:
        erreur("")
        erreur("jeton par defaut : aucun hote a interroger pour le tester.")
        erreur("relance avec --pve HOTE pour le verifier avant ecriture,")
        erreur("ou accepte de l'ecrire sans test avec --force.")
        if not args.force:
            return 1
        ok, motif = True, ""
    else:
        print()
        ok, motif = tester_pve(hote, saisie, conf)

    if not ok:
        print()
        erreur(motif)
        erreur("Rien n'a ete ecrit. La version en place reste active.")
        return 1

    print()
    v = secret_store.ecrire(conn, nom, saisie, utilisateur(), cle)
    print("  {} v{} ecrit".format(nom, v))
    if nom not in etat:
        print()
        print("  a declarer dans infra.yml :")
        if hote:
            print("    pve:")
            print("      tokens:")
            print('        {}: "db:{}"'.format(hote, nom))
        else:
            print('      default_token: "db:{}"'.format(nom))
    return 0


def commande_db(args, conf, conn):
    """Change le mot de passe de la base.

    ATTENTION : ce secret ne va PAS en base. Il faut le lire pour atteindre
    la base, donc l'y mettre empecherait la prochaine collecte de l'ouvrir.
    Cette sous-commande teste, puis dit quoi executer.
    """
    exiger_terminal()
    settings = inventory_db.db_settings(conf)
    compte = settings["user"]
    print("  compte : {}@{}:{}/{}".format(
        compte, settings["host"], settings["port"], settings["database"]))
    print("  Le mot de passe de la base ne va pas en base : il faut le lire")
    print("  pour l'atteindre. Il reste dans systemd-creds.")
    print()

    nouveau = demander_masque("Nouveau mot de passe")
    confirme = demander_masque("Confirmation")
    if nouveau != confirme:
        erreur("les deux saisies differaient. Rien n'a ete fait.")
        return 1

    print()
    print("  1. cote serveur :")
    print("       ALTER USER '{}'@'localhost' IDENTIFIED VIA "
          "mysql_native_password".format(compte))
    print("         USING PASSWORD('<le mot de passe saisi>');")
    print("  2. cote machine de collecte :")
    print("       systemd-creds encrypt --name=inventory_db_password - \\")
    print("           /etc/inventaire-infra/creds/inventory_db_password.cred")
    print("  3. systemctl restart inventaire-collecte.service")
    print()

    # Le test vient apres les instructions, et son echec n'est pas une
    # erreur de la commande : au premier passage le ALTER USER n'a pas encore
    # ete joue, donc le nouveau mot de passe ne peut pas fonctionner. D'ou un
    # code de retour 0 dans les deux cas, et un message qui tranche.
    ok, motif = tester_db(compte, nouveau, conf)
    if ok:
        print("  Le mot de passe est deja actif cote serveur : il ne reste")
        print("  que les etapes 2 et 3.")
    else:
        print("  Refuse pour l'instant, ce qui est normal avant l'etape 1 :")
        erreur(motif)
        print("  relance cette commande apres le ALTER USER pour verifier.")
    return 0


def commande_rotate_key(args, conf, conn):
    """Rechiffre tous les secrets sous une nouvelle clef maitresse."""
    ancienne = secret_store.charger_cle_maitresse()
    nouvelle_b64 = secrets_infra.lire_secret(CRED_NOUVELLE_CLE)

    if not args.appliquer:
        if nouvelle_b64:
            erreur("une clef de rotation est deja scellee ({}).".format(
                CRED_NOUVELLE_CLE))
            erreur("relance avec --appliquer pour rechiffrer.")
            return 1
        print(secret_store.generer_cle())
        erreur("")
        erreur("Nouvelle clef sur la sortie standard. Rotation en trois temps.")
        erreur("")
        erreur("  1. sceller la clef a venir, SANS toucher l'actuelle :")
        erreur("       inventory --rotate-key | sudo systemd-creds encrypt \\")
        erreur("           --name={} - \\".format(CRED_NOUVELLE_CLE))
        erreur("           /etc/inventaire-infra/creds/{}.cred".format(
            CRED_NOUVELLE_CLE))
        erreur("")
        erreur("  2. charger les DEUX clefs dans inventaire-collecte.service,")
        erreur("     puis : inventory --rotate-key --appliquer")
        erreur("")
        erreur("  3. une fois inventory --list sur une seule version de clef :")
        erreur("     remplacer master_key.cred par le nouveau, retirer la")
        erreur("     ligne {}, et supprimer son fichier.".format(
            CRED_NOUVELLE_CLE))
        return 0

    if not nouvelle_b64:
        erreur("clef de rotation absente : {} n'est pas lisible.".format(
            CRED_NOUVELLE_CLE))
        erreur("lance d'abord inventory --rotate-key, sans --appliquer.")
        return 1

    import base64
    nouvelle = base64.b64decode(nouvelle_b64.strip(), validate=True)
    if len(nouvelle) != secret_store.TAILLE_CLE:
        erreur("clef de rotation de longueur incorrecte.")
        return 1
    if nouvelle == ancienne:
        erreur("la clef de rotation est identique a l'actuelle.")
        return 1

    cible = secret_store.VERSION_CLE_COURANTE + 1
    lignes = secret_store.lister(conn)
    a_faire = [l for l in lignes if l["cle_version"] != cible]
    if not a_faire:
        print("  rien a rechiffrer : les {} secret(s) sont deja en clef "
              "v{}.".format(len(lignes), cible))
        return 0

    print("  {} secret(s) a rechiffrer vers la clef v{}.".format(
        len(a_faire), cible))
    print()
    faits, echecs = 0, []
    # Secret par secret, et non en un bloc : trente secrets rechiffres dans
    # une transaction unique ne se reprennent pas si la machine tombe au
    # milieu. La colonne cle_version dit ou on s'est arrete, et une seconde
    # execution reprend le reste.
    for l in a_faire:
        nom = l["nom"]
        try:
            clair = secret_store.lire(conn, nom, ancienne)
            v = secret_store.ecrire(conn, nom, clair, utilisateur(),
                                    nouvelle, cle_version=cible)
        except Exception as exc:
            echecs.append(nom)
            ligne_test(nom, False, str(exc)[:40])
            continue
        faits += 1
        ligne_test(nom, True, "v{} en clef v{}".format(v, cible))

    print()
    print("  {} rechiffre(s), {} echec(s).".format(faits, len(echecs)))
    if echecs:
        erreur("relance --appliquer : les secrets deja traites seront "
               "ignores.")
        return 1
    print("  Verifie avec inventory --list, puis permute les fichiers .cred.")
    return 0


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        prog="inventory",
        description="Administration des secrets de l'inventaire.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--config", default=os.environ.get("INFRA_CONFIG",
                                                       "infra.yml"))

    # Une seule action par appel : les combiner n'aurait pas de sens et
    # ouvrirait la porte a une rotation lancee par megarde.
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true",
                   help="etat des secrets, sans rien dechiffrer")
    g.add_argument("--init-key", action="store_true",
                   help="tire une clef maitresse")
    g.add_argument("--ovh", action="store_true",
                   help="renouvelle les trois clefs OVH")
    g.add_argument("--pve", metavar="HOTE", nargs="?", const="",
                   help="renouvelle le jeton d'un hyperviseur")
    g.add_argument("--db", action="store_true",
                   help="change le mot de passe de la base")
    g.add_argument("--rotate-key", action="store_true",
                   help="rechiffre tout sous une nouvelle clef")

    ap.add_argument("--appliquer", action="store_true",
                    help="execute la rotation (--rotate-key)")
    ap.add_argument("--force", action="store_true",
                    help="passe outre les garde-fous")

    args = ap.parse_args()

    try:
        conf = orch.load_config(args.config)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1

    # --init-key ne touche ni la base ni la clef en place : le traiter avant
    # tout le reste, puisqu'il sert justement quand rien n'est encore pose.
    if args.init_key:
        return commande_init_key(args, conf)

    # Tirer la clef de rotation ne demande pas la base non plus. La traiter
    # ici permet de preparer une rotation avant que la base soit joignable,
    # et surtout de ne pas faire dependre la generation d'une clef de l'etat
    # d'un service qui n'a rien a y voir.
    if args.rotate_key and not args.appliquer:
        try:
            return commande_rotate_key(args, conf, None)
        except SecretErreur as exc:
            erreur("secret : {}".format(exc))
            return 1

    # Refuser une saisie non masquee AVANT d'ouvrir la base : sinon une base
    # injoignable masque le vrai motif du refus, et l'utilisateur cherche du
    # cote du reseau un probleme de terminal.
    if args.ovh or args.pve is not None or args.db:
        exiger_terminal()

    conn = None
    try:
        conn = inventory_db.connect(conf)

        if args.list:
            return commande_list(args, conf, conn)
        if args.db:
            # ne touche pas la clef maitresse : ce secret n'est pas en base
            return commande_db(args, conf, conn)
        if args.rotate_key:
            return commande_rotate_key(args, conf, conn)

        cle = secret_store.charger_cle_maitresse()
        if args.ovh:
            return commande_ovh(args, conf, conn, cle)
        return commande_pve(args, conf, conn, cle)

    except SecretErreur as exc:
        erreur("secret : {}".format(exc))
        return 1
    except inventory_db.DbError as exc:
        erreur("base : {}".format(exc))
        return 1
    except KeyboardInterrupt:
        erreur("")
        erreur("interrompu. Rien n'a ete ecrit.")
        return 130
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
