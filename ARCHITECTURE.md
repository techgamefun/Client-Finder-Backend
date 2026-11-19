# Application Architecture

## Overview

The application is a synchronous-persistence, asynchronous-provider FastAPI
service. It exposes versioned HTTP routes, validates their inputs and outputs
with Pydantic, uses SQLAlchemy for relational storage, and calls Tavily for web
search, Octoparse for template-based search, and Gemini for email-template
draft generation. Website crawl requests use
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
    |       +-- app/api/queue_events.py
    |               WebSocket stream for database-published job changes
    |       +-- app/api/email_campaigns.py
    |               Organization-scoped campaign lifecycle and progress
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
    +-- app/services/campaign_service.py
    |       Tenant validation and timezone-aware campaign scheduling
    |
    +-- app/services/campaign_worker.py
    |       Locked recipient claims, sandboxed rendering, and Gmail delivery
    |
    +-- app/worker.py
    |       Worker for crawl, PageSpeed, and email campaign jobs
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
  Octoparse template search. Provider keys saved through this API, including
  Gemini, are stored as plaintext in the per-user `UserProviderCredential` table.
  Provider selections, Octoparse template configuration, and per-crawl
  page/depth preferences are persisted in the organization's
  `ApplicationSettings` row. API responses never include key values; database
  access must be restricted because keys are not encrypted at rest.
- `app/services/application_settings.py` resolves database preferences and
  per-user provider keys, including the selected discovery tools and Octoparse
  template.
- `app/services/gemini_template_generator.py` requests an email-template draft
  from Gemini, validates the response schema, Jinja syntax, and available
  placeholder paths, and returns it without persistence.
- `app/models.py` defines the `Company`, `Contact`, `SearchKeyword`,
  `PageSpeedScan`, `WebsiteCrawl`, `Website`, `WebsiteCrawlJob`,
  `WebsiteCorpusAnalysis`, `ApplicationSettings`, `DiscoveryProviderJob`,
  `EmailCampaign`, and `EmailCampaignRecipient` tables.
- `WebsiteCrawl.site_type` stores the crawler's detected website type with the
  extracted crawl data. The separate topic classifier fills `Website.site_topic`
  when it is still empty. The `classification_signals` JSON column preserves
  bounded, per-page metadata, schema types, DOM counts, navigation/CTA text,
  URL patterns, matched metadata, URL, and body classification terms, plus
  each attempted page's HTTP status and error detail when available. Pages
  without classification signals are retained when they have crawl diagnostics.
- `Website` is the all-sites registry. It tracks discovery source/provider,
  detected crawl category, page type, site topic, and an optional crawl link.
  Listing sites are identified by `page_type="business_listing"`; discovery
  ingestion does not associate them with companies.
- `app/services/website_registry.py` centralizes website upserts and maps
  crawler categories to page types and listing discovery metadata.
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
  `DiscoveryProviderJob`. Its usage summary counts app-tracked Octoparse tasks
  for the current UTC month and all time; this is not account quota data.
- `app/services/lead_ingestion.py` matches search results to companies, crawls
  source URLs, fills verified website/domain details, imports extracted public
  email and phone values into deduplicated Contact records, and persists source
  and crawl evidence.
- `app/services/website_crawler.py` provides bounded public-site crawling to
  discovery ingestion and the standalone crawl endpoint using headless
  Chromium; crawled page text, emails, phone numbers, social links, detected
  site type, and detailed per-page classification signals are stored in
  `WebsiteCrawl` records. When a crawled domain matches an existing company,
  public email and phone values are imported as separate, deduplicated Contact
  records without replacing existing contact details. Company profiles can
  retrieve the latest successful crawl for their website and trigger a new
  crawl or re-crawl. It
  retries temporary rate-limit and server failures a bounded number of times,
  but reports denied access without attempting to bypass it.
