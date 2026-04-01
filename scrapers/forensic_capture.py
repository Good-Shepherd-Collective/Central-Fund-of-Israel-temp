# scrapers/forensic_capture.py
"""
Forensic web capture engine — evidentiary-grade page archiving.

Produces per-URL capture packages meeting FRE 901(a) authentication standards:
- SHA-256 cryptographic hashes (integrity)
- WARC archival format (completeness)
- Full-page screenshots (visual record)
- Wayback Machine submission (third-party neutral archive)
- OpenTimestamps proofs (Bitcoin-anchored capture time)
- Chain-of-custody log entries (provenance)
"""

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger("forensic_capture")


def compute_sha256(content: bytes) -> str:
    """Compute SHA-256 hex digest of content."""
    return hashlib.sha256(content).hexdigest()


def build_capture_metadata(
    url: str,
    sha256_raw: str,
    sha256_rendered: str,
    operator: str = "automated-pipeline",
    tool_version: str = "1.0.0",
    proxy_ip: str = "",
    wayback_url: str = "",
    ots_path: str = "",
    warc_path: str = "",
    screenshot_path: str = "",
    page_title: str = "",
    sha256_screenshot: str = "",
    raw_captured: bool = False,
) -> dict:
    """Build the chain-of-custody metadata record for a capture."""
    return {
        "capture_id": str(uuid.uuid4()),
        "url": url,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "operator": operator,
        "tool": f"playwright/{tool_version}",
        "tool_version": tool_version,
        "proxy_ip": proxy_ip,
        "sha256_raw": sha256_raw,
        "sha256_rendered": sha256_rendered,
        "sha256_screenshot": sha256_screenshot,
        "raw_captured": raw_captured,
        "wayback_url": wayback_url,
        "ots_path": ots_path,
        "warc_path": warc_path,
        "screenshot_path": screenshot_path,
        "page_title": page_title,
    }


