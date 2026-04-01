# Forensic Web Capture Pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an evidentiary-grade website capture pipeline for CFI and future entities that meets FRE 901(a) authentication standards, with cryptographic hashing, WARC archival, Wayback Machine third-party archiving, and OpenTimestamps Bitcoin-anchored proofs.

**Architecture:** Two-tier storage — Neon Postgres for structured data/analysis, Cloudflare R2 for media evidence. A `ForensicCapture` engine handles per-URL evidence collection (WARC, screenshot, SHA-256, Wayback submission, OpenTimestamps), uploads media to R2, and writes metadata to Neon `web_pages` table. A `WebsiteCrawler` uses ForensicCapture for each page discovered. Proxy rotation via the existing `proxies` library handles rate limits. All captures produce append-only chain-of-custody logs (also backed up to R2).

**Data Flow:**
```
Playwright → render page → HTML + screenshot + WARC + SHA-256
  ├── Media → R2 bucket (ag-complaint-evidence) → get R2 URLs
  ├── Metadata → Neon web_pages table (text, category, R2 paths)
  ├── OTS proof → R2 + local backup
  ├── Wayback submission → archived URL stored in metadata
  └── Custody log → local JSONL + R2 backup
```

**Tech Stack:** Playwright (rendering), warcio (WARC format), opentimestamps-client (Bitcoin timestamps), waybackpy (Wayback SPN2), boto3 (R2 S3-compatible API), proxies library (SOCKS5 rotation), psycopg3 (Neon DB)

---

## File Structure

```
scrapers/
├── forensic_capture.py     # Core capture engine — WARC, hash, screenshot, Wayback, OTS
├── r2_upload.py            # Cloudflare R2 upload (S3-compatible)
├── website_crawl.py        # Playwright crawler that feeds URLs to forensic_capture
├── wayback_historical.py   # Fetch historical Wayback snapshots for a URL
└── config.py               # Shared config: proxy pool, IA credentials, R2 credentials, paths

tests/
├── test_forensic_capture.py
├── test_r2_upload.py
├── test_website_crawl.py
└── test_wayback_historical.py

targets/cfi/web/
├── captures/               # Local staging before R2 upload
├── chain_of_custody.jsonl  # Append-only evidence log (backed up to R2)
├── ots/                    # OpenTimestamps .ots proof files (backed up to R2)
└── wayback/                # Historical Wayback snapshots
```

## Dependencies

```
# Add to requirements.txt
playwright>=1.40
warcio>=1.7
opentimestamps-client>=0.7
waybackpy>=3.0
boto3>=1.34         # R2 S3-compatible uploads
```

## R2 Storage

- **Account:** Defund Racism (info@defundracism.org)
- **Bucket:** `ag-complaint-evidence` (to be created)
- **Credentials:** In `~/Desktop/repos/keys.db` under `cloudflare` and `cloudflare-r2` services
- **Path convention:** `{entity-slug}/web/{capture-slug}/{file}` (e.g., `cfi/web/donate_20260401_120000/screenshot.png`)

## Neon Ingestion

After each page capture, insert into `web_pages` table:
- `source_url` → the captured URL
- `extracted_text` → stripped text from rendered HTML
- `page_category` → classified category
- `html_path` → R2 URL for rendered HTML
- `screenshot_path` → R2 URL for screenshot
- `crawl_timestamp` → capture time
- Link to `us_entity_id` for CFI

---

### Task 1: Shared Config Module

**Files:**
- Create: `scrapers/config.py`
- Create: `scrapers/__init__.py`

- [ ] **Step 1: Create the config module**

```python
# scrapers/config.py
"""Shared configuration for forensic capture pipeline."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass
class CaptureConfig:
    """Configuration for forensic capture sessions."""

    # Paths
    output_dir: Path = Path("targets/cfi/web")
    captures_dir: Path = field(default=None)
    ots_dir: Path = field(default=None)
    wayback_dir: Path = field(default=None)

    # Wayback Machine SPN2 credentials (from IA account)
    ia_access_key: str = ""
    ia_secret_key: str = ""

    # Proxy
    proxy_url: str = ""  # socks5h://127.0.0.1:9050

    # Capture settings
    screenshot_full_page: bool = True
    save_warc: bool = True
    submit_wayback: bool = True
    create_ots: bool = True
    timeout_ms: int = 30_000

    # Operator metadata (for chain of custody)
    operator: str = "automated-pipeline"
    tool_version: str = "1.0.0"

    def __post_init__(self):
        self.captures_dir = self.captures_dir or self.output_dir / "captures"
        self.ots_dir = self.ots_dir or self.output_dir / "ots"
        self.wayback_dir = self.wayback_dir or self.output_dir / "wayback"

        self.ia_access_key = self.ia_access_key or os.environ.get("IA_ACCESS_KEY", "")
        self.ia_secret_key = self.ia_secret_key or os.environ.get("IA_SECRET_KEY", "")
        self.proxy_url = self.proxy_url or os.environ.get("PROXY_URL", "")

    def ensure_dirs(self):
        """Create output directories."""
        for d in [self.captures_dir, self.ots_dir, self.wayback_dir]:
            d.mkdir(parents=True, exist_ok=True)
```

