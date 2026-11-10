#!/usr/bin/env python3
"""
La politique de securite (CSP) d'une page dont le script et le style sont
en ligne : chacun est autorise par son empreinte SHA-256, rien d'autre.

Sans cela, il faut 'unsafe-inline', qui autorise aussi tout script qu'une
faille parviendrait a injecter dans la page. Avec les empreintes, seul le
script genere ici s'execute : un nom de VM piege, meme s'il echappait a
l'echappement, ne serait qu'un texte.

La carte la porte dans une balise <meta> (topologie.py), parce qu'elle est
servie par Apache comme fichier statique et s'ouvre aussi hors ligne. Le
navigateur applique toutes les politiques recues : l'en-tete d'Apache, plus
permissif, ne relache rien.
"""

from __future__ import annotations

import base64
import hashlib
import re

_SCRIPT = re.compile(r"<script>(.*?)</script>", re.S)
_STYLE = re.compile(r"<style>(.*?)</style>", re.S)


def empreinte(texte):
    # le parseur HTML ramene les fins de ligne a \n avant de calculer
    texte = texte.replace("\r\n", "\n")
    brut = hashlib.sha256(texte.encode("utf-8")).digest()
    return "'sha256-{}'".format(base64.b64encode(brut).decode("ascii"))


def politique(page, cadre=False):
    """La CSP de la page HTML donnee, ses blocs en ligne par empreinte.

    cadre=True ajoute frame-ancestors, qu'une balise <meta> ignore et que
    seul un en-tete peut porter.
    """
    scripts = " ".join(empreinte(t) for t in _SCRIPT.findall(page)) or "'none'"
    styles = " ".join(empreinte(t) for t in _STYLE.findall(page)) or "'none'"
    regles = ["default-src 'none'", "script-src " + scripts,
              "style-src " + styles, "img-src data:", "connect-src 'self'",
              "base-uri 'none'", "form-action 'none'"]
    if cadre:
        regles.append("frame-ancestors 'none'")
    return "; ".join(regles)