def append_custody_log(log_path: Path, entry: dict) -> None:
    """Append a capture entry to the chain-of-custody JSONL log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def url_to_slug(url: str) -> str:
    """Convert URL to filesystem-safe slug for directory naming."""
    import re
    from urllib.parse import urlparse

    parsed = urlparse(url)
    path = parsed.path.strip("/").replace("/", "_") or "index"
    slug = re.sub(r"[^a-zA-Z0-9_\-]", "_", path)
    return slug[:100]  # cap length


def write_warc(
    url: str,
    status_code: int,
    headers: dict,
    body: bytes,
    output_dir: Path,
    filename: str = "",
) -> Path:
    """Write an HTTP response as a WARC record.

    Uses warcio to produce a gzipped WARC file containing the response.
    This is the archival standard used by the Internet Archive.
    """
    from io import BytesIO

    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter

    if not filename:
        filename = f"{url_to_slug(url)}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.warc.gz"

    output_dir.mkdir(parents=True, exist_ok=True)
    warc_path = output_dir / filename

    with open(warc_path, "wb") as fh:
        writer = WARCWriter(fh, gzip=True)

        # Build HTTP status line + headers
        http_headers = StatusAndHeaders(
            f"{status_code} OK",
            list(headers.items()),
            protocol="HTTP/1.1",
        )

        record = writer.create_warc_record(
            url,
            "response",
            payload=BytesIO(body),
            http_headers=http_headers,
        )
        writer.write_record(record)

    return warc_path


def create_ots_proof(
    sha256_hex: str,
    output_dir: Path,
    filename: str = "",
    calendar_urls: list[str] = None,
) -> Optional[Path]:
    """Create an OpenTimestamps proof for a SHA-256 hash.

    Submits the hash to OTS calendar servers which aggregate into a Merkle tree
    and anchor to a Bitcoin transaction. The .ots file is the cryptographic proof.

    The proof starts as a "pending" calendar commitment (immediate).
    Full Bitcoin anchoring takes 6-24 hours. Run `ots upgrade <file.ots>`
    later to fetch the completed proof with the Bitcoin block header path.
    """
    try:
        import opentimestamps.core.timestamp as ots_timestamp
        from opentimestamps.core.op import OpSHA256
        from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp
        from opentimestamps.core.notary import PendingAttestation
    except ImportError:
        log.warning("opentimestamps not installed — skipping OTS proof")
        return None

    if not filename:
        filename = f"{sha256_hex[:16]}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.ots"

    output_dir.mkdir(parents=True, exist_ok=True)
    ots_path = output_dir / filename

    # Create detached timestamp from hash digest
    hash_bytes = bytes.fromhex(sha256_hex)
    timestamp = Timestamp(hash_bytes)

    if calendar_urls is None:
        calendar_urls = [
            "https://a.pool.opentimestamps.org",
            "https://b.pool.opentimestamps.org",
            "https://finney.calendar.eternitywall.com",
        ]

    # Submit to calendar servers
    import urllib.request

    submitted = False
    for cal_url in calendar_urls:
        try:
            req = urllib.request.Request(
                f"{cal_url}/digest",
                data=hash_bytes,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp = urllib.request.urlopen(req, timeout=10)
            if resp.status == 200:
                commitment = resp.read()
                # Parse the calendar's timestamp response
                import opentimestamps.core.serialize as ots_serialize
                from io import BytesIO

                ctx = ots_serialize.StreamDeserializationContext(BytesIO(commitment))
                new_timestamp = Timestamp.deserialize(ctx, hash_bytes)
                timestamp.merge(new_timestamp)
                submitted = True
                log.info(f"OTS: submitted to {cal_url}")
        except Exception as e:
            log.warning(f"OTS: failed to submit to {cal_url}: {e}")

    if not submitted:
        log.warning("OTS: no calendar accepted the timestamp — saving local proof only")

    # Write the .ots file
    detached = DetachedTimestampFile(OpSHA256(), timestamp)
    with open(ots_path, "wb") as f:
        import opentimestamps.core.serialize as ots_serialize
        ctx = ots_serialize.StreamSerializationContext(f)
        detached.serialize(ctx)

    return ots_path


def submit_to_wayback(url: str, timeout: int = 30) -> Optional[str]:
    """Submit a URL to the Wayback Machine Save Page Now.

    Uses the simple GET-based SPN endpoint. For high-volume use,
    upgrade to SPN2 with IA API keys (POST-based, async).

    Returns the archived URL or None on failure.
    """
    try:
        save_url = f"https://web.archive.org/save/{url}"
        resp = requests.get(save_url, timeout=timeout, allow_redirects=True)

        if resp.status_code == 200:
            # The Content-Location header or final URL contains the archived path
            archived = resp.headers.get("Content-Location", "")
            if archived:
                wayback_url = f"https://web.archive.org{archived}"
            else:
                wayback_url = resp.url
            log.info(f"Wayback: archived {url} → {wayback_url}")
            return wayback_url
        else:
            log.warning(f"Wayback: status {resp.status_code} for {url}")
            return None
    except Exception as e:
        log.warning(f"Wayback: failed for {url}: {e}")
        return None


def _insert_documents(db_url: str, url: str, capture_ts: str, r2_urls: dict, capture_dir: Path) -> None:
    """Insert rows into the documents table for each evidence file uploaded to R2."""
    import psycopg

    # Map filenames to document_type
    type_map = {
        "screenshot.png": "website_screenshot",
        "rendered.html": "website_html",
    }

    try:
        conn = psycopg.connect(db_url)
        for filename, r2_url in r2_urls.items():
            if filename in type_map:
                doc_type = type_map[filename]
            elif filename.endswith(".warc.gz"):
                doc_type = "other"
            else:
                continue

            # Get file size from local capture dir
            local_file = capture_dir / filename
            file_size = local_file.stat().st_size if local_file.exists() else None

            conn.execute(
                """INSERT INTO documents (
                        source_url, document_type, file_path,
                        capture_date, file_size
                    ) VALUES (%s, %s, %s, %s::timestamptz, %s)
                    ON CONFLICT DO NOTHING""",
                (url, doc_type, r2_url, capture_ts, file_size),
            )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"Failed to insert documents for {url}: {e}")


def capture_page(
    url: str,
    html_content: bytes,
    screenshot_bytes: bytes,
    page_title: str = "",
    response_headers: dict = None,
    status_code: int = 200,
    config=None,
    raw_body: Optional[bytes] = None,
) -> dict:
    """Capture a single page with full forensic evidence chain.

    This is the main entry point. The caller (website_crawl.py) provides
    the rendered HTML and screenshot from Playwright. This function handles
    hashing, WARC writing, Wayback submission, OTS timestamps, and custody logging.

    Args:
        url: The URL that was captured
        html_content: Rendered HTML bytes (after JS execution)
        screenshot_bytes: Full-page screenshot PNG bytes
        page_title: Page title from Playwright
        response_headers: HTTP response headers (for WARC)
        status_code: HTTP status code
        config: CaptureConfig instance

    Returns:
        Capture metadata dict (also appended to chain_of_custody.jsonl)
    """
    from scrapers.config import CaptureConfig

    if config is None:
        config = CaptureConfig()
    config.ensure_dirs()

    slug = url_to_slug(url)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    capture_dir = config.captures_dir / f"{slug}_{ts}"
    capture_dir.mkdir(parents=True, exist_ok=True)

    # 1. SHA-256 hashes
    sha256_rendered = compute_sha256(html_content)
    sha256_raw = compute_sha256(raw_body) if raw_body is not None else sha256_rendered
    raw_captured = raw_body is not None
    sha256_screenshot = compute_sha256(screenshot_bytes)

    # 2. Save rendered HTML
    html_path = capture_dir / "rendered.html"
    html_path.write_bytes(html_content)

    # 3. Save screenshot
    screenshot_path = capture_dir / "screenshot.png"
    screenshot_path.write_bytes(screenshot_bytes)

    # 4. Write WARC
    warc_path = None
    if config.save_warc:
        warc_path = write_warc(
            url=url,
            status_code=status_code,
            headers=response_headers or {"Content-Type": "text/html"},
            body=html_content,
            output_dir=capture_dir,
            filename=f"{slug}.warc.gz",
        )

    # 5. Submit to Wayback Machine
    wayback_url = ""
    if config.submit_wayback:
        wayback_url = submit_to_wayback(url) or ""

    # 6. Create OpenTimestamps proof
    ots_path = None
    if config.create_ots:
        ots_path = create_ots_proof(
            sha256_rendered,
            output_dir=config.ots_dir,
            filename=f"{slug}_{ts}.ots",
        )

    # 7. Build metadata
    metadata = build_capture_metadata(
        url=url,
        sha256_raw=sha256_raw,
        sha256_rendered=sha256_rendered,
        operator=config.operator,
        tool_version=config.tool_version,
        proxy_ip=config.proxy_url,
        wayback_url=wayback_url,
        ots_path=str(ots_path) if ots_path else "",
        warc_path=str(warc_path) if warc_path else "",
        screenshot_path=str(screenshot_path),
        page_title=page_title,
        sha256_screenshot=sha256_screenshot,
        raw_captured=raw_captured,
    )

    # 7b. Upload to R2
    r2_urls = {}
    if config.upload_to_r2:
        from scrapers.r2_upload import upload_capture_dir
        r2_urls = upload_capture_dir(capture_dir, config.entity_slug, config.r2_config)
        metadata["r2_urls"] = r2_urls

    # 7c. Insert into documents table
    if config.db_url and r2_urls:
        _insert_documents(config.db_url, url, ts, r2_urls, capture_dir)

    # Save metadata alongside capture
    meta_path = capture_dir / "metadata.json"
    meta_path.write_text(json.dumps(metadata, indent=2, default=str))

    # 8. Append to chain of custody log
    custody_log = config.output_dir / "chain_of_custody.jsonl"
    append_custody_log(custody_log, metadata)

    log.info(f"Captured {url} → {capture_dir.name} (sha256: {sha256_rendered[:16]}...)")
    return metadata
