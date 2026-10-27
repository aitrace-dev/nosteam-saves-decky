import json
import time

import httpx
import pytest
from nosteam_client import account, automatic
from nosteam_client.config import configuration, update_config
from nosteam_client.storage import atomic_json
from nosteam_client.sync import Offline, Remote


def test_revoked_device_requests_signin_without_discarding_local_configuration(tmp_path):
    update_config(
        lambda cfg: cfg.update(server="https://example.test", token="device-token"), tmp_path
    )
    remote = Remote(
        configuration(tmp_path),
        transport=httpx.MockTransport(lambda _: httpx.Response(401)),
        home=tmp_path,
    )
    try:
        with pytest.raises(Offline, match="Sign in again"):
            remote.head("example-game")
    finally:
        remote.close()
    assert configuration(tmp_path)["auth_required"] is True
    assert configuration(tmp_path)["token"] == "device-token"


def shortcut(root, title="Example Game", app="2147484000"):
    return {
        "title": title,
        "shortcut_id": app,
        "registry": str(root / "shortcuts.vdf"),
        "exe": '"/games/example.exe"',
    }


def test_identity_is_portable_across_devices_and_shortcut_ids(tmp_path):
    a = shortcut(tmp_path / "desktop")
    b = {
        **shortcut(tmp_path / "handheld", app="2147484999"),
        "exe": '"/different/place/example.exe"',
    }
    assert automatic.game_identity(a) == automatic.game_identity(b)
    assert automatic.game_identity(a, "Canonical Game") == automatic.game_identity(
        {**b, "title": "Renamed shortcut"}, "Canonical Game"
    )
    assert automatic.game_identity(a) != automatic.game_identity({**a, "title": "Other Game"})
    assert automatic.game_identity({**a, "title": "Unknown"}) is None


def prepare(tmp_path, monkeypatch, shortcuts, suggestions):
    home = tmp_path / "state"
    update_config(lambda value: value.update(server="https://example.test", token="x" * 40), home)
    atomic_json(home / "automation.json", {"catalog_retry": time.time() + 10000})
    monkeypatch.setattr(automatic, "discover", lambda: shortcuts)
    monkeypatch.setattr(automatic, "suggest", lambda item, *_: suggestions[item["title"]])
    monkeypatch.setattr(automatic, "cached", lambda *_: [])

    class Remote:
        def __init__(self, *_, **__):
            pass

        def close(self):
            pass

    monkeypatch.setattr(automatic, "Remote", Remote)
    return home


def test_all_known_games_are_prepared_without_selection(tmp_path, monkeypatch):
    games = [shortcut(tmp_path, "First"), shortcut(tmp_path, "Second", "2147484001")]
    roots = []
    for title in ["First", "Second"]:
        path = tmp_path / title
        path.mkdir()
        (path / "slot.sav").write_text(title)
        roots.append(str(path))
    home = prepare(
        tmp_path,
        monkeypatch,
        games,
        {
            title: {"matched_title": title, "suggested_roots": [root]}
            for title, root in zip(["First", "Second"], roots)
        },
    )
    result = automatic.plan(home)
    assert len(result["ready"]) == 2 and not result["pending"]
    assert not configuration(home)[
        "games"
    ]  # The frontend must verify/install Steam's wrapper first.


def test_unknown_game_negotiates_with_server_and_auto_accepts_verified_result(
    tmp_path, monkeypatch
):
    item = shortcut(tmp_path)
    save = tmp_path / "save"
    save.mkdir()
    (save / "slot.sav").write_text("progress")
    home = prepare(
        tmp_path,
        monkeypatch,
        [item],
        {item["title"]: {"matched_title": None, "suggested_roots": []}},
    )
    calls = []

    def negotiate(*args):
        calls.append(args[1])
        return {"suggested_roots": [str(save)]}

    monkeypatch.setattr(automatic, "negotiate", negotiate)
    result = automatic.plan(home)
    assert len(calls) == 1 and result["ready"][0]["roots"] == [str(save)]
    assert result["pending"] == []


