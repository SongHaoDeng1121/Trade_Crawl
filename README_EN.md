# Steel Pipe Distributor Outreach MVP

English | [简体中文](README.md)

A local-first B2B system for lead discovery, website crawling, lead scoring, email drafting, and controlled outbound sending. It can also be used with n8n.

The default workflow is:

1. Import company websites or discover them through a search API.
2. Crawl public pages such as home, contact, about, products, and locations pages.
3. Extract public business email addresses such as `sales@`, `info@`, `quotes@`, and `contact@`.
4. Score leads with configurable rules.
5. Use a local or hosted model only for high-priority leads.
6. Generate email drafts.
7. Review drafts manually before sending them in small batches.

The system does not send bulk email by default. Only drafts with `approved=1` enter the sending workflow.

## Quick Start

On a new computer, read [MIGRATION.md](MIGRATION.md) first. The project includes setup scripts that check Python, VS Code, the virtual environment, and dependencies before startup.

macOS:

```bash
./scripts/bootstrap_macos.sh
```

Windows PowerShell:

```powershell
.\scripts\bootstrap_windows.ps1
```

Initialize the database and run the basic workflow:

```bash
python3 -m trade_outreach.cli init-db
python3 -m trade_outreach.cli import-leads-csv data/google_maps_export.csv --source google-maps
python3 -m trade_outreach.cli stats
python3 -m trade_outreach.cli scrape --limit 200
python3 -m trade_outreach.cli score
python3 -m trade_outreach.cli draft --limit 100
python3 -m trade_outreach.cli export-csv data/drafts.csv
```

## Local Dashboard

Start the local dashboard:

```bash
python3 -m trade_outreach.dashboard
```

Then open:

```text
http://127.0.0.1:8765
```

The root page is the task manager. Each task has its own dashboard, SQLite database, search queries, target countries, and company profile. You can create tasks such as `Valve distributors`, and rename or remove existing tasks. Removing a task only removes it from the task list; its local data files are preserved to prevent accidental data loss.

The dashboard displays statistics, leads, extracted email addresses, drafts, prompts, and email templates. It also provides actions for discovery preview/import, crawling email addresses, scoring, drafting, approval, send dry-runs, and test emails. `Crawl Emails` is the only email-crawling action. It processes leads without an email address concurrently, including newly imported leads, websites where no email was found previously, and failed crawls. Once an email is available, you can click `Generate Drafts` without running scoring first. `Generate Drafts` prioritizes leads that have an email address but no existing draft, so older drafted leads do not consume the limit. `Score / Rescore` is only used for ranking and reference. `Approve Batch` approves all pending drafts, including low-score leads and personal business email addresses. To rewrite an individual email, click `Regenerate` in its row in the Drafts table.

The Leads table includes a `Country` column. Leads imported through Google Places automatically store the country used for that search. For older records without a country, the dashboard attempts to infer the country from the source query.

The `Queries` and `Countries` panels at the bottom of the dashboard let you add or remove discovery keywords and target countries. Discovery combines every query with every selected country before submitting searches to Google Places.

The `Your Company` panel lets you save a company name, target market, mailing address, and business summary for the current task. You can also add optional fields such as Facebook, website, WhatsApp, or certification links. These settings affect only the current task and are used in newly generated drafts and the displayed prompt.

To market different products to the same group of companies, click `Export Leads CSV` in the original task. Open another task, enter the exported CSV path in the left panel, and click `Import Leads CSV`. This reuses the lead list while keeping drafts, sending status, and company profiles separate.

Use `Send All Approved` for real sending. It sends approved and unsent drafts, and BCCs the address configured in `MAIL_BCC`. By default, the system sends no more than 100 real emails per day and waits 800 seconds between messages. The same daily limit also applies to individual `Send` actions. Use `Send Dry Run` first to confirm recipients and BCC settings.

If you already have an email address and do not need to crawl a website, enter the address and an optional company name in the left panel, then click `Create Draft`. The system saves it as a local lead and generates a draft directly.

For a company already in the Leads table, enter a new address in the Emails column and click `Add`. The system merges the address into the local `leads.emails` field without overwriting existing addresses. If no draft exists for the new address, a pending draft is created automatically.

Email crawling uses adaptive contact-page discovery. It first checks the homepage and common paths, then analyzes page links and prioritizes pages containing terms such as `contact`, `kontakt`, `ansprechpartner`, `team`, `standorte`, `unternehmen`, and `about`. Low-value links such as `career`, `privacy`, `news`, and `blog` are skipped. The crawler can also follow a small number of relevant contact or company links on associated brands and sub-sites. The extractor handles email addresses split across HTML tags or spaces, such as `info <span>@</span> example . com`.

## Discover Target Companies in Batches

The system does not guess email addresses. It first discovers company websites and then extracts business email addresses from public pages on those websites.

### Automatic Company Discovery

Google Places API is the recommended source for automatic company website discovery. Configure it in `.env`:

