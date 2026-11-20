# AI Lead Generation API

A FastAPI service for storing company leads, searching the web for potential
companies through Tavily, and saving search results with their source evidence
for later review.

## How the workflow works

The application has two ways to create company records:

1. **Manual entry** — submit company details to `POST /api/v1/companies`.
   Company names and domains are normalized before storage. If only a website
   is supplied, its hostname is used as the domain. A duplicate domain is
   rejected with `409 Conflict`.
2. **Web discovery** — search with `POST /api/v1/discovery/search`, then save
   reviewed results using `POST /api/v1/discovery/ingest`. Searches are kept
   with timestamps for suggestions; saved companies show which search found
   them and when.

The discovery and ingestion workflow runs as follows:

1. Validate the query (3–300 characters) and requested result count (1–10;
   default 5).
2. Send the query to Tavily using its basic search endpoint. Search results
   with missing or invalid fields are discarded; provider timeouts and errors
   are returned as API errors rather than being treated as empty results.
3. Record each successful search keyword and its timestamp. The response
   includes a `search_id` used to associate saved leads with that search.
4. For each valid result, use its URL as the source identity. A URL already
   present in the batch or database is skipped.
5. Normalize the result title and look for a company with the same normalized
   name. Normalization applies Unicode compatibility normalization, case
   folding, punctuation removal, and whitespace collapsing.
6. If no matching company exists, create one with a `needs_review` lead status.
   If a company already exists, attach the new source to it without changing
   its existing status or details.
7. Associate the search keyword and search timestamp with each matching
   company. Repeated searches remain separately recorded.
8. Crawl each distinct displayed source URL with bounded concurrency. Successful
   crawls fill missing website/domain fields and add publicly listed email and
   phone contacts without overwriting existing company details. Crawled page text is
   retained with the lead source as evidence. Industry, location, software need,
   and lead score are not inferred.
9. Save a `web_search` lead source. Crawl failures are reported in the response,
   but the source is still saved for review. Database changes are committed as
   one batch; integrity conflicts trigger a retry, and other failures roll back.
10. Queue mobile and desktop PageSpeed scans for each distinct company website
   and return their job IDs without waiting for Google. The worker stores
   completed reports; job status is available from the company PageSpeed jobs
   endpoint and the recent-job list at `GET /api/v1/pagespeed/jobs`.
11. Return counts for search results, new candidates, sources, crawls, and
   enrichment, along with crawl failures and queued PageSpeed jobs.

Website enrichment is also available as a separate workflow.
`POST /api/v1/crawler/crawl` accepts a public website URL and queues a job.
Poll `GET /api/v1/crawler/jobs/{job_id}` or list recent jobs with
`GET /api/v1/crawler/jobs`. Job responses include queue, start, and completion
timestamps and a failure reason when applicable; successful responses include
the saved crawl result. Retry a failed job with
`POST /api/v1/crawler/jobs/{job_id}/retry`; queued or running jobs cannot be
retried. The worker fetches the
homepage and up to five relevant same-site About, Blog, Services, Contact, or
mission/goal pages, then stores their page URLs and text, email addresses,
telephone numbers, and social links in the `website_crawls` table. It renders
JavaScript pages with headless Chromium and does not check `robots.txt`. Each
attempted page's HTTP status and error detail are also stored in the crawl's
per-page classification data, including failed pages without classification
signals. Fatal homepage failures are saved on the crawl job with the upstream
HTTP status when one was received. When a
company has the same normalized domain, extracted email addresses and telephone
numbers are also imported as separate, publicly listed Contact records. Imports
are deduplicated against existing contacts and do not overwrite company fields
or existing contact details.
It retries temporary `429` and server errors at most twice,
respecting a short `Retry-After` delay when provided. A `403` means the site
denied the request; the crawler reports it without attempting to defeat the refusal.
Confirm the backend host is authorized and allowlisted by the site owner.
Company profiles show the latest successful crawl and its extracted details.
Use `GET /api/v1/companies/{id}/website-crawl` to load that result and
`POST /api/v1/companies/{id}/website-crawl` to queue a crawl using the
company's website (or domain when no website URL is set). Discovery ingestion
and research-source enrichment remain synchronous because they need crawl
results before those workflows can finish.
Each successful queued website crawl also creates mobile and desktop PageSpeed
jobs without delaying the crawl response. Jobs are linked to the website and,
when a company with the same normalized domain exists, to that company as well.
The worker processes the scans separately; configure a Google PageSpeed API key
in **Settings → PageSpeed** for them to succeed.

