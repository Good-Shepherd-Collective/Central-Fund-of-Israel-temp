# CFI Funding Connection Research Brief

**Purpose:** Establish primary-source evidence linking CFI to Israeli recipient organizations that are not listed on CFI's public website. The goal is Grade A evidence (org's own words/pages) proving the fiscal sponsorship relationship — donation pages, thank-you letters, tax receipt instructions, etc.

**Why this matters:** CFI's 990 anonymizes all recipients as `[Foreign Grant - middle east/north africa]`. Their website lists 55 orgs — mostly benign community/medical/education. But investigative reporting identifies 14+ additional far-right organizations receiving CFI funding that CFI does not publicly list. Proving these connections with primary sources is critical for Theory 5 (conduit) and Theory 8 (solicitation fraud).

---

## Current State

### What we have
- **CFI website list:** 55 orgs in `cfi_orgs_with_ids.csv` with Israeli registry numbers
- **Far-right NGO list:** 15 orgs in `~/Desktop/repos/data/financial/israeli-nonprofit-data/far_right_israeli_ngos.csv`
- **Neon DB:** 11 orgs in `israeli_entities`, 11 in `funding_relationships` — all tagged "Identified from media evidence extraction" (Grade C/D)
- **Media facts:** 332 `funding_link` facts from articles — third-party reporting, not primary source
- **One primary source:** Tapuach fiscal sponsorship letter (OCR'd) — direct conduit evidence, already in `targets/cfi/external/`

### What we need
Primary-source evidence for each org showing CFI acts as their fiscal sponsor or donation conduit. In priority order:

1. **Donation pages** — Israeli org website says "donate through Central Fund of Israel" or "tax-deductible via CFI"
2. **Thank-you / acknowledgment pages** — "Thank you for your donation through CFI"
3. **Fiscal sponsorship letters** — like the Tapuach letter we already have
4. **Tax receipt instructions** — "For US tax deductions, donate through CFI"
5. **Event pages** — joint fundraising events, galas listing CFI as fiscal sponsor
6. **Wayback Machine snapshots** — historical versions of the above (orgs may have removed CFI references)

---

## Target Organizations

### Priority 1: Known CFI-funded, NOT on CFI website (14 orgs)

These are the concealment evidence — CFI funds them but doesn't list them publicly.

| Org | Hebrew | Registry # | Category | What to search |
|-----|--------|-----------|----------|---------------|
| Im Tirtzu | אם תרצו | 580471662 | Legal warfare | imti.org.il, en.imti.org.il |
| Honenu | חוננו | 580386571 | Legal warfare | honenu.org.il, honenu.org |
| Lehava | להב"ה | (none — operates via proxies) | Legal warfare | Check חמלה, הקרן להצלת עם ישראל |
| Ad Kan | עד כאן | 580615987 | Legal warfare | ad-kan.org.il |
| Kohelet Policy Forum | פורום קהלת | 580553915 | Legal warfare | en.kohelet.org.il |
| Amana | אמנה | 570025742 | Settlement construction | amana.co.il |
| Elad / Ir David Foundation | אלע"ד | 580108660 | Settlement construction | cityofdavid.org.il, elad.org.il |
| Ateret Cohanim | עטרת כהנים | 580008126 | Settlement construction | ateretcohanim.org |
| Yesha Council | מועצת יש"ע | 580186492 | Settlement construction | myesha.org.il |
| Nachala Movement | נחלה | 580554459 | Annexation / vanguard | nachala.org.il |
| Hashomer Yosh | שומר יו"ש | 580575629 | Annexation / vanguard | hashomeryosh.org |
| Tzav 9 | צו 9 | (none — unregistered) | Annexation / vanguard | givechak.co.il |
| Komemiyut | קוממיות | 580151546 | Annexation / vanguard | komemiyut.org.il |
| Women in Green | נשים בירוק | 580231207 | Annexation / vanguard | womeningreen.org |

### Priority 2: On CFI website but warranting investigation

These are listed on CFI's website but their classification may obscure their actual activities:

| Org | CFI Category | Actual concern |
|-----|-------------|----------------|
| Shurat HaDin | Education | Israeli legal warfare org ("Israel Law Center") |
| Truth About Israel | Education | Hasbara/propaganda |
| TPS / Tazpit | Education | Pro-settlement media agency |
| Temple Mount Heritage Foundation | (uncategorized) | Politically sensitive |
| Gush Etzion Foundation | Social & Humanitarian | Settlement region |
| Regavim | (uncategorized) | Demolition advocacy — already profiled |

---

## Research Method

For each target org:

### Step 1: Check current website
- Visit the org's English and Hebrew websites
- Search for: donate, support, contribute, tax deductible, 501(c)(3), Central Fund, CFI
- Check footer, header, sidebar for donation links
- Check "About" / "Partners" / "Supporters" pages

### Step 2: Check Wayback Machine
- Search `web.archive.org/web/*/[org-domain]/donate*` and `*/support*`
- Many orgs have removed CFI references after media scrutiny — historical captures are more valuable
- Also search for `web.archive.org/web/*/[org-domain]/*central+fund*`

### Step 3: Search for donation instructions
- Google: `"[org name]" "Central Fund of Israel" donate`
- Google: `"[org name]" "tax deductible" Israel`
- Google: `site:[org-domain] "Central Fund"`
- Check cached/archived versions of results

### Step 4: Check "Friends of" entities
Some orgs have US-based "Friends of" entities that route through CFI:
- American Friends of Ateret Cohanim
- American Friends of the Israel Land Fund
- Search ProPublica Nonprofit Explorer for "Friends of [org name]"

---

## How to Store Evidence

### Database Schema

**`israeli_entities`** — one row per org:
```sql
INSERT INTO israeli_entities (org_name, org_name_hebrew, amutah_number, primary_activity)
VALUES ('Im Tirtzu', 'אם תרצו', '580471662', 'political_campaign');
```

**`funding_relationships`** — one row per CFI → org link:
```sql
INSERT INTO funding_relationships (
    source_id, us_entity_id, israeli_entity_id,
    relationship_type, notes
) VALUES (
    '13-2992985:im-tirtzu',
    '6c473846-cfd6-4cf2-b161-1f23abd49f58',  -- CFI's UUID
    '<israeli_entity_uuid>',
    'fiscal_sponsorship',  -- or 'grant', 'pass_through'
    'Donation page at imti.org.il/donate directs US donors to CFI (captured 2026-04-02)'
);
```

**`documents`** — one row per captured evidence file:
```sql
INSERT INTO documents (
    source_id, source_url, us_entity_id, israeli_entity_id,
    document_type, file_path, capture_date, notes
) VALUES (
    'donation-page:im-tirtzu:2026-04-02',
    'https://imti.org.il/en/donate',
    '6c473846-cfd6-4cf2-b161-1f23abd49f58',
    '<israeli_entity_uuid>',
    'website_screenshot',  -- or 'website_html', 'wayback_snapshot'
    'cfi/external/im-tirtzu/donate_20260402.png',
    now(),
    'Donation page explicitly directs US donors to CFI for tax deductions'
);
```

**`relationship_type` values to use:**
- `fiscal_sponsorship` — org's donation page explicitly routes through CFI (strongest)
- `pass_through` — evidence CFI passes donations directly to org without discretion
- `grant` — CFI grants money to org (standard charitable relationship)
- `earmarked` — donors earmark CFI donations for this specific org

### Tag the evidence source

When updating `funding_relationships.notes`, indicate the evidence grade:
- **Grade A:** Org's own website/letter says "donate through CFI" 
- **Grade B:** IRS filing, FOIA response, court filing names CFI as funder
- **Grade C:** Investigative journalism documents the link (already have 332 facts)
- **Grade D:** Media report mentions in passing

---

## Forensic Capture Protocol

Every donation page or letter found **must** be forensically captured. Use the canonical module:

```python
from pipeline.shared.forensic_capture import capture_page
from pipeline.shared.capture_config import CaptureConfig

config = CaptureConfig(
    entity_slug="cfi",
    output_dir=Path("targets/cfi/external"),
    submit_wayback=True,
    create_ots=True,
    upload_to_r2=True,
)
```

Or use the CFI repo's existing website crawl:
```bash
# Single page capture
python -c "
from scrapers.website_crawl import capture_single_url
capture_single_url('https://imti.org.il/en/donate')
"
```

See `~/Desktop/repos/data/ag-complaint-pipeline/specs/forensic-capture.md` for the full spec.

**For Wayback snapshots:** Use `scrapers/wayback_historical.py` to pull all historical versions of donation pages. Historical captures showing CFI references that were later removed are particularly valuable.

---

## What a Completed Entry Looks Like

**Example: Kfar Tapuach (already done)**
- Tapuach's website hosted CFI's 501(c)(3) determination letter
- Letter OCR'd and stored at `targets/cfi/external/tapuach_501c3_letter.pdf`
- Proves Tapuach treats CFI as their fiscal sponsor
- Direct conduit evidence (Theory 5)

**Example: What we need for Im Tirtzu**
1. Screenshot of imti.org.il/donate showing "Donate via Central Fund of Israel"
2. Forensic capture: screenshot + WARC + SHA-256 + Wayback + OTS
3. Wayback snapshots of historical donation pages
4. DB: `israeli_entities` row, `funding_relationships` row (type: fiscal_sponsorship), `documents` row
5. If no current donation page mentions CFI: note that in `funding_relationships.notes` and rely on media evidence

---

## After Research is Complete

Once donation page evidence is collected:

1. Run `python -m scrapers.external_sources media --ein "13-2992985"` — the new orgs in `funding_relationships` will trigger cross-reference search queries (`"Central Fund of Israel" "Im Tirtzu"`, etc.)
2. Run `python -m scrapers.external_sources capture --ein "13-2992985"` — forensic capture of new high-value articles
3. Update the `funding_relationships` rows with `total_granted`, `first_grant_year`, `last_grant_year` as financial data becomes available (FOIA responses, PDF grant schedules)