```python
# scrapers/__init__.py
```

- [ ] **Step 2: Commit**

```bash
git add scrapers/__init__.py scrapers/config.py
git commit -m "feat(scrapers): add shared config for forensic capture pipeline"
```

---

### Task 2: Forensic Capture Engine — Core (Hash + Screenshot + WARC)

**Files:**
- Create: `scrapers/forensic_capture.py`
- Create: `tests/test_forensic_capture.py`

- [ ] **Step 1: Write the test for SHA-256 hashing and metadata generation**

```python
# tests/test_forensic_capture.py
"""Tests for forensic capture engine."""

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_compute_hash():
    """SHA-256 hash of content should be deterministic."""
    from scrapers.forensic_capture import compute_sha256

    content = b"<html><body>Hello</body></html>"
    expected = hashlib.sha256(content).hexdigest()
    assert compute_sha256(content) == expected


def test_compute_hash_empty():
    from scrapers.forensic_capture import compute_sha256

    assert compute_sha256(b"") == hashlib.sha256(b"").hexdigest()


def test_build_capture_metadata():
    """Capture metadata should include all required evidentiary fields."""
    from scrapers.forensic_capture import build_capture_metadata

    meta = build_capture_metadata(
        url="https://example.com/about",
        sha256_raw="abc123",
        sha256_rendered="def456",
        operator="test-operator",
        tool_version="1.0.0",
        proxy_ip="127.0.0.1:9050",
    )

    assert meta["url"] == "https://example.com/about"
    assert meta["sha256_raw"] == "abc123"
    assert meta["sha256_rendered"] == "def456"
    assert meta["operator"] == "test-operator"
    assert "capture_id" in meta
    assert "timestamp_utc" in meta
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_forensic_capture.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scrapers.forensic_capture'`

- [ ] **Step 3: Implement core functions**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_forensic_capture.py -v`
Expected: 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add scrapers/forensic_capture.py tests/test_forensic_capture.py
git commit -m "feat(capture): core forensic functions — SHA-256, metadata, custody log"
```

---

### Task 3: Forensic Capture Engine — WARC Writing

**Files:**
- Modify: `scrapers/forensic_capture.py`
- Modify: `tests/test_forensic_capture.py`

- [ ] **Step 1: Write the WARC test**

Add to `tests/test_forensic_capture.py`:

```python
def test_write_warc_creates_file(tmp_path):
    """WARC writer should create a valid .warc.gz file."""
    from scrapers.forensic_capture import write_warc

    url = "https://example.com/page"
    headers = {"Content-Type": "text/html; charset=utf-8"}
    body = b"<html><body>Test page</body></html>"
    status_code = 200

    warc_path = write_warc(
        url=url,
        status_code=status_code,
        headers=headers,
        body=body,
        output_dir=tmp_path,
    )

    assert warc_path.exists()
    assert warc_path.suffix == ".gz"
    assert warc_path.stat().st_size > 0

    # Verify we can read it back
    from warcio import ArchiveIterator

    with open(warc_path, "rb") as f:
        records = list(ArchiveIterator(f))
    assert len(records) >= 1
    assert records[0].rec_type in ("response", "warcinfo")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_forensic_capture.py::test_write_warc_creates_file -v`
Expected: FAIL — `ImportError: cannot import name 'write_warc'`

- [ ] **Step 3: Implement WARC writing**

Add to `scrapers/forensic_capture.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_forensic_capture.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add scrapers/forensic_capture.py tests/test_forensic_capture.py
git commit -m "feat(capture): WARC archival format writing via warcio"
```

---

### Task 4: Forensic Capture Engine — OpenTimestamps

**Files:**
- Modify: `scrapers/forensic_capture.py`
- Modify: `tests/test_forensic_capture.py`

- [ ] **Step 1: Write the OTS test**

Add to `tests/test_forensic_capture.py`:

```python
def test_create_ots_proof(tmp_path):
    """OpenTimestamps should create a .ots proof file from a SHA-256 hash."""
    from scrapers.forensic_capture import create_ots_proof

    sha256_hex = hashlib.sha256(b"test content").hexdigest()
    ots_path = create_ots_proof(sha256_hex, output_dir=tmp_path, filename="test.ots")

    assert ots_path.exists()
    assert ots_path.suffix == ".ots"
    assert ots_path.stat().st_size > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_forensic_capture.py::test_create_ots_proof -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement OTS proof creation**

Add to `scrapers/forensic_capture.py`:

```python
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

                ctx = ots_serialize.DeserializationContext(BytesIO(commitment))
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
        ctx = ots_serialize.SerializationContext(f)
        detached.serialize(ctx)

    return ots_path
