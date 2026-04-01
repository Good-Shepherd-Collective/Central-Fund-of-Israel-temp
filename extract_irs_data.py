"""
Extract CFI data from IRS-DB (DuckDB on infomaniak2) and load into Neon Postgres.

Usage:
    python extract_irs_data.py              # Extract and load all data
    python extract_irs_data.py --dry-run    # Show what would be extracted without loading

Data flow:
    IRS-DB (DuckDB @ infomaniak2:/data/irs990/990_grants.duckdb)
    → SSH query → JSON
    → Transform (DuckDB field names → Neon schema)
    → Load into gsc-ag-complaint Neon project

DuckDB → Neon Field Mapping:
    filings_core + filings_990 → filings
    foreign_activities           → schedule_f_summaries
    grants_paid (foreign)        → foreign_grants
    officers                     → officers
    supplemental_info            → filing_narratives
"""

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import psycopg

# Force unbuffered output
print = lambda *args, **kwargs: __builtins__["print"](*args, **{**kwargs, "flush": True}) if isinstance(__builtins__, dict) else __import__("builtins").print(*args, **{**kwargs, "flush": True})

CFI_EIN = "132992985"
CFI_EIN_FORMATTED = "13-2992985"

DRY_RUN = "--dry-run" in sys.argv


def query_duckdb(sql: str) -> list[dict]:
    """Execute a read-only query against IRS-DB via SSH and return rows as dicts."""
    import csv
    import io
    # Use CSV mode — much faster than JSON for large result sets
    cmd = f"""ssh infomaniak2 "~/.local/bin/duckdb -readonly -csv /data/irs990/990_grants.duckdb \\"{sql}\\""  """
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        print(f"ERROR: {result.stderr}")
        return []
    output = result.stdout.strip()
    if not output:
        return []
    reader = csv.DictReader(io.StringIO(output))
    rows = []
    for row in reader:
        # Convert numeric-looking values
        cleaned = {}
        for k, v in row.items():
            if v == "" or v == "NULL":
                cleaned[k] = None
            else:
                try:
                    if "." in v:
                        cleaned[k] = float(v)
                    else:
                        cleaned[k] = int(v)
                except (ValueError, TypeError):
                    cleaned[k] = v
            # Handle booleans from DuckDB CSV
            if v in ("true", "True"):
                cleaned[k] = True
            elif v in ("false", "False"):
                cleaned[k] = False
        rows.append(cleaned)
    return rows


def get_db():
    """Get Neon dev branch connection."""
    url = os.environ.get("DATABASE_URL_DEV")
    if not url:
        from dotenv import load_dotenv
        load_dotenv()
        url = os.environ["DATABASE_URL_DEV"]
    return psycopg.connect(url)


def create_ingestion_log(conn) -> str:
    """Create an ingestion log entry and return its UUID."""
    metadata = json.dumps({"ein": CFI_EIN, "source": "infomaniak2:/data/irs990/990_grants.duckdb"})
    row = conn.execute(
        """INSERT INTO ingestion_log (source_name, metadata)
           VALUES ('irs-db-extract', %s::jsonb)
           RETURNING id""",
        (metadata,),
    ).fetchone()
    return str(row[0])


def load_us_entity(conn, ingestion_id: str) -> str:
    """Insert CFI as a US entity and return its UUID."""
    rows = query_duckdb(
        f"SELECT DISTINCT name FROM organizations WHERE ein='{CFI_EIN}'"
    )
    org_name = rows[0]["name"] if rows else "CENTRAL FUND OF ISRAEL"

    # Get available tax years
    year_rows = query_duckdb(
        f"SELECT DISTINCT tax_year FROM filings_core WHERE ein='{CFI_EIN}' ORDER BY tax_year"
    )
    years = [r["tax_year"] for r in year_rows]

    print(f"  US Entity: {org_name} (EIN {CFI_EIN_FORMATTED})")
    print(f"  Filing years: {years}")

    if DRY_RUN:
        return "dry-run-uuid"

    row = conn.execute(
        """INSERT INTO us_entities (source_id, source_url, org_name, ein, aliases,
                website_url, filing_type, is_primary_target, collection_status,
                filing_years_available, ingestion_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::uuid)
           ON CONFLICT (ein) DO UPDATE SET
                filing_years_available = EXCLUDED.filing_years_available,
                updated_at = now()
           RETURNING id""",
        (
            CFI_EIN_FORMATTED,
            f"https://projects.propublica.org/nonprofits/organizations/{CFI_EIN}",
            org_name,
            CFI_EIN_FORMATTED,
            ["CFI", "Central Fund"],
            "https://www.centralfundofisrael.org",
            "990",
            True,
            "in_progress",
            years,
            ingestion_id,
        ),
    ).fetchone()
    return str(row[0])


