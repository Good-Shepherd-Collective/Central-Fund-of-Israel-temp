"""Export data from Neon to CSV files in the targets/cfi/ directory structure."""

import csv
import os

import psycopg
from dotenv import load_dotenv

load_dotenv()

OUTPUT_DIR = "targets/cfi"


def get_db():
    return psycopg.connect(os.environ["DATABASE_URL_DEV"])


def export_query(conn, sql: str, filepath: str):
    """Run a query and write results to CSV."""
    cur = conn.execute(sql)
    cols = [desc.name for desc in cur.description]
    rows = cur.fetchall()
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(cols)
        writer.writerows(rows)
    print(f"  {filepath}: {len(rows)} rows")


def main():
    conn = get_db()
    print("Exporting CSVs from Neon...\n")

    # Financial summary
    export_query(conn, """
        SELECT tax_year, form_type, total_revenue, total_expenses, net_assets,
               total_grants_paid, total_foreign_grants, program_expense_ratio,
               foreign_grant_ratio, num_voting_members
        FROM filings ORDER BY tax_year
    """, f"{OUTPUT_DIR}/financials/summary.csv")

    # Foreign grants
    export_query(conn, """
        SELECT fg.tax_year, fg.recipient_name, fg.recipient_country,
               fg.grant_amount, fg.grant_purpose, fg.foundation_status_raw,
               fg.foundation_status_classified, fg.is_potential_violation
        FROM foreign_grants fg ORDER BY fg.tax_year, fg.grant_amount DESC
    """, f"{OUTPUT_DIR}/financials/grants_foreign.csv")

    # Officers
    export_query(conn, """
        SELECT tax_year, person_name, title, hours_per_week, compensation,
               is_officer, is_director, is_key_employee
        FROM officers ORDER BY tax_year, person_name
    """, f"{OUTPUT_DIR}/financials/officers.csv")

    # Schedule F summaries
    export_query(conn, """
        SELECT tax_year, region, expenditures, grant_amount,
               num_offices, num_employees, activities_description
        FROM schedule_f_summaries ORDER BY tax_year, region
    """, f"{OUTPUT_DIR}/financials/schedule_f_summaries.csv")

    # Narratives
    export_query(conn, """
        SELECT tax_year, field_name, field_section, narrative_text
        FROM filing_narratives ORDER BY tax_year, field_section
    """, f"{OUTPUT_DIR}/narratives/all_narratives.csv")

    # Collection log
    export_query(conn, """
        SELECT source_name, started_at, finished_at, records_added,
               records_updated, status
        FROM ingestion_log ORDER BY started_at
    """, f"{OUTPUT_DIR}/collection_log.csv")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
