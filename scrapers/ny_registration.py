#!/usr/bin/env python3
"""
NY Registration Scraper -- AG Complaint Pipeline (Stage 0)

Scrapes two NY state databases for nonprofit entity registration data:
1. NY Charities Bureau (charities-search.ag.ny.gov) -- REST API
2. NY Secretary of State (apps.dos.ny.gov/publicInquiry) -- Playwright browser

Usage:
    python scrapers/ny_registration.py "Central Fund of Israel"
    python scrapers/ny_registration.py "Central Fund of Israel" --dry-run
    python scrapers/ny_registration.py "Central Fund of Israel" --output-dir targets/cfi/registration
    python scrapers/ny_registration.py "Central Fund of Israel" --charities-only
    python scrapers/ny_registration.py "Central Fund of Israel" --sos-only --headed

Environment:
    DATABASE_URL  -- Neon PostgreSQL connection string (or loaded from keys.db)
    DRY_RUN       -- set to 'true' to skip DB writes

Output:
    - JSON to stdout
    - ny_charities.json and ny_sos.json written to output directory
    - us_entities table updated in Neon (unless --dry-run)

Notes:
    The Charities Bureau moved from charitiesnys.com to charities-search.ag.ny.gov
    in late 2024. It exposes a clean REST API at charities-search-api.ag.ny.gov.

    The SOS site (apps.dos.ny.gov) uses F5 Application Security Manager (WAF/anti-bot).
    Headless browsers are blocked. Use --headed mode or run from the Infomaniak VPS
    with a real browser session. On GitHub Actions, this may require Scrapling with
    StealthySession or a pre-authenticated session cookie.
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import traceback
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus

import psycopg
import requests
from playwright.sync_api import (
    Page,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ny_registration")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Charities Bureau REST API (discovered via network inspection)
CHARITIES_API_BASE = "https://charities-search-api.ag.ny.gov/api/FileNet"
CHARITIES_SEARCH_URL = f"{CHARITIES_API_BASE}/RegistrySearch"
CHARITIES_DETAIL_URL = f"{CHARITIES_API_BASE}/RegistryDetail"
CHARITIES_DOC_URL = f"{CHARITIES_API_BASE}/RegistryDocument"
CHARITIES_WEB_BASE = "https://charities-search.ag.ny.gov/RegistrySearch"

# NY SOS
SOS_SEARCH_URL = "https://apps.dos.ny.gov/publicInquiry/"

DEFAULT_OUTPUT_DIR = "output/registration"
TIMEOUT_MS = 30_000
NAV_TIMEOUT_MS = 60_000

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------


def get_connection_string() -> Optional[str]:
    """Get Neon connection string from env or keys.db."""
    conn_str = os.environ.get("DATABASE_URL")
    if conn_str:
        return conn_str

    keys_db = Path.home() / "Desktop" / "repos" / "keys.db"
    if keys_db.exists():
        try:
            conn = sqlite3.connect(str(keys_db))
            cur = conn.execute(
                "SELECT key_value FROM keys WHERE service = 'neon-ag-pipeline' AND key_name = 'connection_string'"
            )
            row = cur.fetchone()
            conn.close()
            if row:
                return row[0]
        except Exception as e:
            log.warning(f"Could not read keys.db: {e}")

    return None


def write_to_db(charities_data: dict, sos_data: dict, org_name: str, dry_run: bool = False) -> None:
    """Upsert scraped registration data into us_entities table."""
    if dry_run:
        log.info("[DRY RUN] Skipping database write")
        return

    conn_str = get_connection_string()
    if not conn_str:
        log.warning("No DATABASE_URL or keys.db entry found -- skipping DB write")
        return

    try:
        conn = psycopg.connect(conn_str)
        conn.autocommit = False
        cur = conn.cursor()

        # Log the ingestion
        cur.execute(
            """
            INSERT INTO ingestion_log (source_name, status, metadata)
            VALUES ('ny-registration-scraper', 'running', %s)
            RETURNING id
            """,
            (json.dumps({"org_name": org_name, "scrape_date": date.today().isoformat()}),),
        )
        ingestion_id = cur.fetchone()[0]

        # Build update fields from scraped data
        update_fields = {}

        if charities_data and charities_data.get("status") != "error":
            update_fields["ny_charities_registered"] = charities_data.get("registration_number") is not None
            update_fields["ny_charities_number"] = charities_data.get("registration_number")
            update_fields["ny_charities_status"] = charities_data.get("registration_status", "active")

        if sos_data and sos_data.get("status") != "error":
            update_fields["ny_sos_entity_id"] = sos_data.get("entity_id")
            if sos_data.get("formation_date"):
                update_fields["date_incorporated"] = sos_data.get("formation_date")
            if sos_data.get("stated_purposes"):
                update_fields["stated_purposes"] = sos_data.get("stated_purposes")
            if sos_data.get("jurisdiction"):
                update_fields["state_incorporated"] = sos_data.get("jurisdiction")

        if not update_fields:
            log.info("No valid data to write to DB")
            cur.execute(
                "UPDATE ingestion_log SET status = 'success', finished_at = now(), records_updated = 0 WHERE id = %s",
                (ingestion_id,),
            )
            conn.commit()
            conn.close()
            return

        # Check if entity already exists by name
        cur.execute("SELECT id FROM us_entities WHERE org_name ILIKE %s", (org_name,))
        row = cur.fetchone()

        if row:
            entity_id = row[0]
            set_clauses = []
            values = []
            for k, v in update_fields.items():
                set_clauses.append(f"{k} = %s")
                values.append(v)
            set_clauses.append("updated_at = now()")
            set_clauses.append("ingestion_id = %s")
            values.append(ingestion_id)
            values.append(entity_id)

            cur.execute(
                f"UPDATE us_entities SET {', '.join(set_clauses)} WHERE id = %s",
                values,
            )
            log.info(f"Updated existing us_entities record: {entity_id}")
        else:
            # Insert new record with placeholder EIN
            ein_placeholder = f"PENDING-{org_name[:20].replace(' ', '-').upper()}"

            # Use EIN from charities data if available
            ein = ein_placeholder
            if charities_data and charities_data.get("ein"):
                raw_ein = charities_data["ein"]
                # Format EIN with dash: 132992985 -> 13-2992985
                if len(raw_ein) == 9 and raw_ein.isdigit():
                    ein = f"{raw_ein[:2]}-{raw_ein[2:]}"
                else:
                    ein = raw_ein

            cur.execute(
                """
                INSERT INTO us_entities (
                    source_id, org_name, ein,
                    ny_charities_registered, ny_charities_number, ny_charities_status,
                    ny_sos_entity_id, date_incorporated, stated_purposes, state_incorporated,
                    collection_status, ingestion_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'identified', %s)
                ON CONFLICT (source_id) DO UPDATE SET
                    ny_charities_registered = EXCLUDED.ny_charities_registered,
                    ny_charities_number = EXCLUDED.ny_charities_number,
                    ny_charities_status = EXCLUDED.ny_charities_status,
                    ny_sos_entity_id = EXCLUDED.ny_sos_entity_id,
                    date_incorporated = EXCLUDED.date_incorporated,
                    stated_purposes = EXCLUDED.stated_purposes,
                    state_incorporated = EXCLUDED.state_incorporated,
                    updated_at = now(),
                    ingestion_id = EXCLUDED.ingestion_id
                """,
                (
                    ein,
                    org_name,
                    ein,
                    update_fields.get("ny_charities_registered"),
                    update_fields.get("ny_charities_number"),
                    update_fields.get("ny_charities_status"),
                    update_fields.get("ny_sos_entity_id"),
                    update_fields.get("date_incorporated"),
                    update_fields.get("stated_purposes"),
                    update_fields.get("state_incorporated"),
                    ingestion_id,
                ),
            )
            log.info(f"Inserted new us_entities record for '{org_name}' (EIN: {ein})")

        cur.execute(
            "UPDATE ingestion_log SET status = 'success', finished_at = now(), records_updated = 1 WHERE id = %s",
            (ingestion_id,),
        )

        conn.commit()
        conn.close()
        log.info("Database write complete")

    except Exception as e:
        log.error(f"Database write failed: {e}")
        traceback.print_exc()


# ---------------------------------------------------------------------------
# NY Charities Bureau -- REST API
# ---------------------------------------------------------------------------


def scrape_charities_bureau(org_name: str, download_filings: bool = False, output_dir: Optional[str] = None) -> dict:
    """
    Query the NY Charities Bureau REST API.

    The Charities Bureau registry moved to charities-search.ag.ny.gov and exposes
    a clean JSON API at charities-search-api.ag.ny.gov/api/FileNet.

    Endpoints:
      - RegistrySearch?orgName=...  -> search results (list)
      - RegistryDetail?orgID=...    -> full detail with documents
      - RegistryDocument?guid=...   -> download a document PDF
    """
    now = datetime.now(timezone.utc).isoformat()
    result = {
        "source": "ny_charities_bureau",
        "source_url": CHARITIES_WEB_BASE,
        "org_name_searched": org_name,
        "scrape_timestamp": now,
        "status": "pending",
        # Search results
        "match_count": 0,
        "matches": [],
        # Best match detail
        "registration_number": None,
        "registration_status": None,
        "registration_date": None,
        "last_filing_date": None,
        "registered_agent": None,
        "ein": None,
        "org_name_official": None,
        "address": None,
        "city": None,
        "state": None,
        "zip": None,
        "county": None,
        "website": None,
        "registration_type": None,
        # Documents
        "annual_filings": [],
        "registration_documents": [],
        "other_documents": [],
        "detail_url": None,
    }

    try:
        # Step 1: Search
        log.info(f"Searching Charities Bureau API for: '{org_name}'")
        search_params = {"orgName": org_name}
        resp = requests.get(CHARITIES_SEARCH_URL, params=search_params, headers=REQUEST_HEADERS, timeout=30)
        resp.raise_for_status()
        search_data = resp.json()

        if not search_data.get("success") or not search_data.get("data"):
            result["status"] = "no_results"
            result["registration_status"] = "not_found"
            log.info("No results found in Charities Bureau")
            return result

        matches = search_data["data"]
        result["match_count"] = len(matches)
        result["matches"] = matches
        log.info(f"Found {len(matches)} match(es)")

        # Pick the best match (exact name match preferred)
        best = None
        for m in matches:
            if m.get("orgName", "").upper() == org_name.upper():
                best = m
                break
        if not best:
            best = matches[0]  # Take first result

        org_id = best.get("orgID")
        result["registration_number"] = org_id
        result["ein"] = best.get("ein")
        result["org_name_official"] = best.get("orgName")
        result["registration_type"] = best.get("regType")
        result["city"] = best.get("city")
        result["state"] = best.get("state")
        result["detail_url"] = f"{CHARITIES_WEB_BASE}/{org_id}"

        # Step 2: Get detail
        if org_id:
            log.info(f"Fetching detail for org ID: {org_id}")
            detail_resp = requests.get(
                CHARITIES_DETAIL_URL,
                params={"orgID": org_id},
                headers=REQUEST_HEADERS,
                timeout=30,
            )
            detail_resp.raise_for_status()
            detail_data = detail_resp.json()

            if detail_data.get("success") and detail_data.get("data"):
                d = detail_data["data"]
                result["address"] = d.get("address")
                result["city"] = d.get("city")
                result["state"] = d.get("state")
                result["zip"] = d.get("zip")
                result["county"] = d.get("county")
                result["website"] = d.get("url")
                result["org_name_official"] = d.get("orgName")
                result["ein"] = d.get("ein")
                result["registration_type"] = d.get("regType")

                # Parse documents
                docs = d.get("documents", {})

                for doc in docs.get("Annual Filing for Charitable Organizations", []):
                    filing = {
                        "title": doc.get("title"),
                        "fiscal_year_end": doc.get("fiscalYearEnd"),
                        "received": doc.get("received"),
                        "guid": doc.get("guid"),
                        "can_download": doc.get("canDownload", False),
                    }
                    if filing["can_download"]:
                        filing["download_url"] = f"{CHARITIES_DOC_URL}?guid={quote_plus(doc['guid'])}"
                    result["annual_filings"].append(filing)

                    # Track latest filing date (dates are MM/DD/YYYY, so parse for comparison)
                    if doc.get("received"):
                        try:
                            doc_date = datetime.strptime(doc["received"], "%m/%d/%Y")
                            if not result["last_filing_date"]:
                                result["last_filing_date"] = doc["received"]
                            else:
                                current = datetime.strptime(result["last_filing_date"], "%m/%d/%Y")
                                if doc_date > current:
                                    result["last_filing_date"] = doc["received"]
                        except ValueError:
                            pass

                for doc in docs.get("Registration Documents", []):
                    reg_doc = {
                        "title": doc.get("title"),
                        "received": doc.get("received"),
                        "guid": doc.get("guid"),
                        "can_download": doc.get("canDownload", False),
                    }
                    if reg_doc["can_download"]:
                        reg_doc["download_url"] = f"{CHARITIES_DOC_URL}?guid={quote_plus(doc['guid'])}"
                    result["registration_documents"].append(reg_doc)

                    # Registration date from earliest registration document
                    if doc.get("received"):
                        if not result["registration_date"] or doc["received"] < result["registration_date"]:
                            result["registration_date"] = doc["received"]

                for doc in docs.get("Other Filed Documents", []):
                    other = {
                        "title": doc.get("title"),
                        "received": doc.get("received"),
                        "guid": doc.get("guid"),
                        "can_download": doc.get("canDownload", False),
                    }
                    if other["can_download"]:
                        other["download_url"] = f"{CHARITIES_DOC_URL}?guid={quote_plus(doc['guid'])}"
                    result["other_documents"].append(other)

                log.info(
                    f"Detail: org={result['org_name_official']}, EIN={result['ein']}, "
                    f"filings={len(result['annual_filings'])}, reg_date={result['registration_date']}"
                )

        # Step 3: Download filings if requested
        if download_filings and output_dir:
            filings_dir = Path(output_dir) / "filings"
            filings_dir.mkdir(parents=True, exist_ok=True)
            downloaded = []

            for filing in result["annual_filings"] + result["other_documents"]:
                if not filing.get("can_download") or not filing.get("guid"):
                    continue
                try:
                    doc_resp = requests.get(
                        filing["download_url"],
                        headers={**REQUEST_HEADERS, "Accept": "application/pdf"},
                        timeout=60,
                    )
                    if doc_resp.status_code == 200:
                        safe_title = re.sub(r"[^a-zA-Z0-9_\-]", "_", filing.get("title", "doc"))
                        fy = filing.get("fiscal_year_end", filing.get("received", "unknown"))
                        filename = f"{safe_title}_{fy}.pdf"
                        filepath = filings_dir / filename
                        filepath.write_bytes(doc_resp.content)
                        downloaded.append(str(filepath))
                        log.info(f"Downloaded: {filepath}")
                except Exception as e:
                    log.warning(f"Failed to download {filing.get('title')}: {e}")

            result["downloaded_filings"] = downloaded

        # Determine registration status
        # If we found the org and it has recent filings, it's active
        if result["registration_number"]:
            result["registration_status"] = "active"  # Found in registry = registered
        else:
            result["registration_status"] = "not_found"

        result["status"] = "success"

    except requests.RequestException as e:
        log.error(f"HTTP error querying Charities Bureau: {e}")
        result["status"] = "error"
        result["error"] = str(e)
    except Exception as e:
        log.error(f"Error scraping Charities Bureau: {e}")
        traceback.print_exc()
        result["status"] = "error"
        result["error"] = str(e)

    return result


# ---------------------------------------------------------------------------
# NY Secretary of State -- Playwright (requires headed mode)
# ---------------------------------------------------------------------------


def scrape_sos(org_name: str, headless: bool = True) -> dict:
    """
    Scrape NY Secretary of State entity search.

    IMPORTANT: The SOS site (apps.dos.ny.gov) uses F5 Application Security Manager
    with aggressive bot detection. Headless browsers are typically blocked with
    ERR_CONNECTION_RESET. Options:
      - Use --headed mode (opens visible browser, passes JS challenge)
      - Run from a VPS with a real user session
      - Use Scrapling StealthySession with persistent cookies

    On failure, the scraper returns status="blocked" with instructions.
    """
    now = datetime.now(timezone.utc).isoformat()
    result = {
        "source": "ny_secretary_of_state",
        "source_url": SOS_SEARCH_URL,
        "org_name_searched": org_name,
        "scrape_timestamp": now,
        "status": "pending",
        "entity_id": None,
        "entity_type": None,
        "entity_status": None,
        "jurisdiction": None,
        "formation_date": None,
        "stated_purposes": None,
        "registered_agent": None,
        "office_address": None,
        "detail_url": None,
        "raw_results": [],
    }

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 900},
            )

            page = context.new_page()

            # Anti-detection
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.chrome = {runtime: {}};
            """)

            log.info(f"Navigating to NY SOS: {SOS_SEARCH_URL}")
            resp = page.goto(SOS_SEARCH_URL, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")

            # Wait for F5 JS challenge to resolve
            page.wait_for_timeout(8000)

            # Check if we actually loaded the page
            page_content = page.content()
            body_text = ""
            try:
                body_text = page.inner_text("body")
            except Exception:
                pass

            if len(page_content) < 500 or "can't be reached" in body_text or "ERR_CONNECTION" in body_text:
                log.warning("SOS site blocked the connection (F5 WAF). Try --headed mode.")
                result["status"] = "blocked"
                result["error"] = (
                    "SOS site uses F5 Application Security Manager which blocks headless browsers. "
                    "Rerun with --headed flag, or use Scrapling StealthySession with persistent cookies, "
                    "or scrape from a VPS with a real browser session."
                )

                # Take debug screenshot
                output_path = Path(DEFAULT_OUTPUT_DIR)
                output_path.mkdir(parents=True, exist_ok=True)
                try:
                    page.screenshot(path=str(output_path / "sos_blocked.png"), full_page=True)
                except Exception:
                    pass

                page.close()
                browser.close()
                return result

            # If we got past the WAF, try to find and fill the search form
            log.info("SOS page loaded, searching for form...")

            search_input = _find_sos_input(page)
            if not search_input:
                result["status"] = "error"
                result["error"] = "Could not find search input on SOS page"
                page.close()
                browser.close()
                return result

            search_input.click()
            search_input.fill(org_name)
            log.info(f"Entered search term: '{org_name}'")

            # Submit
            _submit_sos_search(page, search_input)
            page.wait_for_timeout(5000)

            # Extract results
            _extract_sos_results(page, result, org_name)

            if result["status"] == "pending":
                result["status"] = "success" if result["entity_id"] else "partial"

            # Screenshot
            output_path = Path(DEFAULT_OUTPUT_DIR)
            output_path.mkdir(parents=True, exist_ok=True)
            try:
                page.screenshot(path=str(output_path / "sos_final.png"), full_page=True)
            except Exception:
                pass

            page.close()
            browser.close()

    except PlaywrightTimeout as e:
        log.error(f"Timeout scraping SOS: {e}")
        result["status"] = "error"
        result["error"] = f"Timeout: {e}"
    except Exception as e:
        log.error(f"Error scraping SOS: {e}")
        traceback.print_exc()
        result["status"] = "error"
        result["error"] = str(e)

    return result


def _find_sos_input(page: Page):
    """Find the entity name search input on the SOS page."""
    selectors = [
        "#EntityName",
        'input[name="EntityName"]',
        'input[name*="entityname" i]',
        'input[placeholder*="entity" i]',
        'input[placeholder*="name" i]',
        'input[type="text"]',
    ]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return el
        except Exception:
            continue

    # Fallback: any visible text input
    for inp in page.query_selector_all("input"):
        try:
            if inp.is_visible() and inp.get_attribute("type") in ("text", "search", None, ""):
                return inp
        except Exception:
            continue
    return None


def _submit_sos_search(page: Page, search_input):
    """Submit the SOS search form."""
    submit_selectors = [
        'input[type="submit"]',
        'button[type="submit"]',
        'input[value="Search"]',
        'button:has-text("Search")',
    ]
    for sel in submit_selectors:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                return
        except Exception:
            continue
    search_input.press("Enter")


def _extract_sos_results(page: Page, result: dict, org_name: str) -> None:
    """Extract entity data from SOS search results or detail page."""
    body_text = page.inner_text("body")

    if "no records found" in body_text.lower() or "no entities found" in body_text.lower():
        result["status"] = "no_results"
        return

    # Try table extraction
    rows = page.query_selector_all("table tbody tr, table tr")
    for row in rows:
        row_text = row.inner_text().strip()
        if not row_text or row_text.lower().startswith("entity name"):
            continue
        result["raw_results"].append(row_text)

        cells = row.query_selector_all("td")
        if not cells:
            continue
        texts = [c.inner_text().strip() for c in cells]

        for text in texts:
            if re.match(r"^\d{5,}$", text):
                result["entity_id"] = result["entity_id"] or text
            elif text.lower() in ("active", "inactive", "dissolved"):
                result["entity_status"] = result["entity_status"] or text.lower()
            elif "not-for-profit" in text.lower() or "corporation" in text.lower():
                result["entity_type"] = result["entity_type"] or text
            elif re.match(r"^[A-Z]{2}$", text) or text.upper() == "NEW YORK":
                result["jurisdiction"] = result["jurisdiction"] or text
            elif re.match(r"\d{1,2}/\d{1,2}/\d{2,4}", text):
                result["formation_date"] = result["formation_date"] or text

    # Try regex on body text for detail page fields
    patterns = {
        "entity_id": r"(?:DOS\s*ID|Entity\s*ID)\s*[:\-]?\s*(\d+)",
        "entity_type": r"(?:Entity\s*Type|Type)\s*[:\-]?\s*([\w\s\-]+?)(?:\n|$)",
        "entity_status": r"(?:Current Entity Status|Status)\s*[:\-]?\s*(Active|Inactive|Dissolved)",
        "jurisdiction": r"(?:Jurisdiction)\s*[:\-]?\s*([A-Z]{2}|New York|[\w\s]+?)(?:\n|$)",
        "formation_date": r"(?:Initial DOS Filing Date|Date of Incorporation|Formation Date)\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{2,4})",
        "registered_agent": r"(?:DOS Process Agent|Registered Agent)\s*[:\-]?\s*(.+?)(?:\n\n|\n[A-Z]|$)",
        "office_address": r"(?:(?:Principal )?Office Address)\s*[:\-]?\s*(.+?)(?:\n\n|\n[A-Z]|$)",
    }
    for field, pattern in patterns.items():
        if not result.get(field):
            match = re.search(pattern, body_text, re.I | re.DOTALL)
            if match:
                result[field] = match.group(1).strip()[:300]


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


def run_scraper(
    org_name: str,
    output_dir: str,
    dry_run: bool = False,
    headless: bool = True,
    charities_only: bool = False,
    sos_only: bool = False,
    download_filings: bool = False,
) -> dict:
    """Run NY registration scrapers and return combined results."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    combined = {
        "org_name": org_name,
        "scrape_date": date.today().isoformat(),
        "charities_bureau": None,
        "secretary_of_state": None,
    }

    # --- NY Charities Bureau (REST API -- always works) ---
    if not sos_only:
        log.info("=" * 60)
        log.info("SCRAPING: NY Charities Bureau (REST API)")
        log.info("=" * 60)
        charities_data = scrape_charities_bureau(
            org_name,
            download_filings=download_filings,
            output_dir=output_dir,
        )
        combined["charities_bureau"] = charities_data

        charities_file = output_path / "ny_charities.json"
        with open(charities_file, "w") as f:
            json.dump(charities_data, f, indent=2, default=str)
        log.info(f"Wrote {charities_file}")
    else:
        charities_data = None

    # --- NY Secretary of State (Playwright -- may be blocked) ---
    if not charities_only:
        log.info("=" * 60)
        log.info("SCRAPING: NY Secretary of State (Playwright)")
        log.info("=" * 60)
        sos_data = scrape_sos(org_name, headless=headless)
        combined["secretary_of_state"] = sos_data

        sos_file = output_path / "ny_sos.json"
        with open(sos_file, "w") as f:
            json.dump(sos_data, f, indent=2, default=str)
        log.info(f"Wrote {sos_file}")
    else:
        sos_data = None

    # --- Database write ---
    write_to_db(charities_data or {}, sos_data or {}, org_name, dry_run=dry_run)

    # --- Write combined output ---
    combined_file = output_path / "ny_registration.json"
    with open(combined_file, "w") as f:
        json.dump(combined, f, indent=2, default=str)
    log.info(f"Wrote combined results to {combined_file}")

    return combined


def main():
    parser = argparse.ArgumentParser(
        description="Scrape NY Charities Bureau and Secretary of State for nonprofit registration data"
    )
    parser.add_argument("org_name", help="Organization name to search for")
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory for JSON files (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=os.environ.get("DRY_RUN", "").lower() == "true",
        help="Skip database writes",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run browser in headed mode (visible) -- needed for SOS anti-bot bypass",
    )
    parser.add_argument(
        "--charities-only",
        action="store_true",
        help="Only scrape NY Charities Bureau (REST API, no browser needed)",
    )
    parser.add_argument(
        "--sos-only",
        action="store_true",
        help="Only scrape NY Secretary of State (needs Playwright)",
    )
    parser.add_argument(
        "--download-filings",
        action="store_true",
        help="Download available CHAR500 and other filing PDFs",
    )

    args = parser.parse_args()

    log.info(f"Starting NY registration scraper for: '{args.org_name}'")
    log.info(f"Output directory: {args.output_dir}")
    log.info(f"Dry run: {args.dry_run}")

    results = run_scraper(
        org_name=args.org_name,
        output_dir=args.output_dir,
        dry_run=args.dry_run,
        headless=not args.headed,
        charities_only=args.charities_only,
        sos_only=args.sos_only,
        download_filings=args.download_filings,
    )

    # Print to stdout
    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