The website registry at `GET /api/v1/websites` stores one row per normalized
domain. Discovery saves only the provider and canonical HTTPS origin; it does
not store provider titles or snippets. Each new row starts with status `new`;
website statuses can be `new`, `lead`, or `checked`. Rows have no company link.
The save operation sends the origin to the existing crawl
job queue; crawl progress remains on `website_crawl_jobs`. The Websites page
links each domain to `GET /api/v1/websites/{id}`, which returns the website
record, crawl page content, classification signals, contact/social links, the
ten most recent PageSpeed score summaries, and crawl-job history. Full
Lighthouse audits are loaded on demand from
`GET /api/v1/websites/{id}/pagespeed-scans/{scan_id}`. The list API supports
bounded `limit`/`offset`, page type, and search filters. Website type, site
topic, and language can be
edited with `PATCH /api/v1/websites/{id}`. The **Add to Lead** action creates
companies for the filtered crawled websites and marks their registry status as
`lead`; listing websites are excluded. Newly created companies use the domain
as their initial name and have lead status `new`.
Public email addresses and telephone numbers from the linked crawl are imported
as separate contacts for new companies and added to matching existing companies
without overwriting their existing contact details. Company profiles use the
Contacts section as the single place to manage email and phone values; raw crawl
extractions remain visible on website profiles.
The Websites page also displays the top keywords extracted from each domain's
own saved crawl page titles and descriptions. `GET
/api/v1/websites/corpus-words` computes these independently using the Go
crawler service's `POST /corpus-words` endpoint and persists each domain's
results until that website is re-crawled.
Deploy or restart the Go crawler service whenever its corpus response contract
changes; `GET /health` alone does not verify that `/corpus-words` is current.
When **Save websites** is used, the Agent also requests full HypeStat profiles
through the Go crawler service's authenticated `POST /hypestat/domains` proxy.
The response is stored unchanged in `website_hypestat_profiles` and displayed
on the website detail page. Successful profiles are reused on later saves;
queued or failed lookups can be retried by saving the website again or using
the **Refetch HypeStat** action on the website detail page. HypeStat limits
requests to 60 per minute and 1,000 per day, with up to 50 new or updated
website analyses per day; domains not yet analyzed are queued.

On a company profile, select **Crawl and enrich** on an existing Research
source card to crawl its saved URL and find company website/domain details and
public contact email addresses or telephone links. The source and crawl
evidence are saved to the company; existing details are left unchanged, and
website/domain values are filled only when the source provides a credible
company association. The API endpoint is
`POST /api/v1/companies/{id}/sources/crawl` with a `source_url` field.

The Jobs page combines the Crawler and PageSpeed queues in separate tabs. The
Jobs page and active crawl-status views receive job updates over the WebSocket
endpoint `/api/v1/queue/events`. PostgreSQL publishes an event after a job is
created or updated, including when a separate worker process changes it. The
frontend reconnects and refreshes the affected job data when it receives an
event; it does not continuously poll those job statuses. The Indexing tab can
queue up to 100 websites from a CSV with a `website_url` or `url` column; the
CSV is parsed in the browser and all valid URLs are submitted in one batch
request. Jobs are committed together and dispatched through one Redis request.
The Crawler and PageSpeed tabs can also retry all recent failed jobs in one
batch request; eligible jobs are updated together and dispatched once.

