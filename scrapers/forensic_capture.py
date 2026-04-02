"""Re-exports from canonical forensic capture module.

The canonical implementation lives in ag-complaint-pipeline/pipeline/shared/.
This file provides backwards-compatible imports for existing CFI code.

CFI-specific additions:
- _insert_documents(): writes R2 URLs into the Neon documents table
- capture_page() wraps the shared version with CFI defaults and document insertion
"""

import json
import logging
from pathlib import Path
from typing import Optional

import requests  # noqa: F401 — kept in namespace for test mocking compatibility

from pipeline.shared.forensic_capture import (
    append_custody_log,
    build_capture_metadata,
    compute_sha256,
    create_ots_proof,
    submit_to_wayback,
    url_to_slug,
    write_warc,
)

# Re-export all shared symbols so existing `from scrapers.forensic_capture import X` works
__all__ = [
    "capture_page",
    "compute_sha256",
    "build_capture_metadata",
    "append_custody_log",
    "url_to_slug",
    "write_warc",
    "create_ots_proof",
    "submit_to_wayback",
]

log = logging.getLogger("forensic_capture")


def _insert_documents(db_url: str, url: str, capture_ts: str, r2_urls: dict, capture_dir: Path) -> None:
    """Insert rows into the documents table for each evidence file uploaded to R2."""
    import psycopg

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

    CFI wrapper around the shared capture_page that:
    - Falls back to CaptureConfig() with CFI defaults if no config provided
    - Inserts document rows into the Neon documents table after R2 upload
    """
    from scrapers.config import CaptureConfig as CFICaptureConfig

    if config is None:
        config = CFICaptureConfig()

    # Use the shared implementation for the core capture logic
    from pipeline.shared.forensic_capture import capture_page as _shared_capture_page

    metadata = _shared_capture_page(
        url=url,
        html_content=html_content,
        screenshot_bytes=screenshot_bytes,
        page_title=page_title,
        response_headers=response_headers,
        status_code=status_code,
        config=config,
        raw_body=raw_body,
    )

    # CFI-specific: insert into documents table
    r2_urls = metadata.get("r2_urls", {})
    if config.db_url and r2_urls:
        slug = url_to_slug(url)
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        # Find the capture dir from the warc_path or screenshot_path in metadata
        screenshot_path = metadata.get("screenshot_path", "")
        if screenshot_path:
            capture_dir = Path(screenshot_path).parent
        else:
            capture_dir = config.captures_dir / f"{slug}_{ts}"
        _insert_documents(config.db_url, url, ts, r2_urls, capture_dir)

    return metadata