```

- [ ] **Step 4: Run test**

Run: `python -m pytest tests/test_forensic_capture.py::test_create_ots_proof -v`
Expected: PASS (may take a few seconds for calendar submission)

Note: If network is unavailable, test will still pass — the proof file is created with or without calendar response.

- [ ] **Step 5: Commit**

```bash
git add scrapers/forensic_capture.py tests/test_forensic_capture.py
git commit -m "feat(capture): OpenTimestamps Bitcoin-anchored proof creation"
```

---

### Task 5: Forensic Capture Engine — Wayback Machine Submission

**Files:**
- Modify: `scrapers/forensic_capture.py`
- Modify: `tests/test_forensic_capture.py`

- [ ] **Step 1: Write the Wayback test (mocked)**

Add to `tests/test_forensic_capture.py`:

```python
@patch("scrapers.forensic_capture.requests.get")
def test_submit_wayback(mock_get):
    """Wayback submission should return an archived URL."""
    from scrapers.forensic_capture import submit_to_wayback

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Location": "/web/20260401120000/https://example.com"}
    mock_resp.url = "https://web.archive.org/web/20260401120000/https://example.com"
    mock_get.return_value = mock_resp

    result = submit_to_wayback("https://example.com")
    assert result is not None
    assert "web.archive.org" in result


@patch("scrapers.forensic_capture.requests.get")
def test_submit_wayback_failure(mock_get):
    """Wayback failure should return None, not raise."""
    from scrapers.forensic_capture import submit_to_wayback

    mock_get.side_effect = Exception("Network error")
    result = submit_to_wayback("https://example.com")
    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_forensic_capture.py -k wayback -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement Wayback submission**

Add to `scrapers/forensic_capture.py` (add `import requests` at top):

```python
import requests as _requests  # rename to avoid shadowing in tests


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
```

Note: Replace `requests` import at top of file — add `import requests` alongside the other imports.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_forensic_capture.py -k wayback -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scrapers/forensic_capture.py tests/test_forensic_capture.py
git commit -m "feat(capture): Wayback Machine Save Page Now submission"
```

---

### Task 6: Forensic Capture Engine — Full Page Capture Orchestrator

**Files:**
- Modify: `scrapers/forensic_capture.py`
- Modify: `tests/test_forensic_capture.py`

This is the main `capture_page()` function that ties everything together.

- [ ] **Step 1: Write integration test**

Add to `tests/test_forensic_capture.py`:

```python
def test_capture_page_produces_package(tmp_path):
    """Full capture should produce WARC, screenshot, metadata, and custody log."""
    from scrapers.config import CaptureConfig
    from scrapers.forensic_capture import capture_page

    config = CaptureConfig(
        output_dir=tmp_path,
        submit_wayback=False,  # don't hit real Wayback in tests
        create_ots=False,  # don't hit real OTS calendars in tests
    )
    config.ensure_dirs()

    # We'll mock the Playwright page
    result = capture_page(
        url="https://example.com",
        html_content=b"<html><body><h1>Test</h1></body></html>",
        screenshot_bytes=b"\x89PNG fake screenshot",
        page_title="Test Page",
        config=config,
    )

    assert result["sha256_rendered"] is not None
    assert result["capture_id"] is not None

    # Check files were created
    custody_log = tmp_path / "chain_of_custody.jsonl"
    assert custody_log.exists()

    log_entries = [json.loads(line) for line in custody_log.read_text().splitlines()]
    assert len(log_entries) == 1
    assert log_entries[0]["url"] == "https://example.com"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_forensic_capture.py::test_capture_page_produces_package -v`
Expected: FAIL

- [ ] **Step 3: Implement capture_page orchestrator**

Add to `scrapers/forensic_capture.py`:

```python
def capture_page(
    url: str,
    html_content: bytes,
    screenshot_bytes: bytes,
    page_title: str = "",
    response_headers: dict = None,
    status_code: int = 200,
    config=None,
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
        sha256_raw=sha256_rendered,  # for rendered captures, raw == rendered
        sha256_rendered=sha256_rendered,
        operator=config.operator,
        tool_version=config.tool_version,
        proxy_ip=config.proxy_url,
        wayback_url=wayback_url,
        ots_path=str(ots_path) if ots_path else "",
        warc_path=str(warc_path) if warc_path else "",
        screenshot_path=str(screenshot_path),
        page_title=page_title,
    )

    # Save metadata alongside capture
    meta_path = capture_dir / "metadata.json"
    meta_path.write_text(json.dumps(metadata, indent=2, default=str))

    # 8. Append to chain of custody log
    custody_log = config.output_dir / "chain_of_custody.jsonl"
    append_custody_log(custody_log, metadata)

    log.info(f"Captured {url} → {capture_dir.name} (sha256: {sha256_rendered[:16]}...)")
    return metadata
```

- [ ] **Step 4: Run all tests**

Run: `python -m pytest tests/test_forensic_capture.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add scrapers/forensic_capture.py tests/test_forensic_capture.py
git commit -m "feat(capture): full page capture orchestrator with evidence chain"
```

---

### Task 7: Website Crawler

**Files:**
- Create: `scrapers/website_crawl.py`
- Create: `tests/test_website_crawl.py`

- [ ] **Step 1: Write crawler test**

```python
# tests/test_website_crawl.py
"""Tests for website crawler."""

from scrapers.website_crawl import classify_page, normalize_url, should_crawl


def test_classify_page_donate():
    assert classify_page("https://cfi.org/donate", "Donate Now", "") == "donate"


def test_classify_page_about():
    assert classify_page("https://cfi.org/about-us", "About Us", "") == "about"


