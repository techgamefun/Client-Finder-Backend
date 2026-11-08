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
2. **Web discovery** — either search without saving using
   `POST /api/v1/discovery/search`, or search and save in one operation using
   `POST /api/v1/discovery/ingest`.

The ingestion workflow runs as follows:

1. Validate the query (3–300 characters) and requested result count (1–10;
   default 5).
2. Send the query to Tavily using its basic search endpoint. Search results
   with missing or invalid fields are discarded; provider timeouts and errors
   are returned as API errors rather than being treated as empty results.
3. For each valid result, use its URL as the source identity. A URL already
   present in the batch or database is skipped.
4. Normalize the result title and look for a company with the same normalized
   name. Normalization applies Unicode compatibility normalization, case
   folding, punctuation removal, and whitespace collapsing.
5. If no matching company exists, create one with a `needs_review` lead status.
   If a company already exists, attach the new source to it without changing
   its existing status or details.
6. Save a `web_search` lead source containing the source URL and search-result
   evidence. The batch is committed as a unit; database integrity conflicts
   trigger a retry, and other failures roll back the batch.
7. Return counts for search results, newly created candidates, sources added,
   and results skipped. New discovery candidates are intended for human review;
   discovery does not verify companies or populate contact details or a lead
   score.

## Requirements

- Python 3.10 or newer
- PostgreSQL
- A Tavily API key for the discovery endpoints

The current dependency file is named `requirments.txt` (spelling as in the
repository). Alembic is used for schema migrations but is not listed in that
file, so install it separately if you need to run migrations.

## Local setup

Run these commands from the repository root in PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r .\requirments.txt
python -m pip install alembic
```

If PowerShell prevents activation due to its execution policy, use the virtual
environment interpreter directly as `.\.venv\Scripts\python.exe`.

Create a `.env` file in the repository root (do not commit it):

```dotenv
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DATABASE
TAVILY_API_KEY=your-tavily-api-key
ENVIRONMENT=development
```

`DATABASE_URL` is required when the application starts. `TAVILY_API_KEY` is
only needed for web discovery; without it, discovery requests return
`503 Service Unavailable`. The database layer uses psycopg for PostgreSQL.
Keep real credentials in your local environment and out of source control.

## Database setup

Apply the migrations to the configured PostgreSQL database:

```powershell
alembic upgrade head
```

The migration chain creates the company, contact, and lead-source tables, then
adds the normalized company-name column and index, fills that column for
existing companies, and enforces globally unique source URLs. If the unique
source-URL migration detects duplicates in existing data, it stops with an
error; resolve those duplicates before rerunning the migration.

To see the current migration revision:

```powershell
alembic current
```

## Start the API

```powershell
uvicorn app.main:app --reload
```

By default, Uvicorn listens on `http://127.0.0.1:8000`.

- Health check: `GET http://127.0.0.1:8000/health`
- Interactive API documentation: `http://127.0.0.1:8000/docs`
- Alternative API documentation: `http://127.0.0.1:8000/redoc`

## API usage

All versioned endpoints use the `/api/v1` prefix.

### Search without saving

`POST /api/v1/discovery/search`

```json
{
  "query": "manufacturing companies in Kolkata",
  "max_results": 5
}
```

The response contains the original query and validated results with `title`,
`url`, `content`, and provider `score`. This endpoint does not write to the
database.

### Search and ingest results

`POST /api/v1/discovery/ingest`

Use the same request shape as the search endpoint. An example success response:

```json
{
  "query": "manufacturing companies in Kolkata",
  "results_found": 5,
  "candidates_created": 3,
  "sources_added": 4,
  "results_skipped": 1
}
```

Counts describe the validated search results and what happened while persisting
them. A skipped result can be a repeated URL, a URL already stored, or a result
whose title cannot be normalized into a company name.

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
`offset` must be non-negative. The response is a JSON array of company records.

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
- **LeadSource** — a company-linked source URL, source type, evidence, optional
  verification timestamp, and creation timestamp. Source URLs are unique
  across companies.
- **Contact** — optional company-linked contact details. Discovery currently
  does not create contacts.

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
and transaction rollback behavior.

## Project layout

```text
app/
  api/          HTTP routes and database dependency
  schemas/      Pydantic request/response validation
  services/     Tavily search, ingestion, and normalization logic
  config.py     Environment-backed application settings
  database.py   SQLAlchemy engine, base, and sessions
  models.py     SQLAlchemy data models
  main.py       FastAPI application and health endpoint
migrations/     Alembic environment and schema revisions
test/           API, schema, and ingestion tests
```