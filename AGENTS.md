# Project Guidelines

These instructions apply to coding agents working in this repository.

## Before changing code

- Read [ARCHITECTURE.md](./ARCHITECTURE.md) and [CONVENTIONS.md](./CONVENTIONS.md)
  for project structure, data flow, and implementation patterns.
- Inspect the relevant implementation and tests before editing. Preserve
  unrelated worktree changes.
- Keep changes focused. Update tests and directly related documentation when
  behavior or public API contracts change.

## Implementation

- Keep HTTP concerns in `app/api/`, request/response validation in
  `app/schemas/`, business logic in `app/services/`, and persistence definitions
  in `app/models.py`.
- Preserve explicit validation and error behavior. Do not silently hide
  provider, database, or input errors.
- Schema changes must include an Alembic migration under `migrations/versions/`.

## Verification

- Run the smallest relevant test set; run the full suite with
  `python -m pytest` when practical.
- For API or database changes, confirm the applicable tests cover response
  behavior and persistence/rollback behavior.
- Do not run migrations against a shared or production database without
  explicit authorization.

See [README.md](./README.md) for setup, API usage, and the lead-ingestion
workflow.