The **Excluded domains** page manages organization-scoped domain exclusions.
`GET /api/v1/excluded-domains` supports `search` and `reason`
filters; `POST /api/v1/excluded-domains` accepts a domain or URL, a reason, and
an optional note; `POST /api/v1/excluded-domains/batch` accepts up to 1,000
domains in one request; `DELETE /api/v1/excluded-domains/{id}` re-enables that
domain. The Excluded domains page imports CSV files with a `domain`, `url`, or
`website_url` column and optional `reason` and `note` columns.
Preset social and technology domains are added for each organization.
Exclusions apply to subdomains as well as the exact domain: matching crawl jobs
are marked `skipped` before dispatch, and the worker checks again before
crawling. Classified DNS, refused-connection, and timeout failures are retried
up to the crawl-attempt limit; after three terminal failures, the domain is
automatically excluded as `Unreachable`.

The legacy `POST /api/v1/discovery/ingest` endpoint continues to create and
enrich companies directly and queues mobile and desktop Google PageSpeed
Insights scans. The frontend discovery workflow instead creates domain
registry rows and queues crawls without creating companies. Users configure
their own Google PageSpeed API key from **Settings → PageSpeed**.

`POST /api/v1/discovery/search` accepts Tavily search depth, topic, country,
date range, domain filters, minimum relevance score, and optional AI summary
settings. It returns Tavily usage credits and result favicon and publication
metadata when available. Organization admins can set the default search depth,
country, and excluded domains from **Settings → Tavily**; these defaults
prefill the Discover Leads filters. Apply pending database changes with
`.\.venv\Scripts\python.exe -m alembic upgrade head` from the `Agent` directory.

## Requirements

- Python 3.10 or newer
- PostgreSQL
- Upstash Redis for queued website crawl jobs
- A Tavily API key for the discovery endpoints
- A Google PageSpeed API key for PageSpeed scans after crawls or discovery ingestion

The dependency file is named `requirements.txt` and includes Alembic for schema
migrations.

## Local setup

Run these commands from the repository root in PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r .\requirements.txt
python -m playwright install chromium
```

If PowerShell prevents activation due to its execution policy, use the virtual
environment interpreter directly as `.\.venv\Scripts\python.exe`.

Create a `.env` file in the repository root (do not commit it):

```dotenv
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DATABASE
ACCESS_TOKEN_SECRET=generate-a-unique-64-character-hex-secret
ACCESS_TOKEN_EXPIRY=15m
REFRESH_TOKEN_SECRET=generate-a-different-64-character-hex-secret
REFRESH_TOKEN_EXPIRY=168h
UPSTASH_REDIS_REST_URL=https://your-upstash-database.upstash.io
UPSTASH_REDIS_REST_TOKEN=your-upstash-rest-token
ENVIRONMENT=development
FRONTEND_URL=http://localhost:3000
CRAWLER_SERVICE_SHARED_TOKEN=your-shared-service-token
GOOGLE_OAUTH_CLIENT_ID=your-google-oauth-client-id
GOOGLE_OAUTH_CLIENT_SECRET=your-google-oauth-client-secret
GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/api/v1/gmail/callback
GOOGLE_OAUTH_STATE_SECRET=generate-a-random-64-character-hex-secret
GMAIL_TOKEN_ENCRYPTION_KEY=replace-with-generated-Fernet-key
EMAIL_UNSUBSCRIBE_SECRET=generate-a-random-64-character-hex-secret
CLOUDINARY_CLOUD_NAME=your-cloudinary-cloud-name
CLOUDINARY_API_KEY=your-cloudinary-api-key
CLOUDINARY_API_SECRET=your-cloudinary-api-secret
```

`DATABASE_URL` is required when the application starts. The access and refresh
token secrets must be distinct, random hexadecimal values containing at least
32 bytes each; keep them stable. Generate independent values with
`python -c "import secrets; print(secrets.token_hex(32))"` and set the expiry
values using positive `s`, `m`, `h`, or `d` durations. Access tokens may be at
most 24 hours; refresh tokens may be at most 365 days. Sessions created with
older token signing are not accepted; users must sign in again after rotating
to these separate secrets. The database layer uses psycopg for PostgreSQL.
The website crawler uses Playwright Chromium; install the browser on every
backend host with `python -m playwright install chromium`.
Set the same `CRAWLER_SERVICE_SHARED_TOKEN` in the Agent environment and
`crawler-service/.env` (or its process environment). Enter the HypeStat API key
on the Settings page; it is sent only from the Agent to the Go proxy. The Go
proxy uses it for the first-party HypeStat API and never returns it to the
frontend. Generate a shared token with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`.
Configure the three Cloudinary variables to enable user profile photos and
organization logo/banner uploads. Images are uploaded from the Agent using
server-side credentials; supported formats are JPEG, PNG, and WebP up to 8 MB.
Never expose or commit `CLOUDINARY_API_SECRET`.

