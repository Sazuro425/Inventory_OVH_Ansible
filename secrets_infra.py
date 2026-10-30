#!/usr/bin/env python3
"""
Lecture des secrets, par ordre de preference.

1. `$CREDENTIALS_DIRECTORY/<nom>` — secrets deposes par systemd via
   `LoadCredentialEncrypted=`. Ils vivent dans un tmpfs que seul le service
   peut lire, et **n'apparaissent jamais dans son environnement** : ni dans
   `/proc/<pid>/environ`, ni dans un vidage memoire.
2. La variable d'environnement — pour lancer les scripts a la main, et pour
   les postes qui n'ont pas systemd.

Le fichier ne s'appelle pas secrets.py : ce nom masquerait le module standard
`secrets`, dont le projet se sert pour tirer des jetons.
"""

from __future__ import annotations

import os
from pathlib import Path


def repertoire_credentials():
    chemin = os.environ.get("CREDENTIALS_DIRECTORY")
    return Path(chemin) if chemin else None


def lire_secret(nom, variable_env=None, defaut=None):
    """Secret nomme, cherche d'abord cote systemd puis dans l'environnement.

    Les espaces et sauts de ligne de fin sont retires : un fichier produit par
    `systemd-creds encrypt` depuis un echo contient un retour a la ligne que
    l'API refuserait silencieusement.
    """
    rep = repertoire_credentials()
    if rep is not None:
        fichier = rep / nom
        try:
            if fichier.is_file():
                valeur = fichier.read_text(encoding="utf-8").strip()
                if valeur:
                    return valeur
        except OSError:
            pass  # on retombe sur l'environnement plutot que d'echouer ici

    if variable_env:
        valeur = os.environ.get(variable_env)
        if valeur:
            return valeur.strip()
    return defaut


def resoudre(valeur):
    """Developpe les references d'une configuration.

    `cred:nom` lit un secret systemd, `env:NOM` une variable d'environnement.
    Toute autre valeur est rendue telle quelle — ce qui permet, en test, de
    poser directement la valeur dans la configuration.
    """
    if not isinstance(valeur, str):
        return valeur
    if valeur.startswith("cred:"):
        return lire_secret(valeur[5:])
    if valeur.startswith("env:"):
        nom = valeur[4:]
        # un secret systemd portant le nom de la variable l'emporte : cela
        # permet de passer une configuration existante sous systemd-creds
        # sans la reecrire
        return lire_secret(nom.lower(), nom) or lire_secret(nom, nom)
    return valeur


def source(nom, variable_env=None):
    """D'ou vient ce secret ? Pour les messages de diagnostic."""
    rep = repertoire_credentials()
    if rep is not None and (rep / nom).is_file():
        return "systemd-creds"
    if variable_env and os.environ.get(variable_env):
        return "environnement"
    return "absent"
