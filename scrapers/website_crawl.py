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
import hashlib
import json
import logging
import re
import sys
import time
from collections import deque
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

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
    "annual_report": ["annual-report", "annual report", "impact-report", "impact report"],
}

SKIP_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".eot",
    ".pdf", ".zip", ".mp4", ".mp3", ".wav",
}


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
    conn,
    us_entity_id: str,
    url: str,
    page_title: str,
    page_category: str,
    extracted_text: str,
    html_path: str,
    screenshot_path: str,
    crawl_timestamp: str,
    ingestion_id: str = None,
) -> None:
    """Insert a captured page into the Neon web_pages table."""
    source_id = f"crawl:{hashlib.sha256(url.encode()).hexdigest()[:12]}:{crawl_timestamp}"
    try:
        params = [
            source_id, url, us_entity_id,
            page_title, page_category, extracted_text[:50000],  # cap at 50k chars
            html_path, screenshot_path, crawl_timestamp,
        ]
        if ingestion_id:
            conn.execute(
                """INSERT INTO web_pages (
                        source_id, source_url, us_entity_id,
                        page_title, page_category, extracted_text,
                        html_path, screenshot_path, crawl_timestamp,
                        ingestion_id
                    ) VALUES (%s, %s, %s::uuid, %s, %s, %s, %s, %s, %s::timestamptz, %s::uuid)
                    ON CONFLICT DO NOTHING""",
                (*params, ingestion_id),
            )
        else:
            conn.execute(
                """INSERT INTO web_pages (
                        source_id, source_url, us_entity_id,
                        page_title, page_category, extracted_text,
                        html_path, screenshot_path, crawl_timestamp
                    ) VALUES (%s, %s, %s::uuid, %s, %s, %s, %s, %s, %s::timestamptz)
                    ON CONFLICT DO NOTHING""",
                tuple(params),
            )
    except Exception as e:
        log.error(f"Failed to insert web_page for {url}: {e}")


def classify_page(url: str, title: str, text: str) -> str:
    """Classify a page into a category based on URL path, title, and content."""
    url_lower = urlparse(url).path.lower()
    title_lower = title.lower() if title else ""

    for category, keywords in PAGE_CATEGORIES.items():
        for kw in keywords:
            if kw in url_lower or kw in title_lower:
                return category

    # Fallback: check page content for category-specific keywords
    if text:
        text_lower = text.lower()
        content_signals = {
            "donate": ["donate", "contribute", "tax-deductible", "tax deductible", "give now", "make a gift"],
            "board": ["board of directors", "trustees", "officers", "board members"],
            "about": ["our mission", "who we are", "our story", "founded in"],
            "contact": ["contact us", "email us", "get in touch", "reach out"],
            "financials": ["annual report", "form 990", "financial statement", "audit report"],
            "annual_report": ["annual report", "impact report", "year in review"],
            "faq": ["frequently asked", "common questions"],
            "programs": ["our programs", "our projects", "what we do", "our initiatives"],
            "news": ["press release", "in the news", "latest updates"],
        }
        for category, phrases in content_signals.items():
            for phrase in phrases:
                if phrase in text_lower:
                    return category

    return "other"


TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "fbclid", "gclid", "ref"}


def normalize_url(url: str) -> str:
    """Normalize a URL for deduplication.

    Strips fragment, trailing slash, and common tracking query parameters
    (utm_*, fbclid, gclid, ref) while preserving other query params.
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    # Filter out tracking parameters, keep everything else
    query_params = parse_qs(parsed.query, keep_blank_values=True)
    filtered = {k: v for k, v in query_params.items() if k not in TRACKING_PARAMS}
    query_string = urlencode(filtered, doseq=True)
    if query_string:
        return f"{parsed.scheme}://{parsed.netloc}{path}?{query_string}"
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

    # Open a single DB connection for the entire crawl
    db_conn = None
    ingestion_id = None
    if not dry_run and config.db_url:
        import psycopg
        try:
            db_conn = psycopg.connect(config.db_url)
            row = db_conn.execute(
                """INSERT INTO ingestion_log (source_name, status, metadata)
                   VALUES ('website-crawl', 'running', %s::jsonb)
                   RETURNING id""",
                (json.dumps({"start_url": start_url, "max_pages": max_pages}),),
            ).fetchone()
            ingestion_id = str(row[0])
            db_conn.commit()
            log.info(f"Ingestion log created: {ingestion_id}")
        except Exception as e:
            log.error(f"Failed to create DB connection or ingestion log: {e}")
            db_conn = None

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
                response = page.goto(url, timeout=config.timeout_ms, wait_until="domcontentloaded")
                if not response:
                    log.warning(f"No response for {url}")
                    continue

                status_code = response.status
                if status_code >= 400:
                    log.warning(f"HTTP {status_code} for {url}")
                    continue

                # Wait for dynamic content
                page.wait_for_timeout(5000)  # Wix sites need time for JS rendering

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

                # Insert into Neon web_pages table
                if not dry_run and db_conn:
                    _insert_web_page(
                        conn=db_conn,
                        us_entity_id=config.us_entity_id,
                        url=url,
                        page_title=page_title,
                        page_category=metadata.get("page_category", "other"),
                        extracted_text=_extract_text(html_content),
                        html_path=metadata.get("r2_urls", {}).get("rendered.html", ""),
                        screenshot_path=metadata.get("r2_urls", {}).get("screenshot.png", ""),
                        crawl_timestamp=metadata["timestamp_utc"],
                        ingestion_id=ingestion_id,
                    )

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

    # Finalize ingestion log and close DB connection
    if db_conn:
        try:
            if ingestion_id:
                db_conn.execute(
                    """UPDATE ingestion_log SET
                            finished_at = now(),
                            records_added = %s,
                            status = 'success'
                       WHERE id = %s::uuid""",
                    (len(captures), ingestion_id),
                )
            db_conn.commit()
        except Exception as e:
            log.error(f"Failed to finalize ingestion log: {e}")
        finally:
            db_conn.close()

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
