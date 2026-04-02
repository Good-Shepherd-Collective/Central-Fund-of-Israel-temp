# Central Fund of Israel — Data Collection

## Purpose
Data collection repo for Central Fund of Israel (CFI), a US 501(c)(3) pass-through entity that transfers tax-deductible donations to Israeli organizations. CFI is the primary complaint target for the AG complaint pipeline.

Implements the `us-passthrough-collection.md` spec from `~/Desktop/repos/data/ag-complaint-pipeline/specs/`.

## Entity Profile
- **EIN:** 13-2992985
- **Filing type:** 990 (public charity — Track B)
- **Address:** 429 Central Avenue, c/o Jmark Interiors Inc., Cedarhurst, NY 11516
- **Website:** centralfundofisrael.org (Wix — use `domcontentloaded` not `networkidle`)
- **Key people:** Jay Marcus (President, $115-150k), Dr Linda Kalish Marcus (Secretary, unpaid), Yehuda Marcus (Director) — family operation (married couple + son)
- **Annual revenue:** $39M (2019) → $109M (2024) → $97M (2025)
- **Foreign grants:** 93-97% of total grants go to Middle East recipients
- **NY Charities Bureau:** Registered since 04/01/1980, reg# 02-55-79, active
- **NY SOS:** NOT FOUND in Division of Corporations database — significant compliance finding
- **Social media:** No accounts identified

## Infrastructure

### Neon Postgres (`gsc-ag-complaint`)
- **Project:** `holy-flower-56830004`, region: `us-east-2`
- **Branches:** `main` (production), `dev` (testing/migrations)
- **Schema:** 21 tables + 3 views + pgvector + pg_trgm + full-text search
- **Key tables:** `us_entities`, `filings`, `foreign_grants`, `officers`, `filing_narratives`, `web_pages`, `solicitation_claims`, `findings`, `documents`, `foia_requests`
- **Connection strings:** `.env` (DATABASE_URL, DATABASE_URL_DEV)

### Cloudflare R2 (`ag-complaint-evidence`)
- **Account:** Defund Racism (info@defundracism.org)
- **Bucket:** `ag-complaint-evidence`
- **Path convention:** `{entity-slug}/web/{capture-slug}/{filename}`
- **Purpose:** Immutable evidence storage — WARC archives, screenshots, OTS proofs
- **Credentials:** `.env` (R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY)

### IRS-DB (DuckDB)
- **Location:** `infomaniak2:/data/irs990/990_grants.duckdb` (~25-30GB, ~7M+ filings)
- **Repo:** `~/Desktop/repos/data/financial/IRS-DB/`
- **Access:** `ssh infomaniak2 "~/.local/bin/duckdb -readonly -csv /data/irs990/990_grants.duckdb '<SQL>'"`
- **Key mapping:** See `us-passthrough-collection.md` §3.1 for DuckDB→Neon field mapping

### OpenTimestamps (Bitcoin proof automation)
- **LaunchAgent:** `com.gsc.ots-upgrade` — runs daily at 6 AM + on login
- **Script:** `scrapers/ots_upgrade.py`
- **Cost:** Free (public calendar servers)

### Proxy Pool
- **Repo:** `~/Desktop/repos/tools/proxies/`
- **Usage:** `--proxy socks5://127.0.0.1:9050` (Playwright needs `socks5://` not `socks5h://`)

## Key Scripts

### Data Extraction
- `extract_irs_data.py` — Pulls CFI data from IRS-DB → loads into Neon dev branch
- `export_csvs.py` — Exports Neon data to CSV files in `targets/cfi/`

### Scrapers (`scrapers/`)
- `forensic_capture.py` — Core evidence engine: SHA-256, WARC, OTS, Wayback, chain of custody
- `website_crawl.py` — Playwright BFS crawler with forensic capture, R2 upload, Neon ingestion
- `wayback_historical.py` — CDX API historical snapshot collection with Neon integration
- `r2_upload.py` — Cloudflare R2 S3-compatible upload module
- `ny_registration.py` — NY Charities Bureau REST API + SOS Playwright scraper
- `ots_upgrade.py` — Bitcoin proof upgrade automation (daily cron)
- `config.py` — Shared configuration (paths, credentials, proxy, capture settings)

