"""publish queue job changes through PostgreSQL notifications

Revision ID: d7c58a1e4f20
Revises: f4a91c6d2e80
Create Date: 2026-11-14 17:16:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "d7c58a1e4f20"
down_revision: Union[str, Sequence[str], None] = "f4a91c6d2e80"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute(
        """
        CREATE FUNCTION notify_queue_job_event()
        RETURNS trigger AS $$
        DECLARE
            job_kind text;
        BEGIN
            IF TG_TABLE_NAME = 'website_crawl_jobs' THEN
                job_kind := 'crawler';
            ELSE
                job_kind := 'pagespeed';
            END IF;

            PERFORM pg_notify(
                'queue_job_events',
                json_build_object(
                    'kind', job_kind,
                    'id', NEW.id::text,
                    'status', NEW.status
                )::text
            );
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER website_crawl_job_events
        AFTER INSERT OR UPDATE ON website_crawl_jobs
        FOR EACH ROW EXECUTE FUNCTION notify_queue_job_event();
        """
    )
    op.execute(
        """
        CREATE TRIGGER pagespeed_scan_job_events
        AFTER INSERT OR UPDATE ON pagespeed_scan_jobs
        FOR EACH ROW EXECUTE FUNCTION notify_queue_job_event();
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute("DROP TRIGGER IF EXISTS pagespeed_scan_job_events ON pagespeed_scan_jobs")
    op.execute("DROP TRIGGER IF EXISTS website_crawl_job_events ON website_crawl_jobs")
    op.execute("DROP FUNCTION IF EXISTS notify_queue_job_event()")
