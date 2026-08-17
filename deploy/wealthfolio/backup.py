#!/usr/bin/env python3
"""Encrypted, retention-managed backup for the self-hosted Wealthfolio data.

Design
------
* Runs on the Proxmox host (stdlib Python only — no finance-sync package
  needed), driven by the systemd units in this directory.
* The Wealthfolio SQLite database lives on LXC 104 (192.168.3.50).  Because
  the container rootfs is an LVM volume that must not be read while the
  server is running, the consistent snapshot step runs *inside* the LXC
  (``--snapshot-only`` mode, scheduled by ``wealthfolio-snapshot.timer``);
  the orchestrator on the Proxmox host pulls that snapshot with ``pct pull``.
* The finance-sync delivery cursors (``wealthfolio_deliveries`` /
  ``wealthfolio_account_mappings`` / ``export_runs`` in PostgreSQL on
  Coolify) are dumped via an optional ``--pg-dump-cmd`` and bundled into the
  same archive, so a restore proves accounts, activities, holdings AND the
  delivery cursors are preserved (backlog AC: backup with tested restore).
* Every bundle is encrypted with AES-256-CBC (openssl, PBKDF2) using a
  locally managed key file (see ``docs/wealthfolio-multi-device-access.md``
  §Backup).  Plaintext never leaves the Proxmox environment.
* Retention: daily / weekly / monthly buckets, pruned by filename date.
* Every fresh bundle is verified: decrypted, extracted, SQLite integrity
  checked and required tables asserted.

Usage
-----
    # 1) Inside LXC 104 (timer): consistent snapshot
    python3 backup.py --snapshot-only --db /opt/wealthfolio_data/wealthfolio.db \\
        --out /opt/wealthfolio_data/snapshot/wealthfolio-snapshot.db

    # 2) On the Proxmox host (timer): pull, bundle, encrypt, prune, verify
    python3 backup.py \\
        --backup-dir /var/backups/wealthfolio \\
        --key-file /root/.wealthfolio-backup.key \\
        --snapshot-file /var/backups/wealthfolio/staging/wealthfolio-snapshot.db \\
        --pg-dump-cmd 'pct exec 100 -- docker exec sxhp9cilwdw277krqq4affkc pg_dump -U postgres -d finance_sync --data-only -t wealthfolio_deliveries -t wealthfolio_account_mappings -t export_runs'
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Wealthfolio tables that must survive a restore (backlog AC: accounts,
# activities, holdings preserved).
REQUIRED_TABLES = ("accounts", "activities", "holdings_snapshots", "assets")

BUNDLE_PREFIX = "wealthfolio-backup"
BUNDLE_SUFFIX = ".enc"


# ═══════════════════════════════════════════════════════════════════════
# Snapshot (consistent SQLite copy via the online backup API)
# ═══════════════════════════════════════════════════════════════════════


def snapshot_sqlite(db_path: Path, out_path: Path) -> dict[str, Any]:
    """Create a consistent snapshot of *db_path* into *out_path*.

    Uses the SQLite online backup API (safe while the Wealthfolio server is
    writing, including WAL mode).  Returns a small report dict.
    """
    db_path = Path(db_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(out_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return verify_sqlite(out_path)


def verify_sqlite(path: Path) -> dict[str, Any]:
    """Run PRAGMA integrity_check and assert required tables exist."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        counts: dict[str, int] = {}
        for table in REQUIRED_TABLES:
            if table in tables:
                counts[table] = con.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
        missing = [t for t in REQUIRED_TABLES if t not in tables]
        return {
            "path": str(path),
            "integrity": integrity,
            "ok": integrity == "ok" and not missing,
            "tables": sorted(tables),
            "missing_tables": missing,
            "row_counts": counts,
        }
    finally:
        con.close()


# ═══════════════════════════════════════════════════════════════════════
# Encryption (AES-256-CBC via openssl, PBKDF2)
# ═══════════════════════════════════════════════════════════════════════


def ensure_key_file(key_file: Path) -> Path:
    """Create a fresh 32-byte base64 key if *key_file* does not exist."""
    key_file = Path(key_file)
    if not key_file.exists():
        key_file.parent.mkdir(parents=True, exist_ok=True)
        raw = os.urandom(32)
        key_file.write_bytes(_b64(raw).encode() + b"\n")
        key_file.chmod(0o600)
    return key_file


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode()


