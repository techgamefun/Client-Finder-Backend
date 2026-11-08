from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.models import Company, LeadSource
from app.schemas.discovery import DiscoveryResult, DiscoverySearchRequest
from app.services.discovery import search_companies
from app.services.normalization import normalize_company_name


def _persist_search_results(
    results: Sequence[DiscoveryResult],
    db: Session,
) -> tuple[int, int, int]:
    for attempt in range(2):
        companies_created = 0
        sources_added = 0
        skipped = 0
        seen_urls: set[str] = set()

        try:
            for result in results:
                source_url = str(result.url)
                if source_url in seen_urls:
                    skipped += 1
                    continue
                seen_urls.add(source_url)

                existing_source = db.scalar(
                    select(LeadSource.id)
                    .where(LeadSource.source_url == source_url)
                    .limit(1)
                )
                if existing_source is not None:
                    skipped += 1
                    continue

                candidate_name = result.title.strip()[:200]
                normalized_name = normalize_company_name(candidate_name)
                if not candidate_name or not normalized_name:
                    skipped += 1
                    continue

                company = db.scalar(
                    select(Company)
                    .where(Company.normalized_name == normalized_name)
                    .limit(1)
                )
                if company is None:
                    company = Company(
                        name=candidate_name,
                        normalized_name=normalized_name,
                        lead_status="needs_review",
                    )
                    db.add(company)
                    db.flush()
                    companies_created += 1

                db.add(
                    LeadSource(
                        company_id=company.id,
                        source_url=source_url,
                        source_type="web_search",
                        evidence=result.content,
                    )
                )
                sources_added += 1

            db.commit()
            return companies_created, sources_added, skipped
        except IntegrityError:
            db.rollback()
            if attempt == 1:
                raise
        except Exception:
            db.rollback()
            raise

    raise RuntimeError("Could not persist search results after retry.")


async def ingest_search_results(
    request: DiscoverySearchRequest,
    db: Session,
) -> dict[str, int | str]:
    search_response = await search_companies(
        query=request.query,
        max_results=request.max_results,
    )
    companies_created, sources_added, skipped = await run_in_threadpool(
        _persist_search_results,
        search_response.results,
        db,
    )

    return {
        "query": request.query,
        "results_found": len(search_response.results),
        "candidates_created": companies_created,
        "sources_added": sources_added,
        "results_skipped": skipped,
    }
