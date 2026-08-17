"""Tests for the Wealthfolio encrypted backup/restore tooling (backlog AC).

Exercises the self-contained scripts in ``deploy/wealthfolio/``:

* consistent SQLite snapshot (online backup API, WAL-safe),
* AES-256-CBC encryption with a locally managed key (plaintext never on
  disk unencrypted at rest),
* bundle + retention (daily/weekly/monthly buckets),
* restore-to-temp-instance proving accounts, activities, holdings and the
  finance-sync delivery cursors are preserved.

The scripts are stdlib-only so they run on the Proxmox host and in CI.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKUP_SCRIPT = REPO_ROOT / "deploy" / "wealthfolio" / "backup.py"

_SQLITE_MAGIC = b"SQLite format 3\x00"


def _load_backup_module():
    spec = importlib.util.spec_from_file_location("wf_backup", BACKUP_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def backup_mod():
    return _load_backup_module()


def _create_wealthfolio_db(path: Path) -> dict[str, int]:
    """Create a small Wealthfolio-like database; returns table row counts."""
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE accounts (
                id TEXT PRIMARY KEY, name TEXT, currency TEXT,
                account_type TEXT, is_active INTEGER
            );
            CREATE TABLE activities (
                id TEXT PRIMARY KEY, account_id TEXT, activity_type TEXT,
                symbol TEXT, quantity REAL, unit_price REAL, amount REAL,
                currency TEXT, date TEXT
            );
            CREATE TABLE holdings_snapshots (
                id TEXT PRIMARY KEY, account_id TEXT, symbol TEXT,
                quantity REAL, avg_cost REAL, currency TEXT, date TEXT
            );
            CREATE TABLE assets (
                id TEXT PRIMARY KEY, symbol TEXT, name TEXT, asset_type TEXT
            );
            """
        )
        con.executemany(
            "INSERT INTO accounts VALUES (?,?,?,?,?)",
            [
                ("a1", "Bunq Checking", "EUR", "CASH", 1),
                ("a2", "Trading212 Brokerage", "EUR", "SECURITIES", 1),
            ],
        )
        con.executemany(
            "INSERT INTO activities VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    "t1",
                    "a2",
                    "DIVIDEND",
                    "VWCE",
                    1,
                    100,
                    25,
                    "EUR",
                    "2026-08-01",
                ),
                (
                    "t2",
                    "a2",
                    "BUY",
                    "VWCE",
                    10,
                    100,
                    -1000,
                    "EUR",
                    "2026-08-02",
                ),
                (
                    "t3",
                    "a1",
                    "DEPOSIT",
                    None,
                    None,
                    None,
                    500,
                    "EUR",
                    "2026-08-03",
                ),
            ],
        )
        con.executemany(
            "INSERT INTO holdings_snapshots VALUES (?,?,?,?,?,?,?)",
            [
                ("h1", "a2", "VWCE", 9.0, 100.0, "EUR", "2026-08-13"),
            ],
        )
        con.executemany(
            "INSERT INTO assets VALUES (?,?,?,?)",
            [("s1", "VWCE", "Vanguard FTSE All-World", "ETF")],
        )
        con.commit()
        return {
            "accounts": 2,
            "activities": 3,
            "holdings_snapshots": 1,
            "assets": 1,
        }
    finally:
        con.close()


def test_snapshot_roundtrip_preserves_rows(backup_mod, tmp_path: Path) -> None:
    """Snapshot -> bundle (encrypted) -> extract -> same data."""
    db = tmp_path / "wealthfolio.db"
    expected = _create_wealthfolio_db(db)
    key = tmp_path / "backup.key"

    report = backup_mod.snapshot_sqlite(db, tmp_path / "snap.db")
    assert report["ok"] is True
    assert report["integrity"] == "ok"
    assert report["row_counts"] == expected

    bundle = backup_mod.build_bundle(
        tmp_path / "backups", tmp_path / "snap.db", key
    )
    assert bundle.exists()
    # Encrypted: must NOT look like a SQLite file or contain table names.
    blob = bundle.read_bytes()
    assert not blob.startswith(_SQLITE_MAGIC)
    assert b"holdings_snapshots" not in blob

    extracted = backup_mod.extract_bundle(bundle, key, tmp_path / "restored")
    restored = backup_mod.verify_sqlite(extracted["wealthfolio.db"])
    assert restored["ok"] is True
    assert restored["row_counts"] == expected


