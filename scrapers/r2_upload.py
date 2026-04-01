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
                # Try both accounts, preferring whichever has values
                cur = conn.execute(
                    "SELECT key_value FROM keys WHERE service = 'cloudflare-r2' AND key_name = ? AND account IN ('goodshepherdcollective', 'defundracism') ORDER BY CASE account WHEN 'defundracism' THEN 0 ELSE 1 END LIMIT 1",
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