```bash
GOOGLE_PLACES_API_KEY=your Google Places API key
GOOGLE_PLACES_REGION_CODE=GB
GOOGLE_PLACES_LANGUAGE_CODE=en
```

The search scope is mainly controlled by two files:

- `examples/discovery_queries.txt`: industry, product, and buyer-type keywords such as distributor, stockholder, and supplier
- `examples/countries.txt`: target-country list

The application submits `keyword + country` to Google Places, for example `steel pipe distributor Germany`. Changing the query file changes the types of companies returned, while changing the country file changes the target markets. `GOOGLE_PLACES_REGION_CODE` and `GOOGLE_PLACES_LANGUAGE_CODE` influence Google Places region and language preferences; they do not restrict searches to a single country.

Preview a small batch without writing to the database:

```bash
python3 -m trade_outreach.cli discover-batch \
  --limit-queries 2 \
  --limit-countries 2 \
  --count 5
```

Import the results after reviewing their quality:

```bash
python3 -m trade_outreach.cli discover-batch \
  --limit-queries 2 \
  --limit-countries 2 \
  --count 5 \
  --execute
```

Crawl public business email addresses and continue the workflow:

```bash
python3 -m trade_outreach.cli scrape --limit 100
python3 -m trade_outreach.cli score
python3 -m trade_outreach.cli draft --limit 100
python3 -m trade_outreach.cli export-csv data/drafts.csv
python3 -m trade_outreach.cli stats
```

Retry every website that still has no email address:

```bash
python3 -m trade_outreach.cli scrape --limit 999 --retry-no-email --max-seconds 900 --fetch-attempts 28 --fetch-timeout 4 --workers 6
```

A lower-cost alternative is to avoid a Search API, collect public company website URLs from directories or trade-show lists, and import them manually:

```bash
python3 -m trade_outreach.cli import-urls examples/manual_urls.txt --source europages
python3 -m trade_outreach.cli scrape --limit 100
python3 -m trade_outreach.cli score
python3 -m trade_outreach.cli draft --limit 100
```

CSV exports from Apollo, Lusha, Apify Google Maps Scraper, other Google Maps scrapers, and similar tools can be imported directly:

```bash
python3 -m trade_outreach.cli import-leads-csv data/google_maps_export.csv --source apify-google-maps
```

The importer recognizes these common column names:

- Company: `company_name`, `company`, `title`, `name`, `business name`, `place name`
- Website: `website`, `website_url`, `websiteurl`, `site`, `url`, `company website`, `business website`
- Email: `email`, `emails`, `company email`, `contact email`
- Phone: `phone`, `phones`, `phone number`, `telephone`

Continue after importing:

```bash
python3 -m trade_outreach.cli scrape --limit 200
python3 -m trade_outreach.cli score
python3 -m trade_outreach.cli draft --limit 100
```

Preview the scraper for a single website:

```bash
python3 -m trade_outreach.cli debug-scrape-url https://example.com/
```

This command only previews results and does not write to the dashboard database. To write results to the database, use:

```bash
python3 -m trade_outreach.cli rescrape-url https://example.com/
```

The same command can add a website to the database and rescrape it:

```bash
python3 -m trade_outreach.cli rescrape-url https://example.com/
```

Useful public sources for European steel-pipe targets include:

- Company directories such as Europages, WLW, and Kompass
- Trade-show exhibitor lists such as Tube Düsseldorf
- National association directories for steel stockholders, pipe distributors, and industrial suppliers
- Manually collected company website URLs from Google Maps searches in target countries

Continue after importing:

```bash
python3 -m trade_outreach.cli scrape --limit 100
python3 -m trade_outreach.cli score
python3 -m trade_outreach.cli draft --limit 100
python3 -m trade_outreach.cli approve-batch --limit 20 --min-score 8
python3 -m trade_outreach.cli approve-batch --limit 20 --min-score 8 --execute
python3 -m trade_outreach.cli send --limit 100 --delay-seconds 800 --dry-run
python3 -m trade_outreach.cli send --limit 100 --delay-seconds 800
```

Save the target market in the dashboard's `Your Company` panel or set it in `.env`:

```bash
TARGET_REGION=your target market
GOOGLE_PLACES_REGION_CODE=GB
GOOGLE_PLACES_LANGUAGE_CODE=en
```

## Prompt and Template Locations

- DeepSeek/Ollama personalized opening prompt: [trade_outreach/prompts.py](trade_outreach/prompts.py)
- Email subject and body templates: [trade_outreach/templates.py](trade_outreach/templates.py)
- Keyword scoring rules: [trade_outreach/scoring.py](trade_outreach/scoring.py)
- Discovery logic and excluded domains: [trade_outreach/discovery.py](trade_outreach/discovery.py)

List drafts:

```bash
python3 -m trade_outreach.cli list-drafts
```

Approve a draft manually:

```bash
python3 -m trade_outreach.cli approve-draft 1
```