To connect Gmail accounts, configure a Google OAuth client with the redirect
URI above (use your HTTPS API URL in production). Generate a state-signing key
with `python -c "import secrets; print(secrets.token_hex(32))"` and a Gmail
refresh-token encryption key with
`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
Keep both secrets private and stable. The OAuth flow requests verified profile
email plus Gmail modify and send scopes. Refresh tokens are encrypted in the
database; Gmail access is limited to accounts explicitly assigned by an
organization administrator.

After connecting an account, administrators can assign it to members under
**Settings → Gmail accounts**. Assigned members open **Gmail** in the workspace
navigation to search and page through the inbox, open Spam and custom labels,
read messages, send new messages, and send threaded replies. Message HTML is
shown as plain text; the inbox does not expose message content from accounts
that are not assigned to the signed-in member.

Organization email templates are available under
`/api/v1/orgs/{org_id}/templates`. Administrators can create, update, and
delete templates; organization members can list and retrieve them.
`GET /variables` returns insertable placeholders.
Preview with `POST /{template_id}/preview` using a `company_id`, optional
`contact_id`, optional `gmail_account_id`, and optional `custom_variables`.
Send with `POST /{template_id}/send` using a `company_id`, `contact_id`, and an
assigned `gmail_account_id`; delivery is to that contact's business email. The
body is rendered in a Jinja sandbox with HTML autoescaping, and
`default("there")` substitutes null or empty values. Custom variables may add
top-level values or override nested context fields, except `sender.email`, which
is determined by the preview identity or connected Gmail account.
`company.crawled_text` aggregates available crawl-page text for the company
domain, and `company.phone` maps to the selected contact's business phone
because the Company model has no phone column.
Administrators can generate an editable draft with
`POST /api/v1/orgs/{org_id}/templates/generate-draft`. The request uses the
signed-in administrator's Gemini key from **Settings → Gemini**; generated
drafts are validated and returned without being saved or sent.

### Email drip campaigns

Administrators can create tenant-scoped campaigns at
`/api/v1/orgs/{org_id}/campaigns`. A campaign selects an existing email
template and Gmail accounts assigned to its creator. Preview and start requests
take a list of `company_ids`; the service queues each eligible contact with a
valid business email, reports companies with no eligible contacts, and rotates
recipients round-robin across the selected Gmail accounts.

Use `POST /{campaign_id}/preview` before
`POST /{campaign_id}/start`. The preview reports eligible contacts, skipped
companies and suppressed contacts, per-day account capacity, and an estimated
completion time.
Campaign schedules use the configured IANA timezone, randomized per-account
delays, the daily per-account cap, and weekday send windows. Use `POST
/{campaign_id}/pause` and `POST /{campaign_id}/resume` to pause and recalculate
pending delivery slots. `GET /{campaign_id}` and `GET /` include recipient
counts; `DELETE /{campaign_id}` removes draft or paused campaigns.

Campaign creation accepts `follow_up_steps`, an ordered list of
`{"template_id": "...", "delay_days": 3}` entries, and `verify_mx` (default
`true`). Each follow-up selects an organization template and a 1–365 day wait
after the previous step; up to ten follow-ups may be configured. Replies and
recognized hard bounces stop queued follow-ups. When enabled, DNS mail routing
is checked immediately before sending (MX, with the standard A/AAAA fallback).
This does not validate that a specific mailbox exists; SMTP recipient probing
is not performed.

The existing PostgreSQL worker claims due recipients with
`FOR UPDATE SKIP LOCKED`, renders each message with the same sandboxed template
service as single-email sends, and sends through the assigned Gmail account.
The background worker also periodically checks Gmail threads for contact
replies and recent bounce notices from mail delivery agents. Reply detection
looks for the recipient in the Gmail thread of a campaign message; replies
sent as unrelated new threads may not be associated. It recognizes hard-bounce
reports containing common permanent-failure signals; this is not a general
Gmail webhook or complaint feed. Detected hard bounces are added to the
platform-wide suppression list. Monitoring defaults to every five minutes;
configure `EMAIL_MONITOR_INTERVAL_SECONDS` (30–86400 seconds) to change it.
`GET /api/v1/gmail/accounts/health` reports per-account send, reply, bounce,
failure, unverified-address, and monitor status metrics for the last 30 days.
Campaign response counts include completed recipients, sent messages, replies,
bounces, and verification failures. Open and click tracking are not collected.
Campaign status changes are published through the existing
`/api/v1/queue/events` WebSocket. The `d39a6f2c8e14` migration adds campaign
storage, `e6f4a1b3c9d2` adds and backfills each company's latest successful
campaign email timestamp, and `c62e8f4a1b37` adds sequence, verification,
monitoring, and delivery-event storage. Apply all through the normal deployment
migration process.

### Email opt-outs and suppression

Every Gmail send path adds a visible unsubscribe link. Individual-recipient
messages also include RFC one-click `List-Unsubscribe` headers; group messages
use a recipient-bound public confirmation link. The public unsubscribe page
requires no login; a valid request creates a platform-wide suppression record.
Campaign previews omit suppressed contacts. Every send rechecks the list
immediately before contacting Gmail, so re-importing a contact cannot bypass
an opt-out.
Organization administrators can review entries created by their organization
and add address- or domain-level entries under **Settings → Email suppression**.
These entries, including opt-outs, are platform-wide and apply across all
organizations.
Domain entries also cover subdomains. The admin API is `GET` and `POST
/api/v1/email-suppressions`; the public opt-out API is `GET` and `POST
/api/v1/unsubscribe`, with RFC one-click requests handled by
`POST /api/v1/unsubscribe/one-click`. Admin entries accept `email` or `domain`
targets and `manual_dnc`, `hard_bounce`, or `complaint` reasons. Entries are
intentionally not removable through the UI.

Set `EMAIL_UNSUBSCRIBE_SECRET` to a stable, private random value in production.
If omitted, the application derives a signing key from `ACCESS_TOKEN_SECRET`;
changing either key invalidates outstanding unsubscribe links. Unsubscribe
tokens do not expire. The `c51a7d2e9f40` migration creates the suppression
table and indexes; apply it through the normal deployment migration process.
Recognized hard-bounce notices from monitored Gmail accounts are recorded
automatically. Other hard bounces and spam complaints can still be added by an
administrator; complaints are not automatically detected.

On the frontend, organization admins can select individual leads, the current
page, or all filtered results to seed campaign setup. Successful campaign
delivery updates `Company.last_email_sent_at`; the Leads table shows this
separately from `lead_status`, which remains the sales pipeline status.

Apply migrations, then open the frontend and create the first organization
administrator. Subsequent users can sign in; anyone can create a separate
organization, while an organization administrator can add members from
**Settings → Organization users**. Roles are `admin` and `user`. Members share
their organization's companies, websites, and jobs; provider API keys are
personal to each user and stored as plaintext in the database, including the
Gemini key used for template drafts. Restrict
database access and use dedicated provider keys: anyone with database access
can read them. Existing records are assigned to one default organization by
the migration. Previously saved encrypted provider keys are removed by the
plaintext-storage migration and must be entered again.

All business APIs and queue WebSockets require authentication. The API issues
HTTP-only access and refresh cookies using the configured expiry values.
Refresh tokens are rotated after use and their hashes are stored so they can be
revoked; both token types are invalidated after a password change. The browser
refreshes and retries a request once when its access token expires. Cookies are
same-site; production cookies require HTTPS. The frontend and API should use
the same hostname (for local development, use
`localhost` for both rather than mixing `localhost` and `127.0.0.1`). The API
checks request origins for state-changing browser requests. Organization
administrators can change shared crawler/discovery settings and manage
organization users; regular users can manage their own provider keys.
Provider keys are never returned by the API. The Settings page can query
Tavily's usage endpoint for current-cycle credits and limits.
The Settings page can search Octoparse templates and configure the template's
query input, additional JSON parameters, and output-field mappings. Octoparse
searches start provider jobs; the Discover Leads page polls those jobs and
combines completed results with the selected Tavily results. The chosen search
engines are saved on the backend. The Business listing websites category is
shown as coming soon until a provider is configured. Settings summarizes
Octoparse tasks started through this application for the current UTC month and
all time; it does not report Octoparse account-wide quota or plan usage.
The page also sets maximum pages and depth for new crawls (up to 6 pages and
depth 2); the crawler service may enforce lower deployment limits. Address,
Chrome path, response/timeouts, queue, retries, rate limit, user agent, and Go
runtime memory/CPU settings remain deployment-managed and require a service
restart to change.
`UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` enable immediate
dispatch of website crawl and PageSpeed jobs through Upstash Redis. PostgreSQL
stores durable job status and results; if Redis is unavailable, the running
worker recovers queued jobs from the database.
Set `FRONTEND_URL` to the comma-separated list of frontend origins allowed to
call the API, for example
`FRONTEND_URL=http://localhost:3000,http://127.0.0.1:3000`.
Keep real credentials in your local environment and out of source control.

