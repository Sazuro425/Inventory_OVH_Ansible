#!/usr/bin/env python3
"""
Client d'administration : recupere l'inventaire et lance les playbooks.

    infra update --token <jeton>   recupere l'inventaire et le met en cache
    infra list [motif]             ce que contient le cache
    infra simulate <playbook>      repetition a blanc, sans rien modifier
    infra run <playbook>           execution, apres simulation et confirmation

Le jeton se recupere sur la page web, apres authentification : il est a usage
unique et expire en quelques minutes. Rien d'autre n'est stocke sur le poste,
et surtout aucun acces aux API OVH ou Proxmox : le poste ne sait que lire
l'inventaire deja collecte.

Configuration dans ~/.config/infra/client.toml, ou par variables :
    INFRA_URL     adresse du service    (https://inventaire.exemple.lan)
    INFRA_CACHE   fichier de cache      (~/.cache/infra/inventaire.json)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TTL_DEFAUT_H = 24


def chemin_config():
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "infra" / "client.toml"


def chemin_cache():
    if os.environ.get("INFRA_CACHE"):
        return Path(os.environ["INFRA_CACHE"])
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / "infra" / "inventaire.json"


def lire_config():
    conf = {}
    p = chemin_config()
    if p.is_file():
        try:
            import tomllib
            conf = tomllib.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:  # fichier mal forme : on le dit, on continue
            print("config ignoree ({}) : {}".format(p, exc), file=sys.stderr)
    conf["url"] = os.environ.get("INFRA_URL") or conf.get("url")
    return conf


def log(msg):
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------------
# update
# --------------------------------------------------------------------------

def commande_update(args, conf):
    url = (args.url or conf.get("url") or "").rstrip("/")
    if not url:
        log("adresse du service inconnue : --url, INFRA_URL ou "
            + str(chemin_config()))
        return 2
    jeton = args.token or os.environ.get("INFRA_TOKEN")
    if not jeton:
        log("jeton manquant : recupere-le sur {} puis relance avec "
            "--token".format(url))
        return 2

    requete = urllib.request.Request(
        url + "/api/inventaire",
        headers={"Authorization": "Bearer " + jeton,
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(requete, timeout=60) as r:
            charge = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("erreur", "")
        except Exception:
            pass
        if exc.code == 403:
            log("jeton refuse : {}".format(detail or "expire ou deja utilise"))
            # ne conseiller d'en reprendre un que si c'est bien le sujet :
            # "jeton inconnu" vient plutot d'une faute de frappe ou d'un
            # copier-coller incomplet
            if "utilise" in detail or "expire" in detail:
                log("Un jeton ne sert qu'une fois : reprends-en un sur la page.")
            elif "inconnu" in detail:
                log("Verifie le copier-coller : un caractere manque souvent "
                    "en debut ou en fin.")
        else:
            log("le service a repondu {} : {}".format(exc.code, detail))
        return 1
    except urllib.error.URLError as exc:
        log("service injoignable ({}) : {}".format(url, exc.reason))
        return 1

    cache = chemin_cache()
    cache.parent.mkdir(parents=True, exist_ok=True)
    charge["recupere_le"] = int(time.time())
    charge["source"] = url
    # ecriture atomique : une interruption ne doit pas laisser un cache
    # tronque, qui ferait echouer tous les playbooks suivants
    tampon = cache.with_suffix(".tmp")
    tampon.write_text(json.dumps(charge, ensure_ascii=False), encoding="utf-8")
    os.replace(tampon, cache)
    try:
        os.chmod(cache, 0o600)
    except OSError:
        pass

    inv = charge["inventaire"]
    groupes = [k for k in inv if k not in ("_meta", "all")]
    log("inventaire a jour : {} hote(s), {} groupe(s)".format(
        len(inv["_meta"]["hostvars"]), len(groupes)))
    log("  snapshot #{}, collecte il y a {}".format(
        charge.get("snapshot"), duree_lisible(
            time.time() - charge.get("genere_le", time.time()))))
    log("  cache : " + str(cache))
    return 0


def duree_lisible(secondes):
    secondes = max(0, int(secondes))
    if secondes < 3600:
        return "{} min".format(secondes // 60)
    if secondes < 86400:
        return "{} h".format(secondes // 3600)
    return "{} j".format(secondes // 86400)


# --------------------------------------------------------------------------
# lecture du cache
# --------------------------------------------------------------------------

def charger_cache(ttl_h=TTL_DEFAUT_H, exiger=True):
    cache = chemin_cache()
    if not cache.is_file():
        if exiger:
            log("aucun inventaire en cache : lance 'infra update' d'abord")
        return None
    try:
        charge = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log("cache illisible ({}) : relance 'infra update'".format(exc))
        return None

    age = time.time() - charge.get("genere_le", 0)
    # L'age de la collecte compte plus que celui du telechargement : un cache
    # recupere il y a cinq minutes peut porter un inventaire de la veille.
    if age > ttl_h * 3600:
        log("ATTENTION : inventaire collecte il y a {} — il peut manquer des "
            "machines recentes".format(duree_lisible(age)))
    return charge


def commande_list(args, conf):
    charge = charger_cache(args.ttl)
    if charge is None:
        return 1
    inv = charge["inventaire"]
    hostvars = inv["_meta"]["hostvars"]
    motif = (args.motif or "").lower()

    if args.groupes:
        for nom in sorted(k for k in inv if k not in ("_meta", "all")):
            if motif and motif not in nom.lower():
                continue
            print("{:<32} {}".format(nom, len(inv[nom]["hosts"])))
        return 0

    for nom in sorted(hostvars):
        hv = hostvars[nom]
        if motif and motif not in nom.lower() and motif not in str(
                hv.get("pve_tags", "")).lower():
            continue
        print("{:<28} {:<16} {}".format(
            nom[:28], hv.get("ansible_host", "-"),
            hv.get("pve_tags") or hv.get("ovh_offer") or ""))
    return 0


# --------------------------------------------------------------------------
# simulate / run
# --------------------------------------------------------------------------

def inventaire_temporaire(charge):
    """Ecrit l'inventaire au format que ansible-playbook attend d'un fichier.

    Le JSON du service est au format de SORTIE d'un script d'inventaire
    ({_meta: ..., groupe: {hosts: [...]}}). Passe a -i, Ansible essaie de
    l'executer comme un script, puis de le lire en YAML, puis en INI, et
    echoue sur les trois. Un fichier d'inventaire a une autre structure :
    all.hosts porte les variables, all.children porte les groupes.

    On ecrit du JSON, qui est un sous-ensemble de YAML : Ansible le lit avec
    son greffon yaml, et le client n'a pas besoin de PyYAML.
    """
    inv = charge["inventaire"]
    hostvars = inv.get("_meta", {}).get("hostvars", {})
    natif = {"all": {"hosts": {nom: (v or {}) for nom, v in hostvars.items()},
                     "children": {}}}
    for nom, contenu in inv.items():
        if nom in ("_meta", "all"):
            continue
        hotes = contenu.get("hosts") if isinstance(contenu, dict) else None
        if not hotes:
            continue
        natif["all"]["children"][nom] = {"hosts": {h: None for h in hotes}}

    cible = chemin_cache().with_name("inventaire_ansible.yml")
    cible.write_text(json.dumps(natif, ensure_ascii=False), encoding="utf-8")
    return cible


def lancer_ansible(playbook, inventaire, limite, extra, verifier):
    cmd = ["ansible-playbook", "-i", str(inventaire), playbook]
    if limite:
        cmd += ["--limit", limite]
    if verifier:
        cmd += ["--check", "--diff"]
    cmd += extra or []
    log("$ " + " ".join(cmd))
    try:
        return subprocess.call(cmd)
    except FileNotFoundError:
        log("ansible-playbook introuvable sur ce poste")
        return 127


def commande_simulate(args, conf):
    charge = charger_cache(args.ttl)
    if charge is None:
        return 1
    inv = inventaire_temporaire(charge)
    log("Répétition à blanc : --check --diff, aucune modification appliquée.")
    log("Note : --check ne déroule pas la résolution de paquets ; pour savoir")
    log("ce qu'une mise à jour supprimerait, passe par une tâche de simulation")
    log("du gestionnaire de paquets (apt-get -s dist-upgrade).")
    return lancer_ansible(args.playbook, inv, args.limit, args.extra, True)


def commande_run(args, conf):
    charge = charger_cache(args.ttl)
    if charge is None:
        return 1
    inv = inventaire_temporaire(charge)

    cibles = args.limit or "all"
    log("=" * 70)
    log("Simulation prealable sur : " + cibles)
    log("=" * 70)
    code = lancer_ansible(args.playbook, inv, args.limit, args.extra, True)
    if code != 0:
        log("")
        log("La simulation a echoue (code {}) : execution annulee.".format(code))
        return code

    if not args.oui:
        log("")
        log("=" * 70)
        reponse = demander("Appliquer reellement sur {} ? [oui/non] ".format(
            cibles))
        if reponse.strip().lower() not in ("oui", "o", "yes", "y"):
            log("annule.")
            return 1

    log("")
    log("=" * 70)
    log("Execution reelle")
    log("=" * 70)
    return lancer_ansible(args.playbook, inv, args.limit, args.extra, False)


def demander(invite):
    if not sys.stdin.isatty():
        log("pas de terminal pour confirmer : ajoute --oui si tu assumes "
            "l'execution sans relecture")
        return ""
    try:
        return input(invite)
    except (EOFError, KeyboardInterrupt):
        return ""


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        prog="infra", description="Client d'administration de l'inventaire.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--ttl", type=int, default=TTL_DEFAUT_H,
                    help="age max du cache en heures avant avertissement "
                         "(defaut {})".format(TTL_DEFAUT_H))
    sous = ap.add_subparsers(dest="commande", required=True)

    p = sous.add_parser("update", help="recupere l'inventaire")
    p.add_argument("--token", help="jeton a usage unique pris sur la page web")
    p.add_argument("--url", help="adresse du service")
    p.set_defaults(fn=commande_update)

    p = sous.add_parser("list", help="affiche le contenu du cache")
    p.add_argument("motif", nargs="?", help="filtre sur le nom ou les tags")
    p.add_argument("--groupes", action="store_true", help="liste les groupes")
    p.set_defaults(fn=commande_list)

    p = sous.add_parser("simulate", help="repetition a blanc")
    p.add_argument("playbook")
    p.add_argument("--limit", help="restreint les cibles")
    p.add_argument("extra", nargs="*", help="arguments passes a ansible-playbook")
    p.set_defaults(fn=commande_simulate)

    p = sous.add_parser("run", help="execution, apres simulation")
    p.add_argument("playbook")
    p.add_argument("--limit", help="restreint les cibles")
    p.add_argument("--oui", action="store_true",
                   help="passe la confirmation (scripts)")
    p.add_argument("extra", nargs="*", help="arguments passes a ansible-playbook")
    p.set_defaults(fn=commande_run)

    args = ap.parse_args()
    return args.fn(args, lire_config())


if __name__ == "__main__":
    sys.exit(main())