## Forensic Capture Protocol

Every web page capture meets FRE 901(a) evidentiary standards:

| Component | Purpose | Legal Basis |
|-----------|---------|-------------|
| SHA-256 hash | Integrity proof | FRE 901(a) |
| WARC archive | Complete record (ISO 28500) | Archival standard |
| Full-page screenshot | Visual evidence | FRE 1001-1008 |
| Wayback Machine | Third-party neutral archive | *Telewizja Polska* (2004) |
| OpenTimestamps | Bitcoin-anchored timestamp | FRE 902(13)/(14) |
| Chain-of-custody log | Append-only provenance | FRE 901(b)(9) |

Data flow: Playwright → capture → hash → WARC → R2 upload → Neon insert → Wayback submit → OTS proof → custody log

## Directory Structure
```
Central-Fund-of-Israel/
├── CLAUDE.md
├── .env                           # Neon + R2 credentials (not committed)
├── .env.example
├── requirements.txt
├── extract_irs_data.py            # IRS-DB → Neon extraction
├── export_csvs.py                 # Neon → CSV export
├── scrapers/
│   ├── __init__.py
│   ├── config.py                  # Shared configuration
│   ├── forensic_capture.py        # Core evidence capture engine
│   ├── website_crawl.py           # Playwright BFS crawler
│   ├── wayback_historical.py      # Wayback CDX historical collection
│   ├── r2_upload.py               # Cloudflare R2 upload
│   ├── ny_registration.py         # NY Charities Bureau + SOS
│   └── ots_upgrade.py             # Bitcoin proof automation
├── tests/
│   ├── __init__.py
│   ├── test_forensic_capture.py   # 8 tests
│   ├── test_website_crawl.py      # 10 tests
│   └── test_r2_upload.py          # 3 tests
├── funding-connections/
│   ├── tracker.md                 # Master tracker for all org research
│   ├── im-tirtzu/captures/        # Forensic capture of donation page
│   ├── honenu/captures/           # Forensic capture of donation page
│   ├── kohelet/captures/          # Forensic capture of donation page
│   ├── nachala/captures/          # Forensic capture of donation page
│   └── shurat-hadin/captures/     # Forensic capture of donation page
├── targets/cfi/
│   ├── metadata.json              # Entity profile + collection status
│   ├── financials/                # CSV exports from Neon
│   ├── narratives/                # Schedule O exports
│   ├── web/
│   │   ├── crawl_index.json       # Crawl session summary
│   │   ├── chain_of_custody.jsonl # Forensic evidence log (NEVER modify)
│   │   ├── captures/              # Per-URL evidence packages
│   │   ├── ots/                   # OpenTimestamps .ots proof files
│   │   └── wayback/               # Historical snapshots by page
│   ├── registration/              # NY Charities + SOS data
│   └── external/                  # Media, legal, watchdog (pending)
├── docs/plans/                    # Implementation plans
├── logs/                          # OTS upgrade logs
├── funding-connections.md         # Research brief for funding connection work
├── cfi_orgs_with_ids.csv          # 54 orgs listed on CFI website (with registry IDs, Hebrew names, West Bank flag)
└── CFI-Facts.md                   # Website facts + banking details
```

## Collection Status

| Source | Status | Records |
|--------|--------|---------|
| IRS filings | Complete | 7 filings, 1,677 grants, 47 officers, 47 narratives |
| NY Charities Bureau | Complete | Active since 1980 |
| NY Secretary of State | Complete | NOT FOUND (significant finding) |
| Website (forensic) | Complete | 6 pages with full evidence chain |
| Solicitation claims | Complete | 20 claims from 3 pages |
| Wayback historical | Partial | 38 snapshots (FAQ + About); Who We Help pending |
| Findings | Complete | 8 findings (3 critical) |
| Funding connections | Partial | 5 Grade A (forensic capture), 9 Grade C/D (media only) |
| Social media | N/A | No accounts found |
| External sources | Pending | Spec TODO at ag-complaint-pipeline/specs/external-sources-todo.md |

## Funding Connections (Israeli Recipients)