## Database setup

Apply the migrations to the configured PostgreSQL database:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
```

The migration chain creates the core company, contact, and lead-source tables;
adds normalized company names, source URL uniqueness, timestamped search
keywords, and per-company PageSpeed reports; converts entity IDs to UUIDs while
preserving existing records and relationships; adds durable crawl and scan
jobs, discovery preferences, and Octoparse provider jobs; creates and
backfills the website registry; and adds per-website HypeStat profiles with
the full provider response, lookup status, errors, and timestamps. The latest
migration adds organizations, users, roles, per-user provider credential
records, creator attribution, and organization ownership for existing
business data and queued jobs; it assigns pre-existing records to the default
organization.
If the unique source-URL migration detects duplicates in existing data, it
stops with an error; resolve those duplicates before rerunning the migration.

To see the current migration revision:

```powershell
.\.venv\Scripts\python.exe -m alembic current
```

## Start the API

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

By default, Uvicorn listens on `http://127.0.0.1:8000`.

- Health check: `GET http://127.0.0.1:8000/health`
- Interactive API documentation: `http://127.0.0.1:8000/docs`
- Alternative API documentation: `http://127.0.0.1:8000/redoc`

In another terminal, start a worker with the same environment configuration:

```powershell
.\.venv\Scripts\python.exe -m app.worker
```

