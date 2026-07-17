# AGENTS.md

## Project Goal

This project is a local-first general B2B outreach system.

The system should allow a user to:

1. Define any target industry, region, country, and buyer profile.
2. Discover companies in that target market.
3. Crawl public company websites for business contact emails.
4. Store leads, emails, phones, source URLs, scrape status, and notes in a local database.
5. Let the user enter their own company information.
6. Generate English B2B email drafts.
7. Review drafts before sending.
8. Send emails in controlled small batches through SMTP.

The project should be industry-agnostic. Current Europe steel pipe settings are only examples.

## Core Product Rules

- Support any industry, not only steel pipe.
- Email language is English by default.
- Do not add campaign/project grouping unless the user explicitly asks later.
- Keep the app simple: one active target market and one active company profile at a time.
- All market, product, and company assumptions should be configurable.

## Safety Rules

- Never print `.env` secrets, API keys, SMTP passwords, or tokens.
- Never commit `.env`.
- Never send real emails unless the user explicitly confirms.
- Dry-run and test-send workflows should be easy and visible.
- Keep unsubscribe/opt-out text in outbound emails.
- Keep mailing address in outbound emails when configured.
- Do not delete leads, drafts, or database records without explicit confirmation.
- Do not scrape private, login-only, or paid data.
- Only collect publicly available contact information.

## Configurable User Inputs

Do not hardcode these values:

- Target industry
- Target region
- Target country list
- Target buyer type
- Discovery keywords
- User company name
- User company address
- User company website
- User company social links
- Product/service description
- SMTP account
- LLM provider/model
- API keys
- Sending limits

`.env.example` should document required and optional configuration.

## Lead Discovery Rules

Discovery should support:

- Google Places API
- Manual URL lists
- Imported CSV files
- Google Maps scraper exports
- Apollo/Lusha/other third-party exports if provided by the user
- Future search APIs if configured

Discovery should normalize and deduplicate company websites.

Each lead should store:

- Company name
- Website
- Source
- Source query if available
- Country/region if known
- Created/updated timestamp

## Scraping Rules

Website scraping should:

- Crawl public pages only.
- Prefer homepage, contact, about, products/services, locations, imprint/impressum, team, sales pages.
- Support multilingual contact paths.
- Extract direct emails, `mailto:` links, obfuscated emails, and Cloudflare-protected emails when possible.
- Extract phones when available.
- Keep page text for scoring and email personalization.
- Avoid getting stuck on slow or broken websites.
- Use request limits and timeouts.
- Continue to the next lead if one lead fails.

Status meanings:

- `new`: imported/discovered, not scraped yet.
- `scraped`: website was scraped and at least one email was found.
- `no_email`: website text or phone was found, but no email was extracted.
- `scrape_failed`: website could not be fetched or parsed.
- `scored`: lead has been scored.
- `drafted`: email draft exists.
- `sent`: email was sent.

Important:

- `debug-scrape-url` previews scraping results only and does not write to the database.
- `rescrape-url` writes scraping results into the database.
- Dashboard retry buttons must write results into the database.
- Do not overwrite existing useful emails with empty scrape results.

## Email Selection Rules

When multiple emails are found:

- Store all valid emails.
- Prefer generic business emails when available:
  - `sales@`
  - `info@`
  - `contact@`
  - `quotes@`
  - `export@`
  - `procurement@`
  - `purchasing@`
- Personal emails are allowed.
- If no generic mailbox exists, selecting a personal business email is acceptable.
- Avoid clearly irrelevant emails:
  - `career@`
  - `jobs@`
  - `privacy@`
  - `legal@`
  - `webmaster@`
  - `noreply@`

The selected recipient should be visible in the dashboard.

## Scoring Rules

Scoring is for prioritization, not blocking.

The user should be able to generate drafts or send emails even if score is low.

Score should be explainable and based on configurable signals:

- Industry keyword match
- Product/service keyword match
- Buyer/distributor/importer/stockist signals
- Region/country match
- Email availability
- Negative signals such as careers-only, news-only, irrelevant industry

## Draft Generation Rules

Generated emails must:

- Be in English.
- Use the user's company information.
- Use the target company name.
- Mention the target industry/product only when supported by available data or user config.
- Avoid hallucinated claims.
- Avoid placeholders like `[your product]`, `[product/service]`, `[specific category]`.
- Include opt-out text.
- Include mailing address if configured.
- Include social links if configured.
- Be concise and businesslike.
- Avoid pretending there was prior contact.

If the LLM returns placeholder text, sanitize or replace it with safe fallback wording.

Draft generation should support:

- Generate only missing drafts.
- Regenerate/replace existing drafts.
- Preview before approval.
- Export drafts to CSV.

## Dashboard Rules

The dashboard should clearly show:

- Total leads
- Leads with emails
- Leads without emails
- Failed scrape count
- Draft count
- Approved count
- Sent count
- Current target market
- Current company identity
- Current prompt/template
- Current SMTP status
- Leads table
- Drafts table
- Command output

Dashboard actions should include:

- Discover preview
- Discover import
- Scrape new leads
- Retry all no-email leads
- Score/rescore
- Generate drafts
- Regenerate drafts
- Export CSV
- Send test email
- Dry-run send
- Real send only with explicit confirmation

Dashboard labels must be clear. Avoid labels that imply emails were found when only page text was scraped.

## Sending Rules

Sending must be conservative.

Default behavior:

- Test send to user's own email first.
- Dry-run before real send.
- Batch size limits.
- Delay between sends.
- BCC configured mailbox if provided.
- Record sent timestamp.
- Do not resend already sent drafts unless the user explicitly requests.

Real sending must never happen from discovery, scraping, scoring, or drafting actions.

## Data Rules

Use local SQLite by default.

Database should preserve:

- Leads
- Emails
- Phones
- Website
- Source
- Page text
- Score
- Score note
- Draft subject/body
- Approval status
- Sent timestamp
- Error notes

Never delete or reset the database without explicit user confirmation.

## Development Rules

- Prefer small, focused changes.
- Preserve existing user data.
- Do not delete database records without explicit confirmation.
- Use `rg` for searching files.
- Use `apply_patch` for manual edits.
- Run syntax checks after code changes:

```bash
python3 -m compileall trade_outreach
```

When changing scraping behavior, test:

```bash
python3 -m trade_outreach.cli debug-scrape-url https://example.com/
python3 -m trade_outreach.cli rescrape-url https://example.com/
```

When changing sending behavior, use dry-run or test-recipient first.

## Current Implementation

Current implementation includes:

- Python CLI
- Local SQLite database
- Local dashboard at `http://127.0.0.1:8765`
- Google Places discovery
- Website email scraper
- DeepSeek-compatible LLM generation
- SMTP sending
- n8n workflow export

Current Europe steel pipe configuration is a sample use case only.

## Future Direction

Planned evolution:

- User-editable target market form
- User-editable company profile form
- User-editable discovery keywords
- User-editable product/service description
- Better email extraction for JS-heavy websites
- Optional browser-based scraping for difficult websites
- Better duplicate detection
- Better compliance controls
- Send history and reply tracking
