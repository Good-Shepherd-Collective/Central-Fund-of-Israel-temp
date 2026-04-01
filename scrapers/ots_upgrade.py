#!/usr/bin/env python3
"""
Upgrade pending OpenTimestamps proofs with Bitcoin block anchoring.

Run daily (or after 24 hours post-capture) to fetch completed Bitcoin
block proofs from OTS calendar servers. Proofs that are already complete
are skipped. Also uploads completed proofs to R2 for backup.

Usage:
    python -m scrapers.ots_upgrade                    # upgrade all pending
    python -m scrapers.ots_upgrade --check-only       # just report status
    python -m scrapers.ots_upgrade --dir path/to/ots  # specific directory

Designed for cron:
    0 6 * * * cd /path/to/repo && python -m scrapers.ots_upgrade >> logs/ots_upgrade.log 2>&1
"""

import argparse
import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("ots_upgrade")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

DEFAULT_OTS_DIR = "targets/cfi/web/ots"


def check_proof_status(ots_path: Path) -> str:
    """Check if an OTS proof is complete or pending.

    Returns: 'complete', 'pending', or 'error'
    """
    try:
        result = subprocess.run(
            ["ots", "verify", str(ots_path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and "Success" in result.stdout:
            return "complete"
        elif "Pending" in result.stderr or "waiting" in result.stderr:
            return "pending"
        else:
            return "pending"  # default to pending if unclear
    except Exception as e:
        log.warning(f"Error checking {ots_path.name}: {e}")
        return "error"


def upgrade_proof(ots_path: Path) -> bool:
    """Attempt to upgrade a pending OTS proof.

    Returns True if the proof was upgraded (or was already complete).
    """
    try:
        result = subprocess.run(
            ["ots", "upgrade", str(ots_path)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            log.info(f"Upgraded: {ots_path.name}")
            return True
        elif "already upgraded" in result.stderr.lower() or "already complete" in result.stderr.lower():
            log.info(f"Already complete: {ots_path.name}")
            return True
        else:
            # Still pending — not an error
            pending_info = result.stderr.strip().split("\n")[0] if result.stderr else "unknown"
            log.info(f"Still pending: {ots_path.name} ({pending_info})")
            return False
    except Exception as e:
        log.warning(f"Error upgrading {ots_path.name}: {e}")
        return False


def upload_completed_to_r2(ots_path: Path, entity_slug: str = "cfi") -> None:
    """Upload a completed OTS proof to R2 for backup."""
    try:
        from scrapers.r2_upload import R2Config, upload_file, build_r2_key

        config = R2Config()
        if not config.is_configured:
            return

        r2_key = build_r2_key(entity_slug, "web/ots", "", ots_path.name)
        upload_file(ots_path, r2_key, config, content_type="application/octet-stream")
    except Exception as e:
        log.warning(f"R2 upload failed for {ots_path.name}: {e}")


def main():
    parser = argparse.ArgumentParser(description="Upgrade pending OpenTimestamps proofs")
    parser.add_argument("--dir", default=DEFAULT_OTS_DIR, help="Directory containing .ots files")
    parser.add_argument("--check-only", action="store_true", help="Only report status, don't upgrade")
    parser.add_argument("--upload", action="store_true", help="Upload completed proofs to R2")
    args = parser.parse_args()

    ots_dir = Path(args.dir)
    if not ots_dir.exists():
        log.error(f"OTS directory not found: {ots_dir}")
        sys.exit(1)

    ots_files = sorted(ots_dir.glob("*.ots"))
    if not ots_files:
        log.info(f"No .ots files found in {ots_dir}")
        return

    log.info(f"Found {len(ots_files)} OTS proof files in {ots_dir}")

    stats = {"complete": 0, "pending": 0, "upgraded": 0, "error": 0}

    for ots_path in ots_files:
        if args.check_only:
            status = check_proof_status(ots_path)
            stats[status] = stats.get(status, 0) + 1
            log.info(f"  {ots_path.name}: {status}")
        else:
            upgraded = upgrade_proof(ots_path)
            if upgraded:
                stats["upgraded"] += 1
                if args.upload:
                    upload_completed_to_r2(ots_path)
            else:
                stats["pending"] += 1

    log.info(f"Results: {stats}")


if __name__ == "__main__":
    main()