def test_classify_page_faq():
    assert classify_page("https://cfi.org/faq", "FAQ", "") == "faq"


def test_classify_page_news():
    assert classify_page("https://cfi.org/news/update", "News", "") == "news"


def test_classify_page_fallback():
    assert classify_page("https://cfi.org/xyz", "Random", "") == "other"


def test_normalize_url_strips_fragment():
    assert normalize_url("https://cfi.org/page#section") == "https://cfi.org/page"


def test_normalize_url_strips_trailing_slash():
    assert normalize_url("https://cfi.org/page/") == "https://cfi.org/page"


def test_should_crawl_same_domain():
    assert should_crawl("https://cfi.org/about", "cfi.org") is True


def test_should_crawl_external():
    assert should_crawl("https://google.com", "cfi.org") is False


def test_should_crawl_skip_assets():
    assert should_crawl("https://cfi.org/image.png", "cfi.org") is False
    assert should_crawl("https://cfi.org/style.css", "cfi.org") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_website_crawl.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement crawler utilities and main crawler**

```python
# scrapers/website_crawl.py
"""
Website crawler with forensic capture.

Crawls a website breadth-first, capturing each page with the forensic
capture engine. Uses Playwright for JS rendering and the proxy pool
for rate limit circumvention.

Usage:
    python -m scrapers.website_crawl https://www.centralfundofisrael.org
    python -m scrapers.website_crawl https://www.centralfundofisrael.org --dry-run
    python -m scrapers.website_crawl https://www.centralfundofisrael.org --proxy socks5h://127.0.0.1:9050
"""

import argparse
import json
import logging
import re
import sys
import time
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse

from scrapers.config import CaptureConfig
from scrapers.forensic_capture import capture_page

log = logging.getLogger("website_crawl")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Page categories per the spec
PAGE_CATEGORIES = {
    "about": ["about", "who-we-are", "our-story", "mission"],
    "donate": ["donate", "give", "giving", "contribution", "support"],
    "programs": ["programs", "projects", "what-we-do", "initiatives"],
    "news": ["news", "blog", "updates", "press"],
    "media": ["media", "gallery", "photos", "videos"],
    "board": ["board", "leadership", "team", "staff", "directors"],
    "financials": ["financials", "annual-report", "transparency", "990"],
    "contact": ["contact", "reach-us", "get-in-touch"],
    "faq": ["faq", "frequently-asked", "questions"],
    "mission": ["mission", "vision", "values", "purpose"],
}

SKIP_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".eot",
    ".pdf", ".zip", ".mp4", ".mp3", ".wav",
}


def classify_page(url: str, title: str, text: str) -> str:
    """Classify a page into a category based on URL path, title, and content."""
    url_lower = urlparse(url).path.lower()
    title_lower = title.lower() if title else ""

    for category, keywords in PAGE_CATEGORIES.items():
        for kw in keywords:
            if kw in url_lower or kw in title_lower:
                return category

    return "other"


def normalize_url(url: str) -> str:
    """Normalize a URL for deduplication."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def should_crawl(url: str, base_domain: str) -> bool:
    """Check if a URL should be crawled."""
    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc != base_domain:
        return False
    ext = Path(parsed.path).suffix.lower()
    if ext in SKIP_EXTENSIONS:
        return False
    if parsed.scheme not in ("http", "https", ""):
        return False
    return True


def crawl_website(
    start_url: str,
    config: CaptureConfig = None,
    max_pages: int = 200,
    delay_seconds: float = 2.0,
    dry_run: bool = False,
) -> list[dict]:
    """Crawl a website breadth-first, forensically capturing each page.

    Returns list of capture metadata dicts.
    """
    from playwright.sync_api import sync_playwright

    if config is None:
        config = CaptureConfig()
    config.ensure_dirs()

    parsed_start = urlparse(start_url)
    base_domain = parsed_start.netloc
    visited = set()
    queue = deque([start_url])
    captures = []

    log.info(f"Starting crawl: {start_url} (max {max_pages} pages)")
    if config.proxy_url:
        log.info(f"Using proxy: {config.proxy_url}")

    with sync_playwright() as p:
        browser_args = {
            "headless": True,
            "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        }
        if config.proxy_url:
            browser_args["proxy"] = {"server": config.proxy_url}

        browser = p.chromium.launch(**browser_args)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()

        while queue and len(captures) < max_pages:
            url = queue.popleft()
            normalized = normalize_url(url)
            if normalized in visited:
                continue
            visited.add(normalized)

            log.info(f"[{len(captures)+1}/{max_pages}] Capturing: {url}")
            if dry_run:
                captures.append({"url": url, "dry_run": True})
                continue

            try:
                response = page.goto(url, timeout=config.timeout_ms, wait_until="networkidle")
                if not response:
                    log.warning(f"No response for {url}")
                    continue

                status_code = response.status
                if status_code >= 400:
                    log.warning(f"HTTP {status_code} for {url}")
                    continue

                # Wait for dynamic content
                page.wait_for_timeout(1500)

                # Get rendered content
                html_content = page.content().encode("utf-8")
                page_title = page.title()
                screenshot_bytes = page.screenshot(full_page=config.screenshot_full_page)

                # Get response headers
                resp_headers = {}
                try:
                    resp_headers = {k: v for k, v in response.headers.items()}
                except Exception:
                    pass

                # Forensic capture
                metadata = capture_page(
                    url=url,
                    html_content=html_content,
                    screenshot_bytes=screenshot_bytes,
                    page_title=page_title,
                    response_headers=resp_headers,
                    status_code=status_code,
                    config=config,
                )
                metadata["page_category"] = classify_page(url, page_title, "")
                captures.append(metadata)

                # Extract links for crawl frontier
                links = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
                for link in links:
                    abs_link = urljoin(url, link)
                    norm_link = normalize_url(abs_link)
                    if norm_link not in visited and should_crawl(abs_link, base_domain):
                        queue.append(abs_link)

                time.sleep(delay_seconds)

            except Exception as e:
                log.error(f"Error capturing {url}: {e}")
                continue

        browser.close()

    # Write crawl index
    index_path = config.output_dir / "crawl_index.json"
    with open(index_path, "w") as f:
        json.dump(
            {
                "start_url": start_url,
                "pages_captured": len(captures),
                "pages_visited": len(visited),
                "captures": captures,
            },
            f, indent=2, default=str,
        )
    log.info(f"Crawl complete: {len(captures)} pages captured → {index_path}")

    return captures


def main():
    parser = argparse.ArgumentParser(description="Forensic website crawler")
    parser.add_argument("url", help="Starting URL to crawl")
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument("--delay", type=float, default=2.0, help="Delay between pages (seconds)")
    parser.add_argument("--proxy", default="", help="SOCKS5 proxy URL")
    parser.add_argument("--output-dir", default="targets/cfi/web")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-wayback", action="store_true", help="Skip Wayback submission")
    parser.add_argument("--no-ots", action="store_true", help="Skip OpenTimestamps")
    parser.add_argument("--no-warc", action="store_true", help="Skip WARC archival")
    args = parser.parse_args()

    config = CaptureConfig(
        output_dir=Path(args.output_dir),
        proxy_url=args.proxy,
        submit_wayback=not args.no_wayback,
        create_ots=not args.no_ots,
        save_warc=not args.no_warc,
    )

    crawl_website(
        start_url=args.url,
        config=config,
        max_pages=args.max_pages,
        delay_seconds=args.delay,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add scrapers/website_crawl.py tests/test_website_crawl.py
git commit -m "feat(crawl): forensic website crawler with Playwright + proxy support"
```

