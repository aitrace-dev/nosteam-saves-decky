"""One-time browser sign-in. Pairing secrets never leave the local agent's private state."""

import socket
import time
from urllib.parse import urlencode, urlsplit

import httpx

from .config import configuration, lock, update_config
from .storage import atomic_json, durable_unlink, read_json

SERVER = "https://nosteam.bluggie.com"


def public_state(home):
    pending = read_json(home / "pairing.json", {})
    if not pending or pending.get("expires", 0) <= time.time():
        return {"status": "signed_out"}
    return {"status": "waiting", "url": pending["url"], "expires": pending["expires"]}


def begin(home, server=SERVER):
    with lock(home / "pairing.lock"):
        existing = public_state(home)
        if existing["status"] == "waiting":
            return existing
        origin = urlsplit(server)
        if origin.scheme != "https" and origin.hostname not in ("localhost", "127.0.0.1"):
            raise ValueError("Secure sign-in requires HTTPS")
        with httpx.Client(base_url=server, timeout=15) as client:
            response = client.post("/v1/devices/pair", json={"name": socket.gethostname()[:80]})
            response.raise_for_status()
            result = response.json()
        destination = urlsplit(result["verification_uri"])
        if (
            (destination.scheme, destination.netloc, destination.path)
            != (origin.scheme, origin.netloc, "/pair")
            or destination.query
            or destination.fragment
        ):
            raise ValueError("The server returned an invalid sign-in destination")
        url = result["verification_uri"] + "?" + urlencode({"code": result["user_code"]})
        atomic_json(
            home / "pairing.json",
            {
                "server": server,
                "device_code": result["device_code"],
                "url": url,
                "expires": time.time() + min(int(result["expires_in"]), 600),
                "next_poll": 0,
            },
        )
        return public_state(home)


def poll(home):
    # One lock also protects parallel sidebar/background polling from wasting the token response.
    with lock(home / "pairing.lock"):
        pending = read_json(home / "pairing.json", {})
        if not pending or pending.get("expires", 0) <= time.time():
            return {"status": "connected" if configuration(home).get("token") else "signed_out"}
        if pending.get("next_poll", 0) > time.time():
            return public_state(home)
        pending["next_poll"] = time.time() + 5
        atomic_json(home / "pairing.json", pending)
        with httpx.Client(base_url=pending["server"], timeout=15) as client:
            response = client.post(
                "/v1/devices/pair/token", json={"device_code": pending["device_code"]}
            )
        if response.status_code in (202, 429):
            return public_state(home)
        if response.status_code == 410:
            durable_unlink(home / "pairing.json")
            return {"status": "signed_out"}
        response.raise_for_status()
        result = response.json()
        if not str(result.get("access_token", "")).startswith("nsd_"):
            raise ValueError("Invalid device sign-in response")
        from .cli import configure

        configure(pending["server"], result["access_token"], home)
        update_config(lambda current: current.update(credential_expires=result["expires_at"]), home)
        durable_unlink(home / "pairing.json")
        return {"status": "connected"}
