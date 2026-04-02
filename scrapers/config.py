# scrapers/config.py
"""CFI-specific capture configuration wrapping the shared CaptureConfig.

The canonical CaptureConfig lives in ag-complaint-pipeline/pipeline/shared/capture_config.py.
This module provides a CFI-specific subclass with:
- entity_slug defaulting to "cfi"
- output_dir defaulting to targets/cfi/web
- upload_to_r2 enabled by default
- R2Config auto-loading from scrapers.r2_upload
- wayback_dir and us_entity_id fields
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pipeline.shared.capture_config import CaptureConfig as BaseCaptureConfig

load_dotenv()


@dataclass
class CaptureConfig(BaseCaptureConfig):
    """CFI-specific capture configuration.

    Extends the shared CaptureConfig with CFI defaults and R2 auto-loading.
    """

    entity_slug: str = "cfi"

    # Override shared defaults for CFI
    output_dir: Path = Path("targets/cfi/web")
    upload_to_r2: bool = True

    # CFI-specific fields
    wayback_dir: Optional[Path] = field(default=None)
    us_entity_id: str = ""

    def __post_init__(self):
        # Call shared __post_init__ for env var loading and dir setup
        super().__post_init__()

        # CFI-specific: wayback_dir
        self.wayback_dir = self.wayback_dir or self.output_dir / "wayback"

        # CFI-specific: us_entity_id from env
        self.us_entity_id = self.us_entity_id or os.environ.get("US_ENTITY_ID", "")

        # CFI-specific: db_url uses DATABASE_URL_DEV (not DATABASE_URL)
        if not self.db_url:
            self.db_url = os.environ.get("DATABASE_URL_DEV", "")

        # Auto-load R2Config when upload_to_r2 is enabled
        if self.upload_to_r2 and self.r2_config is None:
            try:
                from scrapers.r2_upload import R2Config
                self.r2_config = R2Config()
            except Exception:
                pass

    def ensure_dirs(self):
        """Create output directories including CFI-specific wayback_dir."""
        super().ensure_dirs()
        if self.wayback_dir:
            self.wayback_dir.mkdir(parents=True, exist_ok=True)