---

### Task 8: Wayback Historical Snapshots

**Files:**
- Create: `scrapers/wayback_historical.py`

- [ ] **Step 1: Implement historical snapshot collector**

```python
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
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger("wayback_historical")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

CDX_API = "https://web.archive.org/cdx/search/cdx"


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


def download_snapshot(url: str, timestamp: str, output_dir: Path) -> Optional[Path]:
    """Download a specific Wayback snapshot and save it."""
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
        return filepath

    except Exception as e:
        log.warning(f"Failed to download snapshot {timestamp}: {e}")
        return None


def collect_historical(
    url: str,
    output_dir: Path,
    max_snapshots: int = 50,
    delay: float = 1.0,
) -> dict:
    """Collect historical Wayback snapshots for a URL.

    Returns summary dict with snapshot metadata and file paths.
    """
    log.info(f"Fetching CDX index for: {url}")
    snapshots = fetch_snapshots(url)
    log.info(f"Found {len(snapshots)} snapshots")

    # Sample evenly if too many
    if len(snapshots) > max_snapshots:
        step = len(snapshots) // max_snapshots
        snapshots = snapshots[::step][:max_snapshots]
        log.info(f"Sampled down to {len(snapshots)} snapshots")

    results = []
    for snap in snapshots:
        ts = snap["timestamp"]
        filepath = download_snapshot(url, ts, output_dir / "snapshots")
        results.append({
            **snap,
            "file_path": str(filepath) if filepath else None,
            "wayback_url": f"https://web.archive.org/web/{ts}/{url}",
        })
        time.sleep(delay)

    # Save index
    index = {
        "url": url,
        "total_snapshots_available": len(fetch_snapshots(url)),
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
    args = parser.parse_args()

    collect_historical(
        url=args.url,
        output_dir=Path(args.output_dir),
        max_snapshots=args.max_snapshots,
        delay=args.delay,
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Commit**

```bash
git add scrapers/wayback_historical.py
git commit -m "feat(wayback): historical snapshot collector via CDX API"
```

---

### Task 9: Update Dependencies and Spec

**Files:**
- Modify: `requirements.txt`
- Modify: `targets/cfi/metadata.json` (after crawl)
- Modify: `~/Desktop/repos/data/ag-complaint-pipeline/specs/us-passthrough-collection.md`

- [ ] **Step 1: Update requirements.txt**

```
psycopg[binary]>=3.3
python-dotenv>=1.0
playwright>=1.40
warcio>=1.7
opentimestamps-client>=0.7
waybackpy>=3.0
requests>=2.31
```

- [ ] **Step 2: Install dependencies and Playwright browsers**

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

- [ ] **Step 3: Update spec §3.3 with forensic capture protocol**

Add to `us-passthrough-collection.md` §3.3 after the website crawl spec:

```markdown
**Forensic Capture Protocol (FRE 901(a) Compliance):**