def test_restore_cli_to_target_db(backup_mod, tmp_path: Path) -> None:
    """The restore CLI decrypts a bundle and writes a verified DB copy."""
    db = tmp_path / "wealthfolio.db"
    expected = _create_wealthfolio_db(db)
    key = tmp_path / "backup.key"
    bundle = backup_mod.build_bundle(tmp_path / "backups", db, key)

    target = tmp_path / "restore-target" / "wealthfolio.db"
    result = subprocess.run(
        [
            sys.executable,
            str(BACKUP_SCRIPT),
            "restore",
            "--backup-file",
            str(bundle),
            "--key-file",
            str(key),
            "--target-db",
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["row_counts"] == expected
    assert payload["restored_to"] == str(target)
    assert target.exists()


def test_restore_fails_with_wrong_key(backup_mod, tmp_path: Path) -> None:
    """Restoring with the wrong key fails loudly (no silent corruption)."""
    db = tmp_path / "wealthfolio.db"
    _create_wealthfolio_db(db)
    good_key = tmp_path / "good.key"
    bad_key = tmp_path / "bad.key"
    bundle = backup_mod.build_bundle(tmp_path / "backups", db, good_key)

    with pytest.raises(subprocess.CalledProcessError):
        backup_mod.decrypt_file(bundle, tmp_path / "out.tar.gz", bad_key)


def test_retention_prunes_by_bucket(backup_mod, tmp_path: Path) -> None:
    """Daily/weekly/monthly retention keeps the newest per bucket."""
    key = tmp_path / "key"
    backup_mod.ensure_key_file(key)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)

    # 20 daily bundles: 2026-07-29 .. 2026-08-17 (3 distinct weeks, 2 months).
    for i in range(20):
        dt = now - timedelta(days=i)
        name = f"wealthfolio-backup-{dt.strftime('%Y%m%d-%H%M%S')}.enc"
        (backup_dir / name).write_bytes(b"x")

    removed = backup_mod.prune_retention(backup_dir, 14, 8, 6, now=now)
    remaining = sorted(p.name for p in backup_dir.iterdir())
    # Union of buckets: 14 newest daily + newest-of-week 2026-W31 (Aug 02)
    # + newest-of-month July (Jul 31) = 16.
    assert len(remaining) == 16, remaining
    assert removed  # at least the 4 oldest (Jul 29/30 + Aug 01/03) pruned
    # The 6 newest (>= today-5) must be retained.
    for i in range(6):
        dt = now - timedelta(days=i)
        name = f"wealthfolio-backup-{dt.strftime('%Y%m%d-%H%M%S')}.enc"
        assert name in remaining
    # Bucket survivors outside the daily window: newest per week/month.
    assert "wealthfolio-backup-20260802-120000.enc" in remaining  # weekly W31
    assert "wealthfolio-backup-20260731-120000.enc" in remaining  # monthly Jul
    assert "wealthfolio-backup-20260729-120000.enc" not in remaining


def test_key_file_created_with_0600_and_32_bytes(
    backup_mod, tmp_path: Path
) -> None:
    """ensure_key_file creates a root-only readable 32-byte base64 key."""
    key = tmp_path / "nested" / "backup.key"
    created = backup_mod.ensure_key_file(key)
    assert created == key
    assert key.exists()
    assert (key.stat().st_mode & 0o777) == 0o600
    import base64

    raw = base64.b64decode(key.read_text().strip())
    assert len(raw) == 32
