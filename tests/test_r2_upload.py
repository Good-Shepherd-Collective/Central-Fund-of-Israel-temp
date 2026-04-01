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