Run one or more workers to process queued crawls and PageSpeed scans. The
worker calls the separate crawler service for crawl jobs, so
`WEB_CRAWLER_SERVICE_URL` must point to a running crawler service.

### Run the worker in the API service

For a deployment where a separate background-worker service is not available,
set `RUN_WORKER_IN_API=true` on the API service. The API starts the queue worker
after database migrations complete, so keep the web service's normal Uvicorn
start command. Do not also run a separate worker unless you intentionally want
multiple workers.

On Render's free web-service plan, the worker runs only while the web service is
awake. Render may spin the service down after 15 minutes without inbound
traffic; queued jobs remain in PostgreSQL and can be processed after a request
wakes the service again. The worker's startup log says
`Background job worker started.` Ensure
`WEB_CRAWLER_SERVICE_URL` points to the deployed crawler service.

## API usage

All versioned endpoints use the `/api/v1` prefix.

### Search and keyword suggestions

`POST /api/v1/discovery/search`

```json
{
  "query": "manufacturing companies in Kolkata",
  "max_results": 20
}
```

The backend runs the search engines selected in Settings. The response
contains the original query, a `search_id`, immediate validated results with
`title`, `url`, `content`, and provider `score`, plus any provider jobs and
partial provider failures. Octoparse searches run asynchronously; poll
`GET /api/v1/discovery/jobs/{job_id}` until the job succeeds or fails. The
search keyword and timestamp are saved, but no lead records are created until
results are ingested. Database IDs, including `search_id` and job IDs, are UUID
strings.
Use `GET /api/v1/discovery/suggestions?query=manu` while typing to retrieve up
to ten recent matching keywords.

