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


def test_create_ots_proof(tmp_path):
    """OpenTimestamps should create a .ots proof file from a SHA-256 hash."""
    from scrapers.forensic_capture import create_ots_proof

    sha256_hex = hashlib.sha256(b"test content").hexdigest()
    ots_path = create_ots_proof(sha256_hex, output_dir=tmp_path, filename="test.ots")

    assert ots_path.exists()
    assert ots_path.suffix == ".ots"
    assert ots_path.stat().st_size > 0


@patch("pipeline.shared.forensic_capture.requests.get")
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


@patch("pipeline.shared.forensic_capture.requests.get")
def test_submit_wayback_failure(mock_get):
    """Wayback failure should return None, not raise."""
    from scrapers.forensic_capture import submit_to_wayback

    mock_get.side_effect = Exception("Network error")
    result = submit_to_wayback("https://example.com")
    assert result is None


def test_capture_page_produces_package(tmp_path):
    """Full capture should produce WARC, screenshot, metadata, and custody log."""
    from scrapers.config import CaptureConfig
    from scrapers.forensic_capture import capture_page

    config = CaptureConfig(
        output_dir=tmp_path,
        submit_wayback=False,  # don't hit real Wayback in tests
        create_ots=False,  # don't hit real OTS calendars in tests
        upload_to_r2=False,  # don't hit real R2 in tests
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