Tracker: `funding-connections/tracker.md`. Research brief: `funding-connections.md`.

### Grade A — Fiscal sponsor (primary-source donation pages, forensically captured)
| Org | Registry | Evidence | On CFI website? |
|-----|----------|----------|-----------------|
| Im Tirtzu | 580471662 | Donation page with CFI bank details + EIN | No |
| Honenu | 580386571 | "Make checks out to Central Fund of Israel" + EIN | No |
| Kohelet Policy Forum | 580553915 | CFI bank account with earmark notation | No |
| Nachala Movement | 580554459 | Checks to Jay Marcus at CFI, earmarked | No |
| Shurat HaDin | 580402469 | CFI earmark code SHD834 + EIN | Yes ("Education") |

### Grade C/D — Media evidence only (9 orgs)
Ateret Cohanim, Bet Knesset Kfar Tapuach, Elad/Ir David, Gush Etzion Foundation, Israel Land Fund, Lehava, Od Yosef Chai Yeshiva, Regavim, Women in Green

### Not CFI (different fiscal sponsor or own 501c3)
- Ad Kan → America Gives, Inc. (EIN 26-3383926)
- Elad/Ir David → own US 501(c)(3)
- Ateret Cohanim → own US 501(c)(3) (American Friends of Ateret Cohanim)
- TPS/Tazpit → P.E.F. Israel Endowment Funds

### Priority targets for webscraping & content
1. **Im Tirtzu** — Political warfare, Grade A CFI evidence
2. **Honenu** — Legal defense for convicted attackers, Grade A CFI evidence
3. **Nachala Movement** — Illegal outpost construction (Daniella Weiss), Grade A, names Jay Marcus
4. **Regavim** — Demolition advocacy, on CFI website, already profiled
5. **Shurat HaDin** — Legal warfare ("Israel Law Center"), dedicated CFI earmark SHD834, $8.5M revenue

### CFI Banking Details (from donation pages)
- **Bank:** Dime Community Bank, 898 Veterans Memorial Highway, Hauppauge NY 11788
- **Account Title:** CENTRAL FUND OF ISRAEL
- **Account:** 5000221843, Routing: 021406667
- **Address:** 461 Central Ave, Cedarhurst NY 11516 (also PO Box 491, Woodmere NY 11598)
- **SWIFT:** BHNBUS3B (international wires)
- **Wire minimum:** $1,000 (per Im Tirtzu page)

## Track B Caveat
CFI files Form 990 (public charity), not 990-PF. Schedule F provides individual grant **amounts** but NOT recipient names in the XML — names are only in the PDF "attached listing." To identify which Israeli orgs receive funds: FOIA request or PDF download + OCR. Track via `foia_requests` table.

## Specs
All specs live in `~/Desktop/repos/data/ag-complaint-pipeline/specs/`:
- `us-passthrough-collection.md` — What to collect for US entities (comprehensive, updated 2026-04-01)
- `israeli-org-profiler.md` — How to profile Israeli recipient orgs
- `database-schema.sql` — Canonical Neon schema (21 tables)
- `database-schema.md` — Schema design rationale + lessons learned
- `external-sources-todo.md` — Design outline for external sources collection

## Shared Dependencies
- Forensic capture module: canonical implementation in `ag-complaint-pipeline/pipeline/shared/`
- Install: `pip install -e ~/Desktop/repos/data/ag-complaint-pipeline`
- Import: `from pipeline.shared.forensic_capture import capture_page`
- CFI-specific config (`scrapers/config.py`) subclasses the shared `CaptureConfig` with CFI defaults (entity_slug="cfi", output_dir=targets/cfi/web, upload_to_r2=True, R2Config auto-loading)
- CFI `scrapers/forensic_capture.py` re-exports shared functions and adds `_insert_documents` for Neon document table writes

## Conventions
- Follow datahub playbook at `~/Desktop/repos/data/datahub/docs/playbook.md`
- Conventional commits: `type(scope): description`
- API keys from `~/Desktop/repos/keys.db`, never committed
- psycopg3 (`psycopg[binary]`) for Neon connections
- 21 tests: `python -m pytest tests/ -v`
