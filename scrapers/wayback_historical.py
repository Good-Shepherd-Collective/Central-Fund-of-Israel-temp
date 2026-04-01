# scrapers/wayback_historical.py
"""
Fetch historical Wayback Machine snapshots for URLs.

Queries the CDX API for all available snapshots, then downloads
priority pages (donation, about, mission) and runs them through
the forensic capture pipeline for diff analysis.

Usage:
    python -m scrapers.wayback_historical https://www.centralfundofisrael.org/donate
    python -m scrapers.wayback_historical https://www.centralfundofisrael.org --all-pages
"""

import argparse
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("wayback_historical")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

CDX_API = "https://web.archive.org/cdx/search/cdx"


class _TextExtractor(HTMLParser):
    """Strip HTML tags and extract visible text."""
    def __init__(self):
        super().__init__()
        self.text_parts = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            self.text_parts.append(data.strip())


def _extract_text(html_bytes: bytes) -> str:
    """Extract visible text from HTML, stripping tags and scripts."""
    extractor = _TextExtractor()
    extractor.feed(html_bytes.decode("utf-8", errors="replace"))
    text = " ".join(p for p in extractor.text_parts if p)
    return re.sub(r"\s+", " ", text).strip()


def _sha256_hex(data: bytes) -> str:
    """Compute SHA-256 hex digest of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def fetch_snapshots(url: str, limit: int = 0, from_date: str = "", to_date: str = "") -> list[dict]:
    """Query Wayback CDX API for all snapshots of a URL.

    Returns list of dicts with: timestamp, original_url, status_code, digest, length.
    """
    params = {
        "url": url,
        "output": "json",
        "fl": "timestamp,original,statuscode,digest,length",
        "collapse": "timestamp:8",  # one per day
    }
    if limit:
        params["limit"] = limit
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date

    resp = requests.get(CDX_API, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if len(data) < 2:
        return []

    headers = data[0]
    return [dict(zip(headers, row)) for row in data[1:]]


def download_snapshot(url: str, timestamp: str, output_dir: Path) -> Optional[tuple[Path, bytes]]:
    """Download a specific Wayback snapshot and save it.

    Returns (filepath, raw_content_bytes) or None on failure.
    """
    wayback_url = f"https://web.archive.org/web/{timestamp}id_/{url}"
    try:
        resp = requests.get(wayback_url, timeout=30)
        if resp.status_code != 200:
            log.warning(f"HTTP {resp.status_code} for snapshot {timestamp}")
            return None

        output_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{timestamp}.html"
        filepath = output_dir / filename
        filepath.write_bytes(resp.content)

        log.info(f"Downloaded snapshot {timestamp} → {filepath}")
        return filepath, resp.content

    except Exception as e:
        log.warning(f"Failed to download snapshot {timestamp}: {e}")
        return None


def _parse_wayback_timestamp(ts: str) -> datetime:
    """Parse a Wayback timestamp (YYYYMMDDHHMMSS or YYYYMMDD) into a datetime."""
    ts = ts.ljust(14, "0")  # pad short timestamps
    return datetime.strptime(ts[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def collect_historical(
    url: str,
    output_dir: Path,
    max_snapshots: int = 50,
    delay: float = 1.0,
    db_url: str = "",
) -> dict:
    """Collect historical Wayback snapshots for a URL.

    Returns summary dict with snapshot metadata and file paths.
    """
    db_url = db_url or os.environ.get("DATABASE_URL_DEV", "")
    us_entity_id = os.environ.get("US_ENTITY_ID", "")

    log.info(f"Fetching CDX index for: {url}")
    snapshots = fetch_snapshots(url)
    total_available = len(snapshots)
    log.info(f"Found {total_available} snapshots")

    # Sample evenly if too many
    if len(snapshots) > max_snapshots:
        step = len(snapshots) // max_snapshots
        snapshots = snapshots[::step][:max_snapshots]
        log.info(f"Sampled down to {len(snapshots)} snapshots")

    # Open DB connection and create ingestion_log entry
    db_conn = None
    ingestion_id = None
    if db_url:
        import psycopg
        try:
            db_conn = psycopg.connect(db_url)
            row = db_conn.execute(
                """INSERT INTO ingestion_log (source_name, status, metadata)
                   VALUES ('wayback-historical', 'running', %s::jsonb)
                   RETURNING id""",
                (json.dumps({"url": url, "max_snapshots": max_snapshots, "total_available": total_available}),),
            ).fetchone()
            ingestion_id = str(row[0])
            db_conn.commit()
            log.info(f"Ingestion log created: {ingestion_id}")
        except Exception as e:
            log.error(f"Failed to create DB connection or ingestion log: {e}")
            db_conn = None

    results = []
    records_added = 0
    for snap in snapshots:
        ts = snap["timestamp"]
        download_result = download_snapshot(url, ts, output_dir / "snapshots")

        filepath = None
        raw_content = None
        if download_result is not None:
            filepath, raw_content = download_result

        result_entry = {
            **snap,
            "file_path": str(filepath) if filepath else None,
            "wayback_url": f"https://web.archive.org/web/{ts}/{url}",
        }

        # Database inserts for successfully downloaded snapshots
        if filepath and raw_content and db_conn:
            try:
                sha256 = _sha256_hex(raw_content)
                url_hash = hashlib.sha256(url.encode()).hexdigest()[:12]
                source_id = f"wayback:{url_hash}:{ts}"
                wayback_date = _parse_wayback_timestamp(ts)
                crawl_timestamp = wayback_date.isoformat()
                extracted_text = _extract_text(raw_content)[:50000]
                now_ts = datetime.now(timezone.utc).isoformat()

                result_entry["sha256"] = sha256

                # Insert into web_pages
                db_conn.execute(
                    """INSERT INTO web_pages (
                            source_id, source_url, us_entity_id,
                            is_wayback, wayback_date,
                            extracted_text, crawl_timestamp,
                            ingestion_id
                        ) VALUES (
                            %s, %s, %s::uuid,
                            true, %s::date,
                            %s, %s::timestamptz,
                            %s::uuid
                        )
                        ON CONFLICT DO NOTHING""",
                    (
                        source_id, url, us_entity_id,
                        wayback_date.strftime("%Y-%m-%d"),
                        extracted_text, crawl_timestamp,
                        ingestion_id,
                    ),
                )

                # Insert into documents
                db_conn.execute(
                    """INSERT INTO documents (
                            source_id, source_url, us_entity_id, document_type, file_path,
                            original_date, capture_date, file_size_bytes
                        ) VALUES (%s, %s, %s::uuid, %s, %s, %s::date, %s::timestamptz, %s)
                        ON CONFLICT DO NOTHING""",
                    (
                        f"wayback_doc:{url_hash}:{ts}",
                        url, us_entity_id, "wayback_snapshot", str(filepath),
                        wayback_date.strftime("%Y-%m-%d"), now_ts,
                        len(raw_content),
                    ),
                )

                db_conn.commit()
                records_added += 1
            except Exception as e:
                log.error(f"Failed to insert DB rows for snapshot {ts}: {e}")
                try:
                    db_conn.rollback()
                except Exception:
                    pass

        results.append(result_entry)
        time.sleep(delay)

    # Finalize ingestion log
    if db_conn:
        try:
            if ingestion_id:
                db_conn.execute(
                    """UPDATE ingestion_log SET
                            finished_at = now(),
                            records_added = %s,
                            status = 'success'
                       WHERE id = %s::uuid""",
                    (records_added, ingestion_id),
                )
            db_conn.commit()
        except Exception as e:
            log.error(f"Failed to finalize ingestion log: {e}")
        finally:
            db_conn.close()

    # Save index (reuse total_available instead of calling fetch_snapshots again)
    index = {
        "url": url,
        "total_snapshots_available": total_available,
        "snapshots_downloaded": len([r for r in results if r["file_path"]]),
        "date_range": f"{results[0]['timestamp'][:8]} - {results[-1]['timestamp'][:8]}" if results else "",
        "snapshots": results,
    }
    index_path = output_dir / "wayback_index.json"
    with open(index_path, "w") as f:
        json.dump(index, f, indent=2)
    log.info(f"Index written to {index_path}")

    return index


def main():
    parser = argparse.ArgumentParser(description="Collect Wayback Machine historical snapshots")
    parser.add_argument("url", help="URL to fetch history for")
    parser.add_argument("--max-snapshots", type=int, default=50)
    parser.add_argument("--output-dir", default="targets/cfi/web/wayback")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--db-url", default="", help="Database connection URL (overrides DATABASE_URL_DEV)")
    args = parser.parse_args()

    collect_historical(
        url=args.url,
        output_dir=Path(args.output_dir),
        max_snapshots=args.max_snapshots,
        delay=args.delay,
        db_url=args.db_url,
    )


if __name__ == "__main__":
    main()