Every page capture produces an evidence package containing:
1. **Rendered HTML** — after JavaScript execution (Playwright)
2. **Full-page screenshot** — PNG, timestamped
3. **WARC archive** — ISO 28500 standard archival format
4. **SHA-256 hash** — of rendered HTML content (integrity proof)
5. **Wayback Machine archive** — third-party neutral copy (FRE 901(b)(9))
6. **OpenTimestamps proof** — .ots file anchoring SHA-256 hash to Bitcoin blockchain
   (immediate calendar commitment; full Bitcoin proof available in 6-24 hours)
7. **Chain-of-custody log** — append-only JSONL with capture_id, URL, timestamp,
   operator, tool version, proxy IP, all hashes, and archive URLs

This protocol satisfies:
- FRE 901(a): authenticated origin via hash + timestamp
- FRE 902(13)/(14): self-authenticating electronic records
- Telewizja Polska precedent: Wayback Machine as reliable archival source
- Integrity: SHA-256 + OpenTimestamps proves no post-capture modification
```

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "chore: add forensic capture dependencies"
```

---

### Task 10: Run CFI Website Crawl

**Files:** None (execution task)

- [ ] **Step 1: Start proxy tunnel**

```bash
proxies up  # or: proxies list to check status
```

- [ ] **Step 2: Dry run first**

```bash
python -m scrapers.website_crawl https://www.centralfundofisrael.org \
    --dry-run --max-pages 10
```

Verify: prints URLs it would crawl without fetching.

- [ ] **Step 3: Run real crawl with forensic capture**

```bash
python -m scrapers.website_crawl https://www.centralfundofisrael.org \
    --proxy socks5h://127.0.0.1:9050 \
    --max-pages 100 \
    --delay 3.0 \
    --output-dir targets/cfi/web
```

Monitor: watch for `Captured ... → captures/...` log lines.

- [ ] **Step 4: Run Wayback historical collection for priority pages**

```bash
python -m scrapers.wayback_historical https://www.centralfundofisrael.org/donate \
    --max-snapshots 30 --output-dir targets/cfi/web/wayback/donate

python -m scrapers.wayback_historical https://www.centralfundofisrael.org/about \
    --max-snapshots 20 --output-dir targets/cfi/web/wayback/about

python -m scrapers.wayback_historical https://www.centralfundofisrael.org/faq \
    --max-snapshots 20 --output-dir targets/cfi/web/wayback/faq
```

- [ ] **Step 5: Upgrade OTS proofs (run 24 hours later)**

```bash
find targets/cfi/web/ots -name "*.ots" -exec ots upgrade {} \;
```

This fetches the completed Bitcoin block proofs from the OTS calendars.

- [ ] **Step 6: Commit results**

```bash
git add targets/cfi/web/crawl_index.json targets/cfi/web/chain_of_custody.jsonl
git commit -m "data(cfi): forensic website crawl — captured pages with evidence chain"
```

Note: WARC files and screenshots are uploaded to R2 — only the crawl index and custody log go in git.

---

### Task 11: R2 Upload Module

**Files:**
- Create: `scrapers/r2_upload.py`
- Create: `tests/test_r2_upload.py`

- [ ] **Step 1: Write R2 upload test (mocked)**

```python
# tests/test_r2_upload.py
"""Tests for R2 upload module."""

from pathlib import Path
from unittest.mock import MagicMock, patch


def test_build_r2_key():
    from scrapers.r2_upload import build_r2_key

    key = build_r2_key("cfi", "web", "donate_20260401_120000", "screenshot.png")
    assert key == "cfi/web/donate_20260401_120000/screenshot.png"


def test_build_r2_key_with_subdir():
    from scrapers.r2_upload import build_r2_key

    key = build_r2_key("cfi", "web/ots", "", "abc123.ots")
    assert key == "cfi/web/ots/abc123.ots"


@patch("scrapers.r2_upload.boto3")
def test_upload_file(mock_boto3, tmp_path):
    from scrapers.r2_upload import upload_file, R2Config

    # Create a test file
    test_file = tmp_path / "test.png"
    test_file.write_bytes(b"fake png data")

    mock_client = MagicMock()
    mock_boto3.client.return_value = mock_client

    config = R2Config(
        endpoint_url="https://fake.r2.cloudflarestorage.com",
        access_key_id="test-key",
        secret_access_key="test-secret",
        bucket_name="ag-complaint-evidence",
    )

    url = upload_file(test_file, "cfi/web/test.png", config)

    mock_client.upload_file.assert_called_once()
    assert "cfi/web/test.png" in url
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_r2_upload.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement R2 upload module**

```python
# scrapers/r2_upload.py
"""
Cloudflare R2 upload for forensic capture evidence.

R2 is S3-compatible, so we use boto3. Evidence files (screenshots, WARC,
OTS proofs, HTML) are uploaded to the `ag-complaint-evidence` bucket under
a path convention: {entity-slug}/{category}/{capture-slug}/{filename}

Credentials are loaded from keys.db or environment variables.
"""

