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

    # R2 storage
    upload_to_r2: bool = True
    entity_slug: str = "cfi"
    r2_config: object = None  # R2Config instance, loaded lazily

    # Neon database
    db_url: str = ""  # loaded from DATABASE_URL_DEV env var
    us_entity_id: str = ""  # UUID of the US entity being crawled

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
        self.db_url = self.db_url or os.environ.get("DATABASE_URL_DEV", "")
        self.us_entity_id = self.us_entity_id or os.environ.get("US_ENTITY_ID", "")

        if self.upload_to_r2 and self.r2_config is None:
            from scrapers.r2_upload import R2Config
            self.r2_config = R2Config()

    def ensure_dirs(self):
        """Create output directories."""
        for d in [self.captures_dir, self.ots_dir, self.wayback_dir]:
            d.mkdir(parents=True, exist_ok=True)
