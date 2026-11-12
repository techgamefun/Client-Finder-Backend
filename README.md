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
   crawls fill missing website/domain fields and add publicly listed email
   contacts without overwriting existing company details. Crawled page text is
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
mission/goal pages, then stores their page URLs and text, email addresses, and
social links in the `website_crawls` table. It does not modify company or
lead-source records by itself. It uses bounded same-site HTTP requests;
JavaScript-only pages fail explicitly because browser rendering is not enabled.
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

On a company profile, select **Crawl and enrich** on an existing Research
source card to crawl its saved URL and find company website/domain details and
public contact email addresses or telephone links. The source and crawl
evidence are saved to the company; existing details are left unchanged, and
website/domain values are filled only when the source provides a credible
company association. The API endpoint is
`POST /api/v1/companies/{id}/sources/crawl` with a `source_url` field.

When discovery results are saved to the pipeline, mobile and desktop Google
PageSpeed Insights scans are queued for each distinct company website. The
ingest response returns without waiting for those scans. The worker saves
category scores and full Lighthouse reports with the company; failed scans
remain visible through their job status. Configure
`GOOGLE_PAGESPEED_API_KEY` in the backend `.env` file.

## Requirements

- Python 3.10 or newer
- PostgreSQL
- Upstash Redis for queued website crawl jobs
- A Tavily API key for the discovery endpoints
- A Google PageSpeed API key for PageSpeed scans during discovery ingestion

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
TAVILY_API_KEY=your-tavily-api-key
OCTOPARSE_API_KEY=your-octoparse-api-key
SETTINGS_ENCRYPTION_KEY=generated-fernet-key
GOOGLE_PAGESPEED_API_KEY=your-google-pagespeed-api-key
UPSTASH_REDIS_REST_URL=https://your-upstash-database.upstash.io
UPSTASH_REDIS_REST_TOKEN=your-upstash-rest-token
ENVIRONMENT=development
FRONTEND_ORIGINS=http://localhost:3000
```

`DATABASE_URL` is required when the application starts. Tavily and Octoparse
can be configured with `TAVILY_API_KEY` and `OCTOPARSE_API_KEY`, respectively,
or from the public Settings page. The database layer uses psycopg for
PostgreSQL.
The website crawler uses Playwright Chromium; install the browser on every
backend host with `python -m playwright install chromium`.
`GOOGLE_PAGESPEED_API_KEY` is used by the backend when discovery results are
saved to the pipeline; it is never sent to the frontend.
`SETTINGS_ENCRYPTION_KEY` is a Fernet key used to encrypt Tavily and Octoparse
keys saved from the page; keep it stable and backed up, because changing or
losing it makes saved keys unreadable. Generate an encryption key with:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

The Settings page and its API are public and require no admin token. Anyone
who can reach the application can view and change crawler limits, discovery
tool preferences, and provider keys. Keys are never returned by the API, but
the settings API must not be exposed to untrusted networks. Use network access
controls and HTTPS if deploying it beyond a trusted network. The Settings page
can query Tavily's usage endpoint for current-cycle credits and limits. Keys
saved there are encrypted in PostgreSQL and are never returned to the browser.
`TAVILY_API_KEY` and `OCTOPARSE_API_KEY` remain deployment-level fallbacks; if
a database key is saved, it takes precedence.
The Settings page can search Octoparse templates and configure the template's
query input, additional JSON parameters, and output-field mappings. Octoparse
searches start provider jobs; the Discover Leads page polls those jobs and
combines completed results with the selected Tavily results. The chosen search
engines are saved on the backend. The Business listing websites category is
shown as coming soon until a provider is configured.
The page also sets maximum pages and depth for new crawls (up to 6 pages and
depth 2); the crawler service may enforce lower deployment limits. Address,
Chrome path, response/timeouts, queue, retries, rate limit, user agent, and Go
runtime memory/CPU settings remain deployment-managed and require a service
restart to change.
`UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` enable immediate
dispatch of website crawl and PageSpeed jobs through Upstash Redis. PostgreSQL
stores durable job status and results; if Redis is unavailable, the running
worker recovers queued jobs from the database.
`FRONTEND_ORIGINS` is a comma-separated list of frontend origins allowed to
call the API; it defaults to `http://localhost:3000,http://127.0.0.1:3000`.
Keep real credentials in your local environment and out of source control.

## Database setup

Apply the migrations to the configured PostgreSQL database:

```powershell
alembic upgrade head
```

The migration chain creates the company, contact, and lead-source tables, adds
the normalized company-name column and index, enforces globally unique source
URLs, adds timestamped search keywords and their company associations, and
stores per-company PageSpeed reports, converts entity IDs and their foreign
keys to UUIDs while preserving existing records and relationships, adds
durable website crawl and PageSpeed scan jobs, and adds persisted discovery
tool preferences and Octoparse provider jobs.
If the unique source-URL migration detects duplicates in existing data, it
stops with an error; resolve those duplicates before rerunning the migration.

To see the current migration revision:

```powershell
alembic current
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

## API usage

All versioned endpoints use the `/api/v1` prefix.

### Search and keyword suggestions

`POST /api/v1/discovery/search`

```json
{
  "query": "manufacturing companies in Kolkata",
  "max_results": 5
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
  "sources_added": 4,
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
extracted email addresses and social-media URLs, and the crawl timestamp.
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
- **LeadSource** — a company-linked source URL and normalized source domain,
  source type, evidence, optional verification timestamp, and creation
  timestamp. Source URLs are unique across companies.
- **Contact** — optional company-linked contact details. Discovery currently
  does not create contacts.
- **WebsiteCrawl** — standalone homepage and relevant About, Blog, Services,
  Contact, and mission/goal page content, email addresses, social
  links, and crawl timestamp, stored independently of company and lead data.

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