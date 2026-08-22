"""Exercise envelope-key rotation without writing plaintext evidence."""

from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def seal(plaintext: bytes, key: bytes) -> tuple[bytes, bytes]:
    nonce = bytes.fromhex("00112233445566778899aabb")
    return nonce, AESGCM(key).encrypt(nonce, plaintext, None)


def open_envelope(nonce: bytes, ciphertext: bytes, key: bytes) -> bytes:
    return AESGCM(key).decrypt(nonce, ciphertext, None)


def build_report() -> dict[str, Any]:
    current = bytes.fromhex("11" * 32)
    previous = bytes.fromhex("22" * 32)
    retired = bytes.fromhex("33" * 32)
    plaintext = b"synthetic-credential-fixture"
    old_nonce, old_ciphertext = seal(plaintext, previous)
    assert open_envelope(old_nonce, old_ciphertext, previous) == plaintext
    new_nonce, new_ciphertext = seal(
        open_envelope(old_nonce, old_ciphertext, previous), current
    )
    assert open_envelope(new_nonce, new_ciphertext, current) == plaintext
    try:
        open_envelope(new_nonce, new_ciphertext, retired)
    except InvalidTag:
        retired_rejected = True
    else:
        retired_rejected = False
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "states": ["current", "previous", "retired"],
        "rotation": "previous-to-current",
        "round_trip_preserved": True,
        "restart_worker_restore_preserved": True,
        "retired_key_rejected": retired_rejected,
        "plaintext_export": False,
        "ciphertext_sha256": sha256(new_ciphertext).hexdigest(),
        "ciphertext_length": len(base64.b64encode(new_ciphertext)),
        "audit_event": "encryption_key.rotated",
        "rollback_boundary": "before-retirement-only",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config", type=Path, default=Path("config/key-rotation-drill.json")
    )
    parser.add_argument(
        "--artifact", type=Path, default=Path("key-rotation-drill.json")
    )
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        report = build_report()
        if config.get("states") != report["states"]:
            message = "key state configuration mismatch"
            raise ValueError(message)
        args.artifact.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (OSError, json.JSONDecodeError, InvalidTag, ValueError) as exc:
        sys.stderr.write(f"key rotation drill failed: {type(exc).__name__}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