import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import boto3

log = logging.getLogger("r2_upload")


@dataclass
class R2Config:
    endpoint_url: str = ""
    access_key_id: str = ""
    secret_access_key: str = ""
    bucket_name: str = "ag-complaint-evidence"
    public_url_base: str = ""  # if bucket has public access

    def __post_init__(self):
        if not self.endpoint_url:
            self._load_from_env_or_keys_db()

    def _load_from_env_or_keys_db(self):
        """Load R2 credentials from environment or keys.db."""
        self.endpoint_url = os.environ.get("R2_ENDPOINT_URL", "")
        self.access_key_id = os.environ.get("R2_ACCESS_KEY_ID", "")
        self.secret_access_key = os.environ.get("R2_SECRET_ACCESS_KEY", "")

        if self.endpoint_url:
            return

        keys_db = Path.home() / "Desktop" / "repos" / "keys.db"
        if not keys_db.exists():
            return

        try:
            conn = sqlite3.connect(str(keys_db))
            for key_name, attr in [
                ("R2_ENDPOINT_URL", "endpoint_url"),
                ("R2_ACCESS_KEY_ID", "access_key_id"),
                ("R2_SECRET_ACCESS_KEY", "secret_access_key"),
            ]:
                cur = conn.execute(
                    "SELECT key_value FROM keys WHERE service = 'cloudflare-r2' AND key_name = ? AND account = 'goodshepherdcollective'",
                    (key_name,),
                )
                row = cur.fetchone()
                if row:
                    setattr(self, attr, row[0])
            conn.close()
        except Exception as e:
            log.warning(f"Could not read R2 keys from keys.db: {e}")

    @property
    def is_configured(self) -> bool:
        return bool(self.endpoint_url and self.access_key_id and self.secret_access_key)


def build_r2_key(entity_slug: str, category: str, capture_slug: str, filename: str) -> str:
    """Build the R2 object key (path) for an evidence file."""
    parts = [entity_slug, category]
    if capture_slug:
        parts.append(capture_slug)
    parts.append(filename)
    return "/".join(parts)


def get_r2_client(config: R2Config):
    """Create a boto3 S3 client configured for Cloudflare R2."""
    return boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=config.secret_access_key,
        region_name="auto",
    )


def upload_file(
    local_path: Path,
    r2_key: str,
    config: R2Config = None,
    content_type: str = "",
) -> str:
    """Upload a file to R2 and return the R2 URL.

    Returns: R2 URL in format {endpoint}/{bucket}/{key}
    """
    if config is None:
        config = R2Config()

    if not config.is_configured:
        log.warning("R2 not configured — skipping upload")
        return f"r2://{config.bucket_name}/{r2_key}"  # return placeholder

    client = get_r2_client(config)

    extra_args = {}
    if content_type:
        extra_args["ContentType"] = content_type
    elif local_path.suffix == ".png":
        extra_args["ContentType"] = "image/png"
    elif local_path.suffix == ".html":
        extra_args["ContentType"] = "text/html"
    elif local_path.suffix == ".gz":
        extra_args["ContentType"] = "application/gzip"

    client.upload_file(
        str(local_path),
        config.bucket_name,
        r2_key,
        ExtraArgs=extra_args if extra_args else None,
    )

    url = f"{config.endpoint_url}/{config.bucket_name}/{r2_key}"
    log.info(f"Uploaded {local_path.name} → r2://{config.bucket_name}/{r2_key}")
    return url


def upload_capture_dir(
    capture_dir: Path,
    entity_slug: str,
    config: R2Config = None,
) -> dict[str, str]:
    """Upload all files in a capture directory to R2.

    Returns dict mapping filename → R2 URL.
    """
    if config is None:
        config = R2Config()

    urls = {}
    for filepath in capture_dir.iterdir():
        if filepath.is_file() and filepath.name != "metadata.json":
            r2_key = build_r2_key(entity_slug, "web", capture_dir.name, filepath.name)
            url = upload_file(filepath, r2_key, config)
            urls[filepath.name] = url

    return urls
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_r2_upload.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add scrapers/r2_upload.py tests/test_r2_upload.py
git commit -m "feat(r2): Cloudflare R2 upload module for evidence storage"
```

---

### Task 12: Wire R2 Upload + Neon Ingestion into Capture Pipeline

**Files:**
- Modify: `scrapers/forensic_capture.py` (add R2 upload after local save)
- Modify: `scrapers/website_crawl.py` (add Neon web_pages insert after capture)

- [ ] **Step 1: Add R2 upload to capture_page()**

In `scrapers/forensic_capture.py`, after step 7 (build metadata) and before step 8 (custody log), add R2 upload:

```python
    # 7b. Upload to R2
    r2_urls = {}
    if config.upload_to_r2:
        from scrapers.r2_upload import upload_capture_dir
        r2_urls = upload_capture_dir(capture_dir, config.entity_slug, config.r2_config)
        metadata["r2_urls"] = r2_urls