def test_missing_or_ambiguous_ai_paths_wait_without_manual_folder_request(tmp_path, monkeypatch):
    item = shortcut(tmp_path)
    home = prepare(
        tmp_path,
        monkeypatch,
        [item],
        {item["title"]: {"matched_title": None, "suggested_roots": []}},
    )
    monkeypatch.setattr(automatic, "negotiate", lambda *_: {"suggested_roots": ["/one", "/two"]})
    first = automatic.plan(home, now=100)
    assert first["ready"] == [] and first["pending"][0]["status"] == "finding_saves"
    monkeypatch.setattr(automatic, "negotiate", lambda *_: pytest.fail("Must respect retry budget"))
    assert not automatic.plan(home, now=101)["ready"]


def test_duplicate_identities_are_not_silently_merged(tmp_path, monkeypatch):
    games = [shortcut(tmp_path / "a"), shortcut(tmp_path / "b", app="2147484001")]
    home = prepare(
        tmp_path,
        monkeypatch,
        games,
        {"Example Game": {"matched_title": "Example Game", "suggested_roots": ["/unused"]}},
    )
    result = automatic.plan(home)
    assert not result["ready"] and len(result["pending"]) == 2


def test_paused_game_is_not_automatically_reenabled(tmp_path, monkeypatch):
    item = shortcut(tmp_path)
    home = prepare(
        tmp_path,
        monkeypatch,
        [item],
        {item["title"]: {"matched_title": None, "suggested_roots": []}},
    )
    update_config(lambda cfg: cfg.update(paused_shortcuts=[automatic.shortcut_key(item)]), home)
    monkeypatch.setattr(
        automatic, "negotiate", lambda *_: pytest.fail("Paused games must be skipped")
    )
    assert automatic.plan(home) == {"ready": [], "pending": []}


def test_browser_signin_keeps_secret_local_and_polls_at_server_interval(tmp_path, monkeypatch):
    real = httpx.Client
    calls = []

    def handle(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/pair"):
            return httpx.Response(
                200,
                json={
                    "verification_uri": "https://example.test/pair",
                    "user_code": "1234-ABCD",
                    "device_code": "secret-only-on-this-device",
                    "expires_in": 600,
                },
            )
        if request.url.path.endswith("/token"):
            return httpx.Response(202, json={"status": "pending"})
        raise AssertionError(request.url.path)

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *args, **kwargs: real(*args, **{**kwargs, "transport": httpx.MockTransport(handle)}),
    )
    state = account.begin(tmp_path, "https://example.test")
    assert state["url"] == "https://example.test/pair?code=1234-ABCD"
    assert "secret-only-on-this-device" not in json.dumps(state)
    assert account.begin(tmp_path, "https://example.test") == state
    account.poll(tmp_path)
    account.poll(tmp_path)
    assert calls.count("/v1/devices/pair") == 1
    assert calls.count("/v1/devices/pair/token") == 1
    assert (tmp_path / "pairing.json").stat().st_mode & 0o777 == 0o600


def test_approved_browser_signin_validates_device_token_then_stores_account(tmp_path, monkeypatch):
    real = httpx.Client
    token = "nsd_" + "a" * 100

    def handle(request):
        if request.url.path.endswith("/pair"):
            return httpx.Response(
                200,
                json={
                    "verification_uri": "https://example.test/pair",
                    "user_code": "1234-ABCD",
                    "device_code": "secret",
                    "expires_in": 600,
                },
            )
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200, json={"access_token": token, "expires_at": time.time() + 3600}
            )
        if request.url.path == "/v1/me":
            assert request.headers["Authorization"] == "Bearer " + token
            return httpx.Response(200, json={"user_id": "user_test"})
        raise AssertionError(request.url.path)

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *args, **kwargs: real(*args, **{**kwargs, "transport": httpx.MockTransport(handle)}),
    )
    account.begin(tmp_path, "https://example.test")
    assert account.poll(tmp_path) == {"status": "connected"}
    assert configuration(tmp_path)["account_id"] == "user_test"
    assert not (tmp_path / "pairing.json").exists()


def test_signin_rejects_server_redirect_to_another_origin(tmp_path, monkeypatch):
    real = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *args, **kwargs: real(
            *args,
            **kwargs,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"verification_uri": "https://evil.test/pair"})
            ),
        ),
    )
    with pytest.raises(ValueError, match="destination"):
        account.begin(tmp_path, "https://example.test")
    assert not (tmp_path / "pairing.json").exists()
