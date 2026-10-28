import json
import uuid

from nosteam_client import reporting
from nosteam_client.config import update_config
from nosteam_client.storage import atomic_json, capture, digest, read_json


def prepared(tmp_path):
    home, saves = tmp_path / "state", tmp_path / "saves"
    saves.mkdir()
    (saves / "slot.sav").write_text("private save progress")
    update_config(
        lambda cfg: cfg.update(
            account_id="user_fixture",
            server="https://example.test",
            token="private-credential",
            games={"celeste": {"title": "Celeste", "roots": [str(saves)]}},
        ),
        home,
    )
    atomic_json(
        home / "games/celeste/state.json",
        {
            "status": "synced",
            "base": str(uuid.uuid4()),
            "digest": digest(capture([saves])),
        },
    )
    return home, saves


def test_reports_observe_local_changes_without_writing_saves_or_exposing_paths(tmp_path):
    home, saves = prepared(tmp_path)
    before = (saves / "slot.sav").read_bytes()
    clean = reporting.observations(home)
    assert clean[0]["dirty"] is False
    assert "private save progress" not in json.dumps(clean)
    assert "private-credential" not in json.dumps(clean)
    assert str(saves) not in json.dumps(clean)
    assert (saves / "slot.sav").read_bytes() == before
    (saves / "slot.sav").write_text("new offline progress")
    assert reporting.observations(home)[0]["dirty"] is True
    assert (saves / "slot.sav").read_text() == "new offline progress"


def test_playing_recovery_and_exhausted_scan_budget_never_report_a_clean_save(
    tmp_path, monkeypatch
):
    home, _ = prepared(tmp_path)
    monkeypatch.setattr(
        reporting, "capture", lambda _: (_ for _ in ()).throw(AssertionError("must not scan"))
    )
    assert reporting.observations(home, budget=0)[0]["dirty"] is None
    atomic_json(home / "games/celeste/state.json", {"status": "playing"})
    row = reporting.observations(home)[0]
    assert row["dirty"] is None and row["status"] == "playing"
    atomic_json(home / "games/celeste/restore.json", {"before": {}})
    assert reporting.observations(home)[0]["status"] == "recovering"


def test_concurrent_game_session_invalidates_observation_without_taking_session_lock(
    tmp_path, monkeypatch
):
    home, _ = prepared(tmp_path)
    original = capture
    calls = []

    def changed(roots):
        result = original(roots)
        calls.append(True)
        if len(calls) == 1:
            atomic_json(home / "games/celeste/state.json", {"status": "playing"})
        return result

    monkeypatch.setattr(reporting, "capture", changed)
    assert reporting.observations(home)[0]["dirty"] is None
    assert not (home / "games/celeste/session.lock").exists()


def test_publisher_recovers_sequence_and_scopes_cached_status_to_account(tmp_path, monkeypatch):
    home, _ = prepared(tmp_path)
    sent = []

    class Remote:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path, **kwargs):
            if method == "POST":
                sent.append(kwargs["json"])
                return {"accepted": False, "sequence": 50}
            return {"devices": [], "games": [], "checked_at": 1000}

        def close(self):
            pass

    monkeypatch.setattr(reporting, "Remote", Remote)
    reporting.publish(home)
    assert sent[0]["sequence"] == 1
    assert read_json(home / "report-sequence.json")["sequence"] == 50
    reporting.publish(home)
    assert sent[1]["sequence"] == 51
    assert reporting.cached_status(home)["checked_at"] == 1000
    update_config(lambda cfg: cfg.update(account_id="user_other"), home)
    assert reporting.cached_status(home) is None
