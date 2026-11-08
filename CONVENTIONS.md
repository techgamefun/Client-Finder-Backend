# Coding Conventions

## Python and structure

- Use type annotations for function parameters and return values. Prefer
  built-in generic types such as `list[Company]` and `dict[str, int]`.
- Keep imports grouped as standard library, third-party packages, then
  application modules.
- Follow the existing `app/` boundaries:
  - `api/` for FastAPI routers and dependency wiring
  - `schemas/` for Pydantic API contracts
  - `services/` for business workflows and integrations
  - `models.py` for SQLAlchemy ORM entities
- Keep functions focused and use existing shared helpers before adding
  duplicate logic.

## API conventions

- Register versioned routers in `app/main.py` under `/api/v1`.
- Define request and response models in `app/schemas/`; use explicit
  `response_model` declarations where the route returns a defined contract.
- Let Pydantic enforce payload constraints. For additional route-level
  constraints, return an explicit FastAPI `HTTPException` with an appropriate
  status and clear detail.
- Keep external-provider error cases explicit. Translate expected network,
  timeout, or provider-status failures into appropriate HTTP errors; do not
  return success-shaped empty data for failed requests.
- Use the `get_db` dependency for request-scoped SQLAlchemy sessions.

## Database and transactions

- Keep ORM models in `app/models.py`, including relationship, uniqueness, and
  index definitions where appropriate.
- Use SQLAlchemy `select()` and session methods for queries and writes.
- Roll back failed transactions before returning or raising a translated
  database error. Do not swallow unexpected exceptions.
- Add an Alembic revision for every schema change. Review upgrade and downgrade
  behavior, including the impact on existing rows and constraints.
- Preserve existing data when backfilling or introducing uniqueness; detect
  conflicts and report them rather than dropping records silently.

## Normalization and deduplication

- Reuse `app/services/normalization.py` for company-name and domain
  normalization.
- Keep normalization deterministic and separate from API formatting.
- Treat source URL uniqueness and company-name matching as distinct rules:
  source URLs are unique in storage, while normalized company names are a
  matching key and are not globally unique at the database layer.

## Tests

- Place tests under `test/` and use the shared `client` fixture for HTTP
  behavior. It configures an isolated in-memory SQLite database.
- Test observable behavior, including response status/body and persisted
  records. For transaction-sensitive behavior, verify rollback as well as the
  raised or returned error.
- Mock external providers; tests should not require real Tavily credentials or
  network access.
- Run tests with `python -m pytest` from the repository root.

## Documentation and configuration

- Update [README.md](./README.md) when setup steps, API behavior, or the
  end-to-end user workflow changes.
- Keep architecture changes reflected in [ARCHITECTURE.md](./ARCHITECTURE.md).
- Read credentials and deployment-specific values from environment settings;
  never commit real secrets or `.env` contents.
