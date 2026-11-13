#!/usr/bin/env python3
"""Epinglage du certificat PVE, contre un vrai serveur TLS local.

Un faux noeud PVE ecoute en HTTPS sur 127.0.0.1 avec un certificat emis,
comme le fait Proxmox, au nom du noeud (pve-13) et non au nom par lequel on
le joint. On verifie :

- le certificat epingle passe, malgre le nom qui ne correspond pas ;
- un autre certificat, meme au meme nom, est refuse ;
- un noeud qui change de certificat est refuse.

    python3 tests_pve_tls.py
"""

import datetime as dt
import json
import ssl
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import proxmox_inventory as pve

reussis = echecs = 0


def verifier(libelle, ok, detail=""):
    global reussis, echecs
    if ok:
        reussis += 1
        print("  ok   " + libelle)
    else:
        echecs += 1
        print("  ECHEC " + libelle + ("  : " + detail if detail else ""))


def certificat(nom):
    """Un certificat auto-signe au nom d'un noeud, comme PVE en produit."""
    cle = ec.generate_private_key(ec.SECP256R1())
    sujet = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, nom)])
    maintenant = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(sujet).issuer_name(sujet)
            .public_key(cle.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(maintenant - dt.timedelta(days=1))
            .not_valid_after(maintenant + dt.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(nom)]),
                           critical=False)
            .sign(cle, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    cle_pem = cle.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode("ascii")
    return pem, cle_pem


class Noeud(BaseHTTPRequestHandler):
    def do_GET(self):
        corps = json.dumps({"data": {"version": "8.3.4"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def log_message(self, *a):
        pass


def demarrer(pem, cle_pem, rep):
    (rep / "c.pem").write_text(pem)
    (rep / "k.pem").write_text(cle_pem)
    srv = HTTPServer(("127.0.0.1", 0), Noeud)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(rep / "c.pem", rep / "k.pem")
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def interroger(port, epingle):
    cli = pve.PveClient("127.0.0.1", "inventory@pve!readonly=x", port=port,
                        pinned_pem=epingle, timeout=5)
    return cli.get("/version")


def refuse(port, epingle):
    try:
        interroger(port, epingle)
    except Exception as exc:  # requests.SSLError ou PveError, selon la version
        return "CERTIFICATE_VERIFY_FAILED" in str(exc) or "certificat" in str(exc)
    return False


def main():
    with tempfile.TemporaryDirectory() as tmp:
        rep = Path(tmp)
        vrai, vraie_cle = certificat("pve-13")
        imposteur, cle_imposteur = certificat("pve-13")

        a = rep / "a"
        a.mkdir()
        srv = demarrer(vrai, vraie_cle, a)
        port = srv.server_address[1]

        # le nom du certificat (pve-13) n'est pas celui qu'on joint (127.0.0.1)
        try:
            v = interroger(port, vrai)
            verifier("certificat epingle accepte malgre le nom du noeud",
                     (v or {}).get("version") == "8.3.4", repr(v))
        except Exception as exc:
            verifier("certificat epingle accepte malgre le nom du noeud", False,
                     str(exc)[:200])

        verifier("un autre certificat au meme nom est refuse",
                 refuse(port, imposteur))
        srv.shutdown()

        # le noeud change de certificat : l'ancien epinglage le refuse
        b = rep / "b"
        b.mkdir()
        srv = demarrer(imposteur, cle_imposteur, b)
        verifier("un noeud qui change de certificat est refuse",
                 refuse(srv.server_address[1], vrai))
        srv.shutdown()

        # la verification reste obligatoire dans le contexte
        ctx = pve.PinnedAdapter(vrai).ctx
        verifier("verification du certificat obligatoire",
                 ctx.verify_mode == ssl.CERT_REQUIRED)

    print("{} reussis, {} echecs".format(reussis, echecs))
    return 1 if echecs else 0


if __name__ == "__main__":
    raise SystemExit(main())