def load_filings(conn, us_entity_id: str, ingestion_id: str) -> dict[int, str]:
    """Load filing records from filings_core + filings_990. Returns {tax_year: filing_uuid}."""
    rows = query_duckdb(f"""
        SELECT fc.ein, fc.tax_year, fc.form_type, fc.object_id,
               fc.total_revenue, fc.total_expenses, fc.net_assets,
               f9.grants_paid_total, f9.num_voting_members
        FROM filings_core fc
        LEFT JOIN filings_990 f9 ON fc.ein = f9.ein AND fc.tax_year = f9.tax_year
        WHERE fc.ein = '{CFI_EIN}'
        ORDER BY fc.tax_year
    """)

    print(f"  Filings: {len(rows)} years")
    if DRY_RUN:
        return {r["tax_year"]: "dry-run" for r in rows}

    filing_map = {}
    for r in rows:
        tax_year = r["tax_year"]
        total_revenue = r.get("total_revenue")
        total_expenses = r.get("total_expenses")
        grants_paid = r.get("grants_paid_total")

        # Calculate ratios
        program_ratio = None
        foreign_ratio = None

        source_id = f"{CFI_EIN_FORMATTED}:{tax_year}:{r['form_type']}"
        source_url = f"https://projects.propublica.org/nonprofits/download-xml?object_id={r['object_id']}" if r.get("object_id") else None

        row = conn.execute(
            """INSERT INTO filings (source_id, source_url, us_entity_id, tax_year,
                    form_type, total_revenue, total_expenses, net_assets,
                    total_grants_paid, num_voting_members, xml_object_id, ingestion_id)
               VALUES (%s, %s, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s::uuid)
               ON CONFLICT (source_id) DO UPDATE SET
                    total_revenue = EXCLUDED.total_revenue,
                    total_expenses = EXCLUDED.total_expenses,
                    updated_at = now()
               RETURNING id""",
            (
                source_id, source_url, us_entity_id, tax_year,
                r["form_type"], total_revenue, total_expenses,
                r.get("net_assets"), grants_paid,
                r.get("num_voting_members"), r.get("object_id"),
                ingestion_id,
            ),
        ).fetchone()
        filing_map[tax_year] = str(row[0])

    return filing_map


def load_schedule_f(conn, us_entity_id: str, filing_map: dict, ingestion_id: str):
    """Load Schedule F regional summaries from foreign_activities table."""
    rows = query_duckdb(f"""
        SELECT tax_year, region, num_offices, num_employees,
               activity_type, expenditures
        FROM foreign_activities
        WHERE ein = '{CFI_EIN}'
        ORDER BY tax_year, region
    """)

    print(f"  Schedule F summaries: {len(rows)} rows")
    if DRY_RUN:
        return

    for r in rows:
        tax_year = r["tax_year"]
        filing_id = filing_map.get(tax_year)
        if not filing_id:
            continue

        source_id = f"{CFI_EIN_FORMATTED}:{tax_year}:sched_f:{r['region']}:{r['expenditures']}"
        conn.execute(
            """INSERT INTO schedule_f_summaries (source_id, filing_id, us_entity_id,
                    tax_year, region, expenditures, num_offices, num_employees,
                    activities_description, ingestion_id)
               VALUES (%s, %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s::uuid)
               ON CONFLICT DO NOTHING""",
            (
                source_id, filing_id, us_entity_id, tax_year,
                r["region"], r["expenditures"], r.get("num_offices"),
                r.get("num_employees"), r.get("activity_type"),
                ingestion_id,
            ),
        )


