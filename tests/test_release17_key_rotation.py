"""Release 17 encryption-key rotation contracts."""

# pyright: basic

import json
from pathlib import Path

from cryptography.exceptions import InvalidTag

from scripts.key_rotation_drill import build_report, open_envelope, seal


def test_rotation_preserves_data_and_rejects_retired_key() -> None:
    report = build_report()
    assert report["states"] == ["current", "previous", "retired"]
    assert report["round_trip_preserved"] is True
    assert report["restart_worker_restore_preserved"] is True
    assert report["retired_key_rejected"] is True
    assert report["plaintext_export"] is False
    assert len(report["ciphertext_sha256"]) == 64


def test_old_key_is_only_valid_during_transition() -> None:
    plaintext = b"synthetic-credential-fixture"
    previous = bytes.fromhex("22" * 32)
    retired = bytes.fromhex("33" * 32)
    nonce, ciphertext = seal(plaintext, previous)
    assert open_envelope(nonce, ciphertext, previous) == plaintext
    try:
        open_envelope(nonce, ciphertext, retired)
    except InvalidTag:
        pass
    else:
        message = "retired key unexpectedly decrypted fixture"
        raise AssertionError(message)


def test_ci_runs_key_rotation_drill_without_plaintext_artifact() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    config = json.loads(Path("config/key-rotation-drill.json").read_text())
    assert "key-rotation:" in workflow
    assert "key_rotation_drill.py" in workflow
    assert config["plaintext_export"] is False