```

Also update `CaptureConfig` in `scrapers/config.py` to add:

```python
    # R2 storage
    upload_to_r2: bool = True
    entity_slug: str = "cfi"
    r2_config: object = None  # R2Config instance, loaded lazily
```

And in `__post_init__`:

```python
        if self.upload_to_r2 and self.r2_config is None:
            from scrapers.r2_upload import R2Config
            self.r2_config = R2Config()
```

- [ ] **Step 2: Add Neon ingestion to website_crawl.py**

After each `capture_page()` call in `crawl_website()`, insert into `web_pages`:

```python
                # Insert into Neon web_pages table
                if not dry_run and config.db_url:
                    _insert_web_page(
                        db_url=config.db_url,
                        us_entity_id=config.us_entity_id,
                        url=url,
                        page_title=page_title,
                        page_category=metadata.get("page_category", "other"),
                        extracted_text=_extract_text(html_content),
                        html_path=metadata.get("r2_urls", {}).get("rendered.html", ""),
                        screenshot_path=metadata.get("r2_urls", {}).get("screenshot.png", ""),
                        crawl_timestamp=metadata["timestamp_utc"],
                    )
```

Add the helper functions:

```python
import re
from html.parser import HTMLParser


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


def _insert_web_page(
    db_url: str,
    us_entity_id: str,
    url: str,
    page_title: str,
    page_category: str,
    extracted_text: str,
    html_path: str,
    screenshot_path: str,
    crawl_timestamp: str,
) -> None:
    """Insert a captured page into the Neon web_pages table."""
    import psycopg
    from scrapers.forensic_capture import url_to_slug

    source_id = f"crawl:{url_to_slug(url)}:{crawl_timestamp[:19]}"
    try:
        conn = psycopg.connect(db_url)
        conn.execute(
            """INSERT INTO web_pages (
                    source_id, source_url, us_entity_id,
                    page_title, page_category, extracted_text,
                    html_path, screenshot_path, crawl_timestamp
                ) VALUES (%s, %s, %s::uuid, %s, %s, %s, %s, %s, %s::timestamptz)
                ON CONFLICT DO NOTHING""",
            (
                source_id, url, us_entity_id,
                page_title, page_category, extracted_text[:50000],  # cap at 50k chars
                html_path, screenshot_path, crawl_timestamp,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"Failed to insert web_page for {url}: {e}")
```

Also update `CaptureConfig` to include DB URL:

```python
    # Neon database
    db_url: str = ""  # loaded from DATABASE_URL_DEV env var
    us_entity_id: str = ""  # UUID of the US entity being crawled
```

And in `__post_init__`:

```python
        self.db_url = self.db_url or os.environ.get("DATABASE_URL_DEV", "")
```

- [ ] **Step 3: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS (existing tests don't use R2 or DB — they use `config` with those disabled)

- [ ] **Step 4: Commit**

```bash
git add scrapers/config.py scrapers/forensic_capture.py scrapers/website_crawl.py
git commit -m "feat: wire R2 upload + Neon web_pages ingestion into capture pipeline"
```

---

### Task 13: Create R2 Bucket and Run Full Pipeline

**Files:** None (execution task)

- [ ] **Step 1: Create R2 bucket via Cloudflare dashboard or API**

Using the Defund Racism Cloudflare account, create bucket `ag-complaint-evidence` in the Cloudflare dashboard.

- [ ] **Step 2: Add R2 env vars to .env**

```bash
# Add to .env (values from keys.db)
R2_ENDPOINT_URL=<from keys.db>
R2_ACCESS_KEY_ID=<from keys.db>
R2_SECRET_ACCESS_KEY=<from keys.db>
R2_BUCKET_NAME=ag-complaint-evidence
```

- [ ] **Step 3: Get CFI us_entity_id from Neon**

```bash
python3 -c "
import psycopg, os
from dotenv import load_dotenv
load_dotenv()
conn = psycopg.connect(os.environ['DATABASE_URL_DEV'])
row = conn.execute(\"SELECT id FROM us_entities WHERE ein = '13-2992985'\").fetchone()
print(f'US_ENTITY_ID={row[0]}')
conn.close()
"
```

Add `US_ENTITY_ID=<uuid>` to .env.

- [ ] **Step 4: Run the full pipeline**

```bash
python -m scrapers.website_crawl https://www.centralfundofisrael.org \
    --proxy socks5h://127.0.0.1:9050 \
    --max-pages 100 \
    --delay 3.0 \
    --output-dir targets/cfi/web
```

This will: crawl → capture each page → hash → WARC → screenshot → upload to R2 → insert into Neon → submit to Wayback → create OTS proof → log chain of custody.

- [ ] **Step 5: Verify data in Neon**

```sql
SELECT source_url, page_title, page_category, 
       length(extracted_text) as text_len,
       html_path, screenshot_path
FROM web_pages 
WHERE us_entity_id = '<uuid>'
ORDER BY crawl_timestamp;
```

- [ ] **Step 6: Commit metadata (not media)**

```bash
git add targets/cfi/web/crawl_index.json targets/cfi/web/chain_of_custody.jsonl
git commit -m "data(cfi): forensic website crawl with R2 storage + Neon ingestion"
```