def load_foreign_grants(conn, us_entity_id: str, filing_map: dict, ingestion_id: str):
    """Load individual foreign grant records from grants_paid (Schedule F check/wire entries)."""
    rows = query_duckdb(f"""
        SELECT tax_year, recipient_name, recipient_country, amount,
               purpose, grant_type, foundation_status, relationship
        FROM grants_paid
        WHERE ein = '{CFI_EIN}'
          AND (recipient_country NOT IN ('US') OR recipient_country IS NULL)
          AND grant_type LIKE 'schedule_f%%'
        ORDER BY tax_year, amount DESC
    """)

    print(f"  Foreign grants: {len(rows)} individual grant records")
    if DRY_RUN:
        return

    for i, r in enumerate(rows):
        tax_year = r["tax_year"]
        filing_id = filing_map.get(tax_year)
        if not filing_id:
            continue

        source_id = f"{CFI_EIN_FORMATTED}:{tax_year}:fg:{hashlib.sha256(f'{r.get(\"recipient_name\",\"\")}{r.get(\"amount\",\"\")}{r.get(\"recipient_country\",\"\")}'.encode()).hexdigest()[:12]}"
        conn.execute(
            """INSERT INTO foreign_grants (source_id, filing_id, us_entity_id,
                    tax_year, recipient_name, recipient_country,
                    foundation_status_raw, grant_amount, grant_purpose,
                    relationship_txt, ingestion_id)
               VALUES (%s, %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s::uuid)
               ON CONFLICT DO NOTHING""",
            (
                source_id, filing_id, us_entity_id, tax_year,
                r.get("recipient_name"), r.get("recipient_country"),
                r.get("foundation_status"), r.get("amount"),
                r.get("purpose"), r.get("relationship"),
                ingestion_id,
            ),
        )


def load_officers(conn, us_entity_id: str, filing_map: dict, ingestion_id: str):
    """Load officers from IRS-DB officers table."""
    rows = query_duckdb(f"""
        SELECT tax_year, person_name, title, hours_per_week,
               compensation, is_officer, is_director, is_trustee,
               is_key_employee
        FROM officers
        WHERE ein = '{CFI_EIN}'
        ORDER BY tax_year, person_name
    """)

    print(f"  Officers: {len(rows)} person-year records")
    if DRY_RUN:
        return

    for r in rows:
        tax_year = r["tax_year"]
        filing_id = filing_map.get(tax_year)
        if not filing_id:
            continue

        source_id = f"{CFI_EIN_FORMATTED}:{tax_year}:officer:{r['person_name']}:{r.get('title', '')}"
        conn.execute(
            """INSERT INTO officers (source_id, filing_id, us_entity_id,
                    tax_year, person_name, title, hours_per_week,
                    compensation, is_officer, is_director, is_key_employee,
                    ingestion_id)
               VALUES (%s, %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s::uuid)
               ON CONFLICT DO NOTHING""",
            (
                source_id, filing_id, us_entity_id, tax_year,
                r["person_name"], r.get("title"), r.get("hours_per_week"),
                r.get("compensation"), r.get("is_officer", False),
                r.get("is_director", False), r.get("is_key_employee", False),
                ingestion_id,
            ),
        )