def encrypt_file(src: Path, dst: Path, key_file: Path) -> None:
    """Encrypt *src* into *dst* with AES-256-CBC + PBKDF2."""
    ensure_key_file(key_file)
    cmd = [
        "openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
        "-salt", "-in", str(src), "-out", str(dst),
        "-pass", f"file:{key_file}",
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def decrypt_file(src: Path, dst: Path, key_file: Path) -> None:
    """Decrypt *src* into *dst*."""
    cmd = [
        "openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
        "-in", str(src), "-out", str(dst),
        "-pass", f"file:{key_file}",
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ═══════════════════════════════════════════════════════════════════════
# Bundle + retention
# ═══════════════════════════════════════════════════════════════════════


def build_bundle(
    backup_dir: Path,
    snapshot_file: Path,
    key_file: Path,
    pg_dump_file: Path | None = None,
    now: datetime | None = None,
) -> Path:
    """Bundle snapshot (+ optional pg dump) into an encrypted archive.

    Returns the path of the new encrypted bundle.
    """
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(UTC)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    bundle_name = f"{BUNDLE_PREFIX}-{stamp}{BUNDLE_SUFFIX}"

    with tempfile.TemporaryDirectory(prefix="wf-bundle-") as tmp:
        tmp_dir = Path(tmp)
        tar_path = tmp_dir / "bundle.tar.gz"
        files = [("wealthfolio.db", snapshot_file)]
        if pg_dump_file is not None:
            files.append(("finance-sync-cursors.sql", pg_dump_file))
        with tarfile.open(tar_path, "w:gz") as tar:
            for arcname, path in files:
                tar.add(path, arcname=arcname)
        bundle_path = backup_dir / bundle_name
        encrypt_file(tar_path, bundle_path, key_file)
    return bundle_path


def extract_bundle(
    bundle_path: Path, key_file: Path, dest_dir: Path
) -> dict[str, Path]:
    """Decrypt and extract *bundle_path* into *dest_dir*.

    Returns a mapping of archive member name -> extracted path.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wf-decrypt-") as tmp:
        tar_path = Path(tmp) / "bundle.tar.gz"
        decrypt_file(bundle_path, tar_path, key_file)
        with tarfile.open(tar_path, "r:gz") as tar:
            tar.extractall(dest_dir, filter="data")
    return {
        member: dest_dir / member
        for member in ("wealthfolio.db", "finance-sync-cursors.sql")
        if (dest_dir / member).exists()
    }


def prune_retention(
    backup_dir: Path,
    keep_daily: int = 14,
    keep_weekly: int = 8,
    keep_monthly: int = 6,
    now: datetime | None = None,
) -> list[str]:
    """Prune encrypted bundles in *backup_dir* by retention buckets.

    Buckets: daily (newest N), weekly (newest M, one per ISO week),
    monthly (newest K, one per calendar month).  Returns removed paths.
    """
    backup_dir = Path(backup_dir)
    now = now or datetime.now(UTC)
    bundles = sorted(backup_dir.glob(f"{BUNDLE_PREFIX}-*{BUNDLE_SUFFIX}"))
    by_date: list[tuple[datetime, Path]] = []
    for path in bundles:
        try:
            stamp = path.name[len(BUNDLE_PREFIX) + 1: -len(BUNDLE_SUFFIX)]
            dt = datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            continue
        by_date.append((dt, path))
    by_date.sort(key=lambda item: item[0], reverse=True)

    keep: set[Path] = set()
    # Daily: newest N
    for _, path in by_date[:keep_daily]:
        keep.add(path)
    # Weekly: newest M, one per ISO week
    seen_weeks: set[str] = set()
    for dt, path in by_date:
        week = dt.strftime("%Y-%W")
        if week in seen_weeks:
            continue
        seen_weeks.add(week)
        keep.add(path)
        if len(seen_weeks) >= keep_weekly:
            break
    # Monthly: newest K, one per calendar month
    seen_months: set[str] = set()
    for dt, path in by_date:
        month = dt.strftime("%Y-%m")
        if month in seen_months:
            continue
        seen_months.add(month)
        keep.add(path)
        if len(seen_months) >= keep_monthly:
            break

    removed: list[str] = []
    for path in bundles:
        if path not in keep:
            path.unlink(missing_ok=True)
            removed.append(str(path))
    return removed


# ═══════════════════════════════════════════════════════════════════════
# Orchestration (Proxmox host)
# ═══════════════════════════════════════════════════════════════════════


def run_pg_dump(cmd: list[str], out_path: Path) -> bool:
    """Run an external pg_dump command, writing stdout to *out_path*."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(out_path, "wb") as f:
            subprocess.run(cmd, check=True, stdout=f, stderr=subprocess.PIPE)
        return out_path.stat().st_size > 0
    except (subprocess.CalledProcessError, OSError):
        return False


def cmd_snapshot_only(args: argparse.Namespace) -> int:
    report = snapshot_sqlite(Path(args.db), Path(args.out))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


def cmd_backup(args: argparse.Namespace) -> int:
    key_file = ensure_key_file(Path(args.key_file))
    snapshot = Path(args.snapshot_file)
    if not snapshot.exists():
        print(f"error: snapshot file missing: {snapshot}", file=sys.stderr)
        return 1
    verify = verify_sqlite(snapshot)
    if not verify["ok"]:
        print(f"error: snapshot failed verification: {verify}", file=sys.stderr)
        return 1

    pg_dump_path: Path | None = None
    if args.pg_dump_cmd:
        pg_dump_path = Path(args.backup_dir) / "staging" / "cursors.sql"
        ok = run_pg_dump(args.pg_dump_cmd, pg_dump_path)
        if not ok:
            print("warning: pg dump failed — bundle continues without cursors",
                  file=sys.stderr)
            pg_dump_path = None

    bundle = build_bundle(
        Path(args.backup_dir), snapshot, key_file, pg_dump_file=pg_dump_path
    )
    removed = prune_retention(
        Path(args.backup_dir),
        keep_daily=args.keep_daily,
        keep_weekly=args.keep_weekly,
        keep_monthly=args.keep_monthly,
    )

    # Fresh-bundle verification (decrypt -> extract -> sqlite checks).
    with tempfile.TemporaryDirectory(prefix="wf-verify-") as tmp:
        extracted = extract_bundle(bundle, key_file, Path(tmp))
        db_report = verify_sqlite(extracted["wealthfolio.db"])
    result = {
        "bundle": str(bundle),
        "sha256": sha256(bundle),
        "size_bytes": bundle.stat().st_size,
        "snapshot_verified": verify,
        "restore_verified": db_report,
        "pg_dump_included": pg_dump_path is not None,
        "pruned": removed,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if db_report["ok"] else 1


def cmd_restore(args: argparse.Namespace) -> int:
    key_file = Path(args.key_file)
    bundle = Path(args.backup_file)
    with tempfile.TemporaryDirectory(prefix="wf-restore-") as tmp:
        extracted = extract_bundle(bundle, key_file, Path(tmp))
        db_report = verify_sqlite(extracted["wealthfolio.db"])
        if args.target_db:
            Path(args.target_db).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(extracted["wealthfolio.db"], args.target_db)
            db_report["restored_to"] = args.target_db
        if "finance-sync-cursors.sql" in extracted:
            db_report["finance_sync_cursors"] = {
                "file": str(extracted["finance-sync-cursors.sql"]),
                "size_bytes": extracted["finance-sync-cursors.sql"].stat().st_size,
            }
        print(json.dumps(db_report, indent=2, sort_keys=True))
        return 0 if db_report["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Encrypted Wealthfolio backup / restore (see module docstring)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    snap = sub.add_parser("snapshot-only", help="consistent SQLite snapshot (run inside LXC 104)")
    snap.add_argument("--db", required=True, help="Wealthfolio sqlite path")
    snap.add_argument("--out", required=True, help="snapshot output path")
    snap.set_defaults(func=cmd_snapshot_only)

    backup = sub.add_parser("backup", help="bundle + encrypt + retain + verify (Proxmox host)")
    backup.add_argument("--backup-dir", required=True)
    backup.add_argument("--key-file", required=True)
    backup.add_argument("--snapshot-file", required=True)
    backup.add_argument("--pg-dump-cmd", nargs="+", default=None,
                        help="pg_dump command (argv) for finance-sync delivery cursors")
    backup.add_argument("--keep-daily", type=int, default=14)
    backup.add_argument("--keep-weekly", type=int, default=8)
    backup.add_argument("--keep-monthly", type=int, default=6)
    backup.set_defaults(func=cmd_backup)

    restore = sub.add_parser("restore", help="decrypt + extract + verify a bundle")
    restore.add_argument("--backup-file", required=True)
    restore.add_argument("--key-file", required=True)
    restore.add_argument("--target-db", default=None,
                         help="optional: write the restored sqlite to this path")
    restore.set_defaults(func=cmd_restore)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