- `app/services/crawl_jobs.py` creates durable PostgreSQL crawl jobs and
  dispatches their IDs through Upstash Redis when available. CSV imports use a
  batch endpoint that commits jobs together and dispatches IDs in one Redis
  request. Batch retry endpoints update eligible crawl or PageSpeed jobs
  together and dispatch their IDs once. `app/worker.py` consumes jobs, persists
  crawl results, records failures, and recovers queued jobs from PostgreSQL
  when Redis dispatch is unavailable. Every successful
  queued crawl creates mobile and desktop PageSpeed jobs linked to its Website
  registry record and, when present, a matching company. Discovery ingestion
  remains synchronous because its results are required by that workflow. Set
  `RUN_WORKER_IN_API=true` to run the worker in the API
  lifespan when a separate background-worker service is unavailable.
  PageSpeed jobs remain queued when their creating user has no saved PageSpeed
  key; worker claims and recovery skip those jobs, and retry endpoints reject
  retries until that user's key is configured. Google validates a saved key
  when the scan request runs.
- `app/services/campaign_service.py` validates assigned Gmail accounts and
  tenant-owned companies/contacts, previews campaign capacity, and schedules
  recipients round-robin within weekday send windows in each campaign's
  timezone. `app/services/campaign_worker.py` claims due rows with PostgreSQL
  `FOR UPDATE SKIP LOCKED`, reuses the sandboxed template renderer and Gmail
  sender, and records delivery outcomes. Successful delivery updates
  `Company.last_email_sent_at`; this email-contact marker is distinct from the
  sales pipeline's `Company.lead_status`.
- `app/services/gmail.py` uses assigned-account checks for Gmail message reads,
  replies, composed sends, label listing, and label-filtered message lists.
- `POST /api/v1/orgs/{org_id}/templates/generate-draft` is admin-only and uses
  the authenticated user's Gemini credential. The generated draft is returned
  for review in the editor; it is not saved or sent automatically.
- PostgreSQL triggers publish crawl, PageSpeed, and email campaign status
  changes on the
  `queue_job_events` notification channel. `app/api/queue_events.py` forwards
  those notifications over `/api/v1/queue/events`, so the frontend can update
  active job views without periodic status polling.
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
sources. Authenticated organization members can independently list, create,
read, update, and delete company contacts through
`/api/v1/companies/{id}/contacts`; each operation verifies both the company and
contact belong to the active organization and scopes contacts to their parent
company.

### Discovery search

`POST /api/v1/discovery/search` validates a query and result limit, then calls
the tools selected in Settings. Tavily returns validated results immediately;
Octoparse starts a configured template task and returns a durable provider-job
ID. The successful search is recorded with its keyword and timestamp, and its
ID is returned for the later website review request. Provider failures are
included in the response when another selected tool succeeds or starts a job; if all
selected tools fail, the API returns an error. `GET
/api/v1/discovery/jobs/{job_id}` polls an Octoparse export and persists its
status and mapped results. `GET /api/v1/discovery/suggestions` returns up to
ten recent unique keywords matching the supplied query. Search results are
kept transient until the user saves them for website review.

### Domain registry and discovery crawl queue

The Discover leads page's save action sends only result URLs and providers to
`POST /api/v1/discovery/save-websites`; provider titles and snippets are not
stored or sent to the save endpoint. URLs are reduced to canonical HTTPS
origins and deduplicated by normalized domain. The `websites` table has a
unique domain constraint, defaults to status `"new"`, has no company foreign
key, and stores a `site_topic` list. Website statuses are `new`, `lead`, and
`checked`. Repeated discoveries reuse the same domain row.

New domains are sent to the existing `website_crawl_jobs` queue. Crawl state
remains solely on those job rows; a domain with an active job or saved crawl
does not get another discovery crawl queued. The worker persists successful
crawl data and links it to the domain registry row. The Websites page displays
the crawl signals and allows editing website type, site topic, and language.
This discovery flow does not create companies or queue PageSpeed scans.

### Discovery ingestion

`POST /api/v1/discovery/ingest` remains available for legacy clients that
create and enrich company records directly. It accepts search results (or runs
Tavily search for older clients that omit them), crawls each distinct source
URL with a maximum of ten concurrent crawls, then hands the results and
successful crawls to `ingest_search_results`. Persistence is moved to
Starlette's thread pool because the SQLAlchemy session is synchronous:

1. Skip repeated source URLs within the search batch.
2. Normalize each result title and find a company by `normalized_name`.
3. Create unmatched candidates with `needs_review` status; attach matching
   results to the existing company without changing its status.
4. Associate the saved search keyword and its timestamp with each matching
   website in the domain-unique registry.
5. Use successful crawls to fill missing website/domain fields, add publicly
   listed email and phone values as separate Contact records, and save crawled
   page text and extracted metadata. Contact values are deduplicated without
   overwriting existing contacts; existing company details are not overwritten.