def load_narratives(conn, us_entity_id: str, filing_map: dict, ingestion_id: str):
    """Load Schedule O supplemental info as filing narratives."""
    rows = query_duckdb(f"""
        SELECT tax_year, form_line_ref, explanation
        FROM supplemental_info
        WHERE ein = '{CFI_EIN}'
        ORDER BY tax_year, form_line_ref
    """)

    print(f"  Narratives: {len(rows)} Schedule O entries")
    if DRY_RUN:
        return

    for r in rows:
        tax_year = r["tax_year"]
        filing_id = filing_map.get(tax_year)
        if not filing_id:
            continue

        form_line = r.get("form_line_ref", "unknown")

        # Map form_line_ref to field_name and field_section
        if "Part III" in form_line:
            field_name = "part_iii_programs"
            field_section = "Part III"
        elif "Part VI" in form_line:
            field_name = "part_vi_governance"
            field_section = "Part VI"
        elif "Schedule F" in form_line:
            field_name = "schedule_f_description"
            field_section = "Schedule F"
        else:
            field_name = form_line.lower().replace(" ", "_").replace(",", "")
            field_section = "Schedule O"

        source_id = f"{CFI_EIN_FORMATTED}:{tax_year}:narrative:{form_line}"
        conn.execute(
            """INSERT INTO filing_narratives (source_id, filing_id, us_entity_id,
                    tax_year, field_name, field_section, narrative_text, ingestion_id)
               VALUES (%s, %s::uuid, %s::uuid, %s, %s, %s, %s, %s::uuid)
               ON CONFLICT DO NOTHING""",
            (
                source_id, filing_id, us_entity_id, tax_year,
                field_name, field_section, r["explanation"],
                ingestion_id,
            ),
        )


def finalize_ingestion(conn, ingestion_id: str, counts: dict):
    """Update ingestion log with final counts."""
    if DRY_RUN:
        return
    conn.execute(
        """UPDATE ingestion_log SET
                finished_at = now(),
                records_added = %s,
                status = 'success'
           WHERE id = %s::uuid""",
        (counts.get("total", 0), ingestion_id),
    )


def main():
    print(f"{'[DRY RUN] ' if DRY_RUN else ''}Extracting CFI data from IRS-DB → Neon")
    print(f"  EIN: {CFI_EIN_FORMATTED}")
    print()

    if DRY_RUN:
        load_us_entity(None, None)
        filing_map = {}
        rows = query_duckdb(f"SELECT DISTINCT tax_year FROM filings_core WHERE ein='{CFI_EIN}' ORDER BY tax_year")
        for r in rows:
            filing_map[r["tax_year"]] = "dry-run"
        load_filings(None, "dry-run", None)
        load_schedule_f(None, "dry-run", filing_map, None)
        load_foreign_grants(None, "dry-run", filing_map, None)
        load_officers(None, "dry-run", filing_map, None)
        load_narratives(None, "dry-run", filing_map, None)
        print("\n[DRY RUN] No data loaded.")
        return

    conn = get_db()
    try:
        ingestion_id = create_ingestion_log(conn)
        print(f"  Ingestion ID: {ingestion_id}")
        print()

        us_entity_id = load_us_entity(conn, ingestion_id)
        filing_map = load_filings(conn, us_entity_id, ingestion_id)
        load_schedule_f(conn, us_entity_id, filing_map, ingestion_id)
        load_foreign_grants(conn, us_entity_id, filing_map, ingestion_id)
        load_officers(conn, us_entity_id, filing_map, ingestion_id)
        load_narratives(conn, us_entity_id, filing_map, ingestion_id)

        # Count what we loaded
        total = conn.execute("SELECT count(*) FROM filings WHERE us_entity_id = %s::uuid", (us_entity_id,)).fetchone()[0]
        total += conn.execute("SELECT count(*) FROM schedule_f_summaries WHERE us_entity_id = %s::uuid", (us_entity_id,)).fetchone()[0]
        total += conn.execute("SELECT count(*) FROM foreign_grants WHERE us_entity_id = %s::uuid", (us_entity_id,)).fetchone()[0]
        total += conn.execute("SELECT count(*) FROM officers WHERE us_entity_id = %s::uuid", (us_entity_id,)).fetchone()[0]
        total += conn.execute("SELECT count(*) FROM filing_narratives WHERE us_entity_id = %s::uuid", (us_entity_id,)).fetchone()[0]

        finalize_ingestion(conn, ingestion_id, {"total": total})
        conn.commit()

        print(f"\nLoaded {total} total records into Neon (dev branch)")
    except Exception as e:
        conn.rollback()
        print(f"\nERROR: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