Unapprove an unsent draft:

```bash
python3 -m trade_outreach.cli unapprove-draft 1
```

Run a sending dry-run:

```bash
python3 -m trade_outreach.cli send --limit 5 --dry-run
```

Real sending requires SMTP environment variables and removal of the `--dry-run` flag.

Batch sending still processes only drafts where `approved=1` and no sent timestamp exists:

```bash
python3 -m trade_outreach.cli send --limit 100 --delay-seconds 800 --dry-run
python3 -m trade_outreach.cli send --limit 100 --delay-seconds 800
```

## Automatic Pipeline

`auto-pipeline` checks the current draft and sending state:

1. If approved drafts remain unsent, send that batch first.
2. When every draft has been sent, run the next `discover-batch -> scrape -> score -> draft` cycle automatically.
3. New drafts are approved automatically by default.
4. The default mode sends through SMTP and still respects `SEND_DAILY_LIMIT`, `--send-limit`, and the configured delay.

Run continuously:

```bash
python3 -m trade_outreach.cli auto-pipeline \
  --cycles 0 \
  --sleep-seconds 0 \
  --send-limit 5 \
  --delay-seconds 800
```

Auto Pipeline enables `--continue-after-sleep` for its internal send command. If the computer sleeps longer than `--delay-seconds`, sending continues after the computer wakes instead of stopping the batch.

Run a safe preview without real sending:

```bash
python3 -m trade_outreach.cli auto-pipeline \
  --cycles 1 \
  --dry-run \
  --send-limit 5 \
  --delay-seconds 800
```

Disable automatic approval when manual review is required:

```bash
python3 -m trade_outreach.cli auto-pipeline \
  --cycles 1 \
  --dry-run \
  --no-auto-approve
```

Real sending processes only drafts where `approved=1` and `sent_at` is empty, and remains subject to `SEND_DAILY_LIMIT`.

## Configuration

Copy the example configuration:

```bash
cp .env.example .env
```

Common environment variables:

- `OUTREACH_DB`: SQLite file path; default `data/outreach.sqlite3`
- `LLM_PROVIDER`: `deepseek` or `ollama`
- `DEEPSEEK_API_KEY`: DeepSeek API key
- `DEEPSEEK_BASE_URL`: default `https://api.deepseek.com`
- `DEEPSEEK_MODEL`: default `deepseek-v4-flash`
- `OLLAMA_URL`: Ollama URL; default `http://localhost:11434`
- `OLLAMA_MODEL`: local model name; default `llama3.1:8b`
- `USE_LLM`: whether to use an LLM; default `0`
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`
- `SMTP_SECURITY`: `starttls`, `ssl`, or `none`
- `MAIL_FROM`, `MAIL_REPLY_TO`, `MAIL_CC`, `MAIL_BCC`
- `SEND_DELAY_SECONDS`: default delay between messages in seconds; default `800`
- `SEND_DAILY_LIMIT`: maximum number of real messages per day; default `100`
- `COMPANY_NAME`, `COMPANY_ADDRESS`, `COMPANY_FACEBOOK_URL`

To CC yourself on every customer email:

```bash
MAIL_CC=test@example.com
```

To hide the copy address from recipients, use BCC:

```bash
MAIL_BCC=test@example.com
```

## Using DeepSeek

Copy `.env.example` to `.env`, then configure:

```bash
USE_LLM=1
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=your DeepSeek API key
DEEPSEEK_MODEL=deepseek-v4-flash
```

DeepSeek's official documentation describes an OpenAI-compatible API. `deepseek-v4-flash` is suitable for low-cost batch classification and short copy generation. `deepseek-chat` and `deepseek-reasoner` are scheduled for deprecation at 23:59 Beijing time on July 24, 2026, so this project does not use those older model names by default.

## Cost-Efficiency Strategy

- LLM use is disabled by default; the full workflow can run with rule-based scoring only.
- With `USE_LLM=1`, the model processes high-priority leads and generates only one personalized opening sentence.
- Scoring is mainly used for ranking and relevance and does not block draft generation by default.
- Drafts are created for generic business mailboxes by default, such as `sales@`, `info@`, `export@`, `contact@`, and `vertrieb@`.
- To include personal business email addresses, run `python3 -m trade_outreach.cli draft --include-personal`.
- Crawling focuses on home and contact pages so large amounts of unrelated website content are not sent to a model.
- Sending always checks the opt-out blocklist and approval state.

## n8n

Import [n8n/steel_pipe_outreach_workflow.json](n8n/steel_pipe_outreach_workflow.json). It uses Execute Command nodes to call the local CLI.

## Compliance Reminder

For commercial email sent to the United States, comply with CAN-SPAM requirements: accurate sender information, non-deceptive subject lines, a valid physical mailing address, a clear opt-out method, and prompt handling of opt-out requests. For a new sending domain, start with approximately 20-50 messages per day and increase volume gradually.