### Search and ingest results

`POST /api/v1/discovery/ingest`

The frontend submits the results it displayed so ingestion crawls the exact
source URLs the user reviewed. Older clients may omit `results`, in which case
the API performs a fresh search. Example request:

```json
{
  "query": "manufacturing companies in Kolkata",
  "max_results": 5,
  "search_id": "6ba7b810-9dad-41d1-80b4-00c04fd430c8",
  "results": [
    {
      "title": "Example Manufacturing",
      "url": "https://example.com/about",
      "content": "Manufacturing company",
      "score": 0.9
    }
  ]
}
```

An example success response:

```json
{
  "query": "manufacturing companies in Kolkata",
  "results_found": 5,
  "candidates_created": 3,
  "results_skipped": 1,
  "websites_crawled": 4,
  "companies_enriched": 3,
  "contacts_added": 2,
  "crawl_failures": [],
  "pagespeed_scans_queued": 6,
  "pagespeed_jobs": [
    {
      "id": "b7e9f15d-3e9b-4c7f-b777-4b6a8ad21e6e",
      "company_ids": ["b3f53f4f-4f4b-4b9d-9c67-3403a180b532"],
      "website_url": "https://example.com/",
      "strategy": "mobile",
      "status": "queued",
      "attempts": 0,
      "error": null,
      "created_at": "2026-11-11T00:00:00Z",
      "updated_at": "2026-11-11T00:00:00Z",
      "started_at": null,
      "completed_at": null
    }
  ]
}
```

Counts describe the validated search results and what happened while persisting
them. A skipped result can be a repeated URL, a URL already stored, or a result
whose title cannot be normalized into a company name. Crawl failures include
the source URL and reason; failed sources are still saved without enrichment.
PageSpeed scan jobs include one job per website and strategy. Poll
`GET /api/v1/companies/{company_id}/pagespeed-jobs/{job_id}` to check whether a
scan is queued, running, succeeded, or failed. The discovery screen polls jobs
automatically and shows queue, start, finish, and elapsed times, as well as a
failure reason. Completed scan summaries and full Lighthouse reports appear in
the company's PageSpeed scan history. Retry a failed scan from the PageSpeed
queue page or with `POST /api/v1/pagespeed/jobs/{job_id}/retry`.

### Crawl and enrich a website

`POST /api/v1/crawler/crawl`

```json
{
  "website_url": "https://example.com"
}
```

The endpoint returns `202 Accepted` with a job ID and initial status. Poll
`GET /api/v1/crawler/jobs/{job_id}`; successful responses include the saved
homepage, About, Blog, Services, Contact, and mission/goal page text,
extracted email addresses, telephone numbers, social-media URLs, and the crawl
timestamp.
Re-crawling the same website
replaces its saved crawl result. The crawler starts at the supplied URL,
follows only same-site links, limits page count and response size, and rejects
private, local, non-standard-port, and cross-site redirect targets.

### Add a company manually

`POST /api/v1/companies`

```json
{
  "name": "Example Manufacturing Pvt Ltd",
  "website": "https://example.com",
  "domain": "www.example.com",
  "industry": "Manufacturing",
  "city": "Kolkata",
  "state": "West Bengal",
  "software_need": "Inventory management"
}
```

The `name` is required; the other fields are optional. Website values must be
valid URLs. The stored domain is normalized to its hostname (for example,
`www.example.com` becomes `example.com`); when `domain` is omitted, the
hostname is derived from `website` if available. A successful response has
status `201` and includes the created company fields, ID, status, and timestamp.
Manually created companies default to the `new` lead status.

### List companies

`GET /api/v1/companies?limit=50&offset=0`

