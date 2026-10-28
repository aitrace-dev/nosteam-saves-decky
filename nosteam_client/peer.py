"""TLS 1.3 direct transport with certificate pinning and narrowly scoped transfer grants."""

import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .game_files import BLOCK_SIZE, MAX_MANIFEST, canonical, open_file


def identity(home):
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    key_path, cert_path = home / "peer-key.pem", home / "peer-cert.pem"
    if key_path.exists():
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    else:
        key = ec.generate_private_key(ec.SECP256R1())
        data = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        with key_path.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    if not cert_path.exists():
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "NoSteam direct peer")])
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )
        with cert_path.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(cert.public_bytes(serialization.Encoding.PEM))
            stream.flush()
            os.fsync(stream.fileno())
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    return key_path, cert_path, cert.fingerprint(hashes.SHA256()).hex()


class PeerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        self.request.settimeout(5)
        self.request.do_handshake()
        super().setup()

    def log_message(self, *_):
        pass

    def do_GET(self):
        try:
            if len(self.path) > 220:
                raise ValueError("Invalid request")
            match = re.fullmatch(
                r"/v1/(manifest|content)/([a-f0-9]{64})(?:/([0-9]{1,7})/([0-9]{1,9}))?", self.path
            )
            if not match:
                return self.error(404)
            kind, version, file_index, chunk_index = match.groups()
            token = self.headers.get("Authorization", "")
            if not token.startswith("Bearer ") or len(token) > 8192:
                return self.error(401)
            claims = jwt.decode(
                token[7:],
                self.server.public_key,
                algorithms=["EdDSA"],
                issuer=self.server.issuer,
                audience=self.server.device_id,
                options={
                    "require": [
                        "exp",
                        "iat",
                        "nbf",
                        "iss",
                        "aud",
                        "sub",
                        "owner",
                        "version",
                        "scope",
                    ]
                },
            )
            if (
                claims["owner"] != self.server.owner
                or claims["version"] != version
                or claims["scope"] != "game-read"
                or claims["exp"] - claims["iat"] > 60
            ):
                return self.error(403)
            source = self.server.provider(version)
            if source is None:
                return self.error(404)
            if kind == "manifest" and file_index is None:
                data = canonical(source["manifest"])
                mime = "application/json"
            elif kind == "content" and file_index is not None:
                entry = source["manifest"]["files"][int(file_index)]
                index = int(chunk_index)
                expected = entry["hashes"][index]
                with open_file(Path(source["root"]), entry["path"]) as stream:
                    stream.seek(index * BLOCK_SIZE)
                    data = stream.read(min(BLOCK_SIZE, entry["size"] - index * BLOCK_SIZE))
                if hashlib.sha256(data).hexdigest() != expected:
                    return self.error(409)
                mime = "application/octet-stream"
            else:
                return self.error(404)
            remaining = claims["exp"] - time.time()
            if remaining <= 0:
                return self.error(401)
            self.connection.settimeout(min(10, remaining))
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except jwt.PyJWTError:
            self.error(403)
        except (IndexError, KeyError, ValueError, OSError):
            self.error(409)

    def error(self, status):
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
        except OSError:
            pass


class PeerServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, home, provider, owner, device_id, issuer, public_key, host="0.0.0.0", port=8468
    ):
        key, cert, self.fingerprint = identity(home)
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_3
        self.context.load_cert_chain(cert, key)
        self.provider, self.owner, self.device_id = provider, owner, device_id
        self.issuer, self.public_key = issuer, public_key
        self.slots = threading.BoundedSemaphore(16)
        super().__init__((host, port), PeerHandler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return self.context.wrap_socket(
            connection, server_side=True, do_handshake_on_connect=False
        ), address

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, *_):
        pass  # Do not log authorization headers, content, or local file paths.


class DirectClient:
    def __init__(self, host, port, fingerprint):
        self.host, self.port, self.fingerprint = host, port, fingerprint
        self.connection = None

    def close(self):
        if self.connection:
            self.connection.close()
            self.connection = None

    def get(self, path, token, limit):
        if self.connection is None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            context.minimum_version = ssl.TLSVersion.TLSv1_3
            connection = http.client.HTTPSConnection(
                self.host, self.port, context=context, timeout=2
            )
            connection.connect()
            peer = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
            if peer != self.fingerprint:
                connection.close()
                raise OSError("Source device identity changed")
            self.connection = connection
            connection.sock.settimeout(10)
        try:
            self.connection.request("GET", path, headers={"Authorization": f"Bearer {token}"})
            response = self.connection.getresponse()
            size = int(response.getheader("Content-Length", "-1"))
            if response.status != 200 or not 0 <= size <= limit:
                raise OSError("Source unavailable or content changed")
            data = response.read(limit + 1)
            if len(data) != size:
                raise OSError("Interrupted game chunk")
            return data
        except (OSError, http.client.HTTPException, ValueError):
            self.close()
            raise OSError("Direct transfer interrupted; verified progress is retained") from None

    def manifest(self, version, token):
        return json.loads(self.get(f"/v1/manifest/{version}", token, MAX_MANIFEST))

    def chunk(self, version, file_index, chunk_index, token):
        return self.get(f"/v1/content/{version}/{file_index}/{chunk_index}", token, BLOCK_SIZE)


def local_addresses():
    addresses = set()
    # UDP connect selects the route without sending a packet or involving a relay.
    for family, destination in ((socket.AF_INET, ("192.0.2.1", 9)),):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as connection:
                connection.connect(destination)
                addresses.add(connection.getsockname()[0])
        except OSError:
            pass
    return sorted(addresses)
