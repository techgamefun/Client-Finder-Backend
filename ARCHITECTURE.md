# Application Architecture

## Overview

The application is a synchronous-persistence, asynchronous-provider FastAPI
service. It exposes versioned HTTP routes, validates their inputs and outputs
with Pydantic, uses SQLAlchemy for relational storage, and calls Tavily for web
search. PostgreSQL is the configured application database; tests use an
in-memory SQLite database.

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
    +-- migrations/          Alembic-managed database schema history
```

- `app/config.py` loads application settings from environment variables and
  `.env`.
- `app/database.py` creates the SQLAlchemy engine, declarative base, and session
  factory.
- `app/api/deps.py` provides a database session per request and closes it when
  the request is complete.
- `app/models.py` defines `Company`, `Contact`, and `LeadSource` tables and
  their relationships.
- `app/schemas/` keeps API payload validation separate from ORM entities.
- `app/services/discovery.py` handles Tavily requests, provider failures, and
  validation of returned results.
- `app/services/lead_ingestion.py` matches search results to companies and
  persists new source evidence.
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
records in descending ID order with bounded pagination.

### Discovery search

`POST /api/v1/discovery/search` validates a query and result limit, then calls
`search_companies`. The service sends a basic-search request to Tavily and
validates each result. A missing provider key, timeout, provider HTTP error, or
network failure is surfaced as an HTTP error. This route does not persist data.

### Discovery ingestion

`POST /api/v1/discovery/ingest` uses the same validated request and Tavily
search service, then hands validated results to `ingest_search_results`.
Persistence is moved to Starlette's thread pool because the SQLAlchemy session
is synchronous:

1. Skip repeated source URLs within the search batch and URLs already stored.
2. Normalize each result title and find a company by `normalized_name`.
3. Create unmatched candidates with `needs_review` status; attach matching
   results to the existing company without changing its details or status.
4. Insert each `LeadSource` with `web_search` type, source URL, and evidence.
5. Commit the batch once. Roll back on failure. Retry once after an
   `IntegrityError` to handle concurrent uniqueness conflicts.
6. Return result, candidate, source, and skipped counts.

The database has a unique company domain and a globally unique lead-source URL.
Normalized company names are indexed for matching but are not unique, so
normalization-based matching occurs in the service rather than being enforced
as a database constraint.

## Database evolution

Alembic migrations are the source of truth for schema changes. The initial
revision creates the lead tables; subsequent revisions add normalized company
names and update lead-source URL uniqueness. A migration that backfills or
constrains existing data must account for incompatible existing rows and fail
clearly rather than silently discarding data.

## Design boundaries

- Search is an external side effect; database ingestion is a separate step, so
  callers can inspect search results without saving them.
- Services own the workflow and provider-specific behavior; route handlers
  handle HTTP input/output and dependency wiring.
- Ingestion stores candidates and evidence, not verified company facts,
  contacts, or scores. Review/enrichment should be implemented as explicit
  future workflows rather than implied by discovery.