6. Create durable mobile and desktop PageSpeed jobs for each distinct company
   website and enqueue them for the worker. Return without waiting for Google;
   the worker saves category scores and detailed Lighthouse report history or
   records a failed job for status lookup. Jobs record queue, start, and finish
   timestamps so the frontend can show elapsed time and failure details.
7. Commit the database changes as one batch. Roll back on failure. Retry once after an
   `IntegrityError` to handle concurrent uniqueness conflicts.
8. Register each result and successful crawl in the domain-unique `Website`
   registry. Website rows are not linked to companies; listing sites still do
   not receive company PageSpeed jobs.
9. Return result, candidate, enrichment, PageSpeed, and skipped counts, plus
   any crawl or PageSpeed failures.

`GET /api/v1/websites` lists the canonical URL, normalized domain, permanent
review status, page type, site topic, language, discovery source/provider, and crawl
status. It supports bounded `limit`/`offset` pagination, page type, and search
filters. `GET /api/v1/websites/{id}` returns the website record, associated discovery
keywords, stored crawl page content, classification signals, contact/social
links, crawl-job history, and the ten most recent PageSpeed score summaries for
the website detail page. Lighthouse audit reports are loaded on demand from
`/api/v1/websites/{id}/pagespeed-scans/{scan_id}`. `GET
/api/v1/websites/corpus-words` gathers saved page titles and descriptions from
successful crawls, asks the stateless Go crawler service to analyze each
domain independently, and caches each domain's top 50 keywords in
`WebsiteCorpusAnalysis` until that site's crawl or analyzer version changes.
The Websites page displays each site's own keyword phrases and scores; words
are not ranked or counted across domains. It can add all currently filtered,
eligible websites to Leads, creating companies with their website URL/domain
and marking their registry status as `lead`. Listing and uncrawled websites
are excluded.

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
or redirects. Extracted page text, emails, phone numbers, and social URLs are upserted into
the separate `website_crawls` table. When a crawl matches an existing company
domain, its public emails and phone numbers are imported as separate Contact
records with deduplication. Discovery ingestion continues using synchronous
crawling.
Company profiles, discovery results, direct crawl jobs, and successful crawls
also upsert the `websites` registry by normalized domain. Website rows are
never linked to companies; the crawl relation is maintained independently.
Company profiles can read their latest successful crawl at
`GET /api/v1/companies/{id}/website-crawl` and start or repeat a crawl with
`POST /api/v1/companies/{id}/website-crawl`. These routes use the company's
website, falling back to its domain, and return no status when no successful
crawl has been stored yet.
Discovery keywords are associated with Website rows and displayed on website
profiles; they are no longer stored on companies. Company records do not retain
research-source URLs or evidence.

The database has a unique company domain. Normalized company names are indexed
for matching but are not unique, so
normalization-based matching occurs in the service rather than being enforced
as a database constraint.

## Database evolution

Alembic migrations are the source of truth for schema changes. The initial
revision creates the lead tables; subsequent revisions add normalized company
names, update lead-source URL uniqueness, track discovery search keywords and
their company associations, save website crawls, store PageSpeed reports,
convert entity primary keys and their foreign keys to UUIDs while preserving
existing relationships, add durable website crawl jobs, persist discovery
tool preferences and Octoparse provider jobs, and backfill the website
registry from existing crawl, company, lead-source, and crawl-job records.
Later revisions allow PageSpeed jobs and reports to belong to Website registry
rows as well as companies. The latest registry migration removes the redundant
`Website.site_type` field while preserving listing classification in
`page_type`.
A migration that backfills or constrains existing data must account for
incompatible existing rows and fail clearly rather than silently discarding
data.

## Design boundaries

- Search results are returned for review; the query and timestamp are recorded
  immediately, while company associations are saved when results are ingested.
- PageSpeed reports are saved during discovery ingestion and loaded in full on
  demand from the company pipeline. Successful queued crawls also enqueue
  independent scans, which are associated with their Website and any matching
  company.
- Services own the workflow and provider-specific behavior; route handlers
  handle HTTP input/output and dependency wiring.
- Ingestion only fills company values explicitly discovered by the bounded
  crawl; industry, location, software need, and lead score remain untouched
  unless separately provided or reviewed.