Results are ordered newest first. `limit` must be between 1 and 100 and
`offset` must be non-negative. The response is a JSON array of company records,
including their saved contacts and lead sources.

### Read, update, and delete a company

- `GET /api/v1/companies/{company_id}` returns the company and all associated
  contacts and source evidence.
- `PATCH /api/v1/companies/{company_id}` updates any supplied company fields.
  Company names and domains are normalized; duplicate domains return `409`.
- `DELETE /api/v1/companies/{company_id}` removes the company and its related
  contacts and lead sources. It returns `204` on success.

Missing company IDs return `404`.

### Manage company contacts

Authenticated organization members can manage contacts belonging to companies
in their organization:

- `GET /api/v1/companies/{company_id}/contacts` lists a company's contacts.
- `POST /api/v1/companies/{company_id}/contacts` creates a contact. Provide at
  least one of `name`, `role`, `business_email`, or `business_phone`.
- `GET /api/v1/companies/{company_id}/contacts/{contact_id}` retrieves one
  contact.
- `PATCH /api/v1/companies/{company_id}/contacts/{contact_id}` updates supplied
  fields; nullable text fields can be set to `null` to clear them.
- `DELETE /api/v1/companies/{company_id}/contacts/{contact_id}` deletes the
  contact and returns `204`.

Contact reads and writes are scoped to both the company and the signed-in
organization. A contact belonging to another company is reported as `404`.
The Company detail page's **Contacts** tab provides the same create, edit, and
delete actions.

### Change lead status

`PATCH /api/v1/companies/{company_id}/status`

```json
{ "lead_status": "qualified" }
```

The status must be a non-empty string of at most 30 characters. The response
contains the updated company record, contacts, and lead sources.

### Operational responses

- `422 Unprocessable Entity` — invalid request data or pagination values.
- `409 Conflict` — a manually submitted company uses a domain that already
  exists.
- `503 Service Unavailable` — Tavily is not configured.
- `502 Bad Gateway` — the search provider returned an error or could not be
  reached.
- `504 Gateway Timeout` — the search provider did not respond within the
  configured timeout.

## Data model

- **Company** — company name and normalized matching key, website/domain,
  optional industry/location/software need, optional lead score, status, and
  creation timestamp.
- **SearchKeyword** — a recorded discovery query and timestamp linked to the
  websites surfaced by that search.
- **Contact** — optional company-linked contact details. Discovery currently
  and crawl workflows store extracted public email addresses and telephone
  numbers as separate contacts, deduplicated against existing contact values.
- **WebsiteCrawl** — standalone homepage and relevant About, Blog, Services,
  Contact, and mission/goal page content, email addresses, telephone numbers, social
  links, per-page classification signals, and crawl timestamp, stored
  independently of company detail fields. A matching company's Contact records
  are enriched separately when crawl data is saved. The crawler runs separate website
  type and topic classifiers; inferred topics populate an empty Website
  `site_topic` list without replacing existing topic values. Classification
  signals include page
  metadata, JSON-LD and Microdata types, article/password/shopping element
  counts, navigation and CTA text, URL-path patterns, and matched classifier
  terms.

Company names are matched through `normalized_name`; this helps discovery
results with different capitalization or punctuation map to one company.
Domains are independently unique when present. Company-name matching is not a
database uniqueness constraint, so manually created companies are not
automatically merged by name.

## Tests

Run the test suite from the repository root:

```powershell
python -m pytest
```

Tests use an in-memory SQLite database and override the application's database
dependency, so they do not write to your PostgreSQL database. The suite covers
company validation and CRUD behavior, discovery ingestion and deduplication,
website-crawler extraction and persistence, and transaction rollback behavior.

## Project layout

```text
app/
  api/          HTTP routes and database dependency
  schemas/      Pydantic request/response validation
  services/     Tavily search, ingestion, crawling, and normalization logic
  config.py     Environment-backed application settings
  database.py   SQLAlchemy engine, base, and sessions
  models.py     SQLAlchemy data models
  main.py       FastAPI application and health endpoint
migrations/     Alembic environment and schema revisions
test/           API, schema, and ingestion tests
```