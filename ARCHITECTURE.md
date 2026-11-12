# Application Architecture

## Overview

The application is a synchronous-persistence, asynchronous-provider FastAPI
service. It exposes versioned HTTP routes, validates their inputs and outputs
with Pydantic, uses SQLAlchemy for relational storage, and calls Tavily for web
search and Octoparse for template-based search. Website crawl requests use
Upstash Redis to dispatch jobs to a
separately run worker; PostgreSQL stores durable job state and crawl results.
PostgreSQL is the configured application database; tests use an in-memory
SQLite database.

## Component boundaries

```text
HTTP client
    |
    v
app/main.py                 FastAPI app, router registration, health check
    |
    +-- app/api/             HTTP routes and request-scoped DB dependency
    |       |
    |       +-- app/schemas/ Pydantic request/response contracts
    |       |
    |       +-- app/services/ business workflows and provider integration
    |                           |
    |                           +-- app/models.py
    |                           +-- app/database.py
    |
    +-- app/services/website_crawler.py
    |       Bounded same-site page crawling and contact/social extraction
    |
    +-- app/services/redis_queue.py
    |       Upstash Redis REST queue transport
    |
    +-- app/worker.py
    |       Separate worker process for queued website crawl jobs
    |
    +-- migrations/          Alembic-managed database schema history
```

- `app/config.py` loads application settings from environment variables and
  `.env`.
- `app/database.py` creates the SQLAlchemy engine, declarative base, and session
  factory.
- Database entity primary keys use UUIDs; foreign keys and API IDs use the same
  UUID representation. The discovery company-keyword link uses a composite
  primary key made from UUID foreign keys.
- `app/api/deps.py` provides a database session per request and closes it when
  the request is complete.
- `app/api/settings.py` exposes public settings operations, Tavily usage, and
  Octoparse template search. Tavily and Octoparse keys saved through this API
  are Fernet-encrypted in `ApplicationSettings`; provider selections,
  Octoparse template configuration, and per-crawl page/depth preferences are
  persisted in the same row. Anyone who can reach the API can modify these
  settings, so deployments should restrict network access appropriately.
- `app/services/application_settings.py` resolves database preferences and
  environment fallbacks for Tavily and Octoparse, including the selected
  discovery tools and Octoparse template.
- `app/models.py` defines the `Company`, `Contact`, `LeadSource`,
  `PageSpeedScan`, `WebsiteCrawl`, `WebsiteCrawlJob`, `ApplicationSettings`,
  and `DiscoveryProviderJob` tables and their relationships.
- `WebsiteCrawl.site_type` stores the crawler's detected website category with
  the extracted crawl data.
- `app/schemas/` keeps API payload validation separate from ORM entities.
- `app/services/discovery.py` defines the lead-discovery tool contract,
  executes the selected Tavily and Octoparse tools concurrently, validates
  results, and reports provider-specific failures. Tavily returns results
  immediately; Octoparse starts a task that is polled separately. The service
  also retrieves live Tavily usage for the Settings page. The business-listing
  category is present in the tool taxonomy but has no configured provider yet.
- `app/services/octoparse.py` searches templates, starts configured Octoparse
  tasks, polls their exports, maps template-specific output fields into
  discovery results, and persists task status and results through
  `DiscoveryProviderJob`.
- `app/services/lead_ingestion.py` matches search results to companies, crawls
  source URLs, fills verified website/domain and public-email details, and
  persists source and crawl evidence.
- `app/services/website_crawler.py` provides bounded public-site crawling to
  discovery ingestion and the standalone crawl endpoint using headless
  Chromium; crawled page text, emails, social links, and detected site type are
  stored in `WebsiteCrawl` records. Company profiles can retrieve the latest
  successful crawl for their website and trigger a new crawl or re-crawl. It
  retries temporary rate-limit and server failures a bounded number of times,
  but reports denied access without attempting to bypass it.
- `app/services/crawl_jobs.py` creates durable PostgreSQL crawl jobs and
  dispatches their IDs through Upstash Redis when available. `app/worker.py`
  consumes jobs, persists crawl results, records failures, and recovers queued
  jobs from PostgreSQL when Redis dispatch is unavailable. Discovery and
  research-source crawls remain synchronous because their results are required
  by those workflows.
- `app/services/research_source_crawler.py` crawls URLs entered on a company
  profile, saves source and crawl evidence, and fills only missing website,
  domain, and public contact details that can be inferred from the source.
- `app/services/pagespeed.py` requests mobile and desktop Google PageSpeed
  Insights reports for the Lighthouse categories and returns detailed results.
- `app/services/normalization.py` contains shared company-name and domain
  normalization.
- `migrations/versions/` records schema changes; `migrations/env.py` loads
  application metadata for Alembic.
- `test/` exercises schemas, API routes, and service workflows. The shared
  `client` fixture overrides the app's database dependency with SQLite.

## Request and data flows

### Company entry and listing

`POST /api/v1/companies` validates a `CompanyCreate` payload, derives a
normalized company-name key, normalizes an optional domain (falling back to the
website hostname), and inserts the company. A domain uniqueness violation is
rolled back and translated to `409 Conflict`. `GET /api/v1/companies` reads
records in descending creation-time order with bounded pagination and includes related
contacts and source evidence, including each source URL's normalized domain.
`GET /api/v1/companies/{id}` reads one complete
record; `PATCH /api/v1/companies/{id}` updates supplied company fields while
maintaining normalized names/domains and domain uniqueness; the dedicated
`PATCH /api/v1/companies/{id}/status` changes lead status. `DELETE
/api/v1/companies/{id}` deletes a company and its dependent contacts and
sources.

### Discovery search

`POST /api/v1/discovery/search` validates a query and result limit, then calls
the tools selected in Settings. Tavily returns validated results immediately;
Octoparse starts a configured template task and returns a durable provider-job
ID. The successful search is recorded with its keyword and timestamp, and its
ID is returned for the later ingestion request. Provider failures are included
in the response when another selected tool succeeds or starts a job; if all
selected tools fail, the API returns an error. `GET
/api/v1/discovery/jobs/{job_id}` polls an Octoparse export and persists its
status and mapped results. `GET /api/v1/discovery/suggestions` returns up to
ten recent unique keywords matching the supplied query.

### Discovery ingestion

`POST /api/v1/discovery/ingest` accepts the displayed search results (or runs
Tavily search for older clients that omit them), crawls each distinct source
URL with a maximum of ten concurrent crawls, then hands the results and
successful crawls to `ingest_search_results`. Persistence is moved to
Starlette's thread pool because the SQLAlchemy session is synchronous:

1. Skip repeated source URLs within the search batch and URLs already stored.
2. Normalize each result title and find a company by `normalized_name`.
3. Create unmatched candidates with `needs_review` status; attach matching
   results to the existing company without changing its status.
4. Associate the saved search keyword and its timestamp with every matched
   company, including companies that already have a source URL.
5. Use successful crawls to fill missing website/domain fields, add publicly
   listed email addresses as contacts, and save crawled page text and extracted
   metadata. Existing company details are not overwritten.
6. Create durable mobile and desktop PageSpeed jobs for each distinct company
   website and enqueue them for the worker. Return without waiting for Google;
   the worker saves category scores and detailed Lighthouse report history or
   records a failed job for status lookup. Jobs record queue, start, and finish
   timestamps so the frontend can show elapsed time and failure details.
7. Insert each `LeadSource` with `web_search` type, source URL, normalized
   source domain, and combined search/crawl evidence.
8. Commit the database changes as one batch. Roll back on failure. Retry once after an
   `IntegrityError` to handle concurrent uniqueness conflicts.
9. Return result, candidate, source, enrichment, PageSpeed, and skipped counts,
   plus any crawl or PageSpeed failures. Provider failures do not prevent
   sources from being saved.

### Website crawling

`POST /api/v1/crawler/crawl` takes a public website URL and creates a durable
job in PostgreSQL, then dispatches its ID through Upstash Redis. It returns
`202 Accepted`; clients poll `GET /api/v1/crawler/jobs/{job_id}` for `queued`,
`running`, `succeeded`, or `failed` status and receive the crawl result when
successful. `GET /api/v1/crawler/jobs` lists recent jobs for queue monitoring.
Job responses include queued, started, and completed timestamps. Company profile
crawls use the same job queue. A separately
started `python -m app.worker` process consumes jobs and calls the existing
crawler service. The worker can recover abandoned jobs after a restart. This
workflow runs independently from discovery search and lead ingestion. The
crawler starts at the supplied URL, then fetches up to five discovered
same-site About, Blog, Services, Contact, and mission/goal pages. It limits
response size, request duration, page count, and redirects; blocks non-public
IP addresses; and does not follow cross-site links
or redirects. Extracted page text, emails, and social URLs are upserted into
the separate `website_crawls` table. Discovery ingestion and research-source
enrichment continue using synchronous crawling.
Company profiles can read their latest successful crawl at
`GET /api/v1/companies/{id}/website-crawl` and start or repeat a crawl with
`POST /api/v1/companies/{id}/website-crawl`. These routes use the company's
website, falling back to its domain, and return no status when no successful
crawl has been stored yet.
The Research sources cards on a company profile can invoke
`POST /api/v1/companies/{id}/sources/crawl` with their saved source URL. The
route crawls it with the same public-host and resource limits. A successful
crawl saves the source and crawl evidence, adds company-associated public
email addresses and telephone links as contacts, and infers website and domain
only when the company name or an associated corporate email/link supports that
association. Existing company fields are never overwritten.

The database has a unique company domain and a globally unique lead-source URL.
Normalized company names are indexed for matching but are not unique, so
normalization-based matching occurs in the service rather than being enforced
as a database constraint.

## Database evolution

Alembic migrations are the source of truth for schema changes. The initial
revision creates the lead tables; subsequent revisions add normalized company
names, update lead-source URL uniqueness, track discovery search keywords and
their company associations, save website crawls, store PageSpeed reports,
convert entity primary keys and their foreign keys to UUIDs while preserving
existing relationships, add durable website crawl jobs, and persist discovery
tool preferences and Octoparse provider jobs.
A migration that backfills or constrains existing data must account for
incompatible existing rows and fail clearly rather than silently discarding
data.

## Design boundaries

- Search results are returned for review; the query and timestamp are recorded
  immediately, while company associations are saved when results are ingested.
- PageSpeed reports are saved during discovery ingestion and loaded in full on
  demand from the company pipeline.
- Services own the workflow and provider-specific behavior; route handlers
  handle HTTP input/output and dependency wiring.
- Ingestion only fills company values explicitly discovered by the bounded
  crawl; industry, location, software need, and lead score remain untouched
  unless separately provided or reviewed.
