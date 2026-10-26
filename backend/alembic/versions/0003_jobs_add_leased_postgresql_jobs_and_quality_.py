"""Add leased PostgreSQL jobs and quality results

Revision ID: 0003_jobs
Revises: 0002_assets
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_jobs"
down_revision: str | Sequence[str] | None = "0002_assets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Frozen table definitions plus database protections for history and state transitions.
    op.create_table(
        "processing_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_type", sa.String(length=16), nullable=False),
        sa.Column("root_key", sa.String(length=64), nullable=False),
        sa.Column("target_identity", sa.String(length=512), nullable=False),
        sa.Column("raw_file_id", sa.Uuid(), nullable=True),
        sa.Column("segment_id", sa.Uuid(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.Integer(), server_default="0", nullable=False),
        sa.Column("parent_job_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="PENDING", nullable=False),
        sa.Column("priority", sa.Integer(), server_default="0", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("worker_id", sa.String(length=64), nullable=True),
        sa.Column("execution_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "next_run_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result_digest", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(job_type='SCAN' AND raw_file_id IS NULL AND segment_id IS NULL) OR "
            "(job_type='SEGMENT' AND raw_file_id IS NOT NULL AND segment_id IS "
            "NULL) OR (job_type='QUALITY' AND raw_file_id IS NULL AND segment_id "
            "IS NOT NULL)",
            name=op.f("ck_processing_jobs_valid_target"),
        ),
        sa.CheckConstraint(
            "(status = 'RUNNING' AND worker_id IS NOT NULL AND lease_until IS NOT"
            " NULL) OR (status <> 'RUNNING' AND lease_until IS NULL)",
            name=op.f("ck_processing_jobs_valid_lease"),
        ),
        sa.CheckConstraint(
            "(status='SUCCEEDED') = (result IS NOT NULL AND result_digest IS NOT NULL)",
            name=op.f("ck_processing_jobs_valid_result"),
        ),
        sa.CheckConstraint(
            "job_type IN ('SCAN','SEGMENT','QUALITY')", name=op.f("ck_processing_jobs_valid_type")
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','RETRY_WAIT','SUCCEEDED','FAILED')",
            name=op.f("ck_processing_jobs_valid_status"),
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND attempts <= max_attempts AND max_attempts BETWEEN "
            "1 AND 10 AND execution_version >= 0 AND generation >= 0",
            name=op.f("ck_processing_jobs_valid_attempts"),
        ),
        sa.CheckConstraint(
            "priority BETWEEN -100 AND 100", name=op.f("ck_processing_jobs_valid_priority")
        ),
        sa.ForeignKeyConstraint(
            ["parent_job_id"],
            ["processing_jobs.id"],
            name=op.f("fk_processing_jobs_parent_job_id_processing_jobs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["raw_file_id"],
            ["raw_files.id"],
            name=op.f("fk_processing_jobs_raw_file_id_raw_files"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["segment_id"],
            ["signal_segments.id"],
            name=op.f("fk_processing_jobs_segment_id_signal_segments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_processing_jobs")),
        sa.UniqueConstraint("dedupe_key", "generation", name="uq_job_generation"),
    )
    op.create_index(
        "ix_jobs_claim",
        "processing_jobs",
        ["root_key", sa.literal_column("priority DESC"), "next_run_at", "created_at", "id"],
        unique=False,
        postgresql_where=sa.text("status IN ('PENDING','RETRY_WAIT')"),
    )
    op.create_index(
        "ix_jobs_created", "processing_jobs", ["root_key", "created_at", "id"], unique=False
    )
    op.create_index(
        "ix_jobs_expired",
        "processing_jobs",
        ["root_key", "lease_until", "id"],
        unique=False,
        postgresql_where=sa.text("status = 'RUNNING'"),
    )
    op.create_index("ix_jobs_raw_file", "processing_jobs", ["raw_file_id"], unique=False)
    op.create_index("ix_jobs_segment", "processing_jobs", ["segment_id"], unique=False)
    op.create_table(
        "job_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("worker_id", sa.String(length=64), nullable=True),
        sa.Column("execution_version", sa.Integer(), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["processing_jobs.id"],
            name=op.f("fk_job_events_job_id_processing_jobs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_events")),
    )
    op.create_index(
        "ix_job_events_history", "job_events", ["job_id", "created_at", "id"], unique=False
    )
    op.create_table(
        "job_requests",
        sa.Column("request_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["processing_jobs.id"],
            name=op.f("fk_job_requests_job_id_processing_jobs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("request_key", name=op.f("pk_job_requests")),
    )
    op.create_index(op.f("ix_job_requests_job_id"), "job_requests", ["job_id"], unique=False)
    op.create_table(
        "segment_quality_results",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("segment_id", sa.Uuid(), nullable=False),
        sa.Column("algorithm_version", sa.String(length=64), nullable=False),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("created_by_job_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by_job_id"],
            ["processing_jobs.id"],
            name=op.f("fk_segment_quality_results_created_by_job_id_processing_jobs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["segment_id"],
            ["signal_segments.id"],
            name=op.f("fk_segment_quality_results_segment_id_signal_segments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_segment_quality_results")),
        sa.UniqueConstraint("segment_id", "algorithm_version", name="uq_segment_quality"),
    )
    op.add_column("signal_segments", sa.Column("created_by_job_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_signal_segments_created_by_job_id_processing_jobs"),
        "signal_segments",
        "processing_jobs",
        ["created_by_job_id"],
        ["id"],
        ondelete="RESTRICT",
        use_alter=True,
    )
    for table in ("job_events", "job_requests", "segment_quality_results"):
        op.execute(
            f"CREATE TRIGGER protect_{table} BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION protect_asset_history()"
        )
    op.execute("""
        CREATE FUNCTION protect_job_contract() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'job history is immutable' USING ERRCODE = '23514';
            END IF;
            IF (NEW.id,NEW.job_type,NEW.root_key,NEW.target_identity,NEW.raw_file_id,
                NEW.segment_id,NEW.payload,NEW.dedupe_key,NEW.generation,NEW.parent_job_id,
                NEW.priority,NEW.max_attempts,NEW.created_at) IS DISTINCT FROM
               (OLD.id,OLD.job_type,OLD.root_key,OLD.target_identity,OLD.raw_file_id,
                OLD.segment_id,OLD.payload,OLD.dedupe_key,OLD.generation,OLD.parent_job_id,
                OLD.priority,OLD.max_attempts,OLD.created_at) THEN
                RAISE EXCEPTION 'job inputs are immutable' USING ERRCODE = '23514';
            END IF;
            IF OLD.status IN ('SUCCEEDED','FAILED') THEN
                RAISE EXCEPTION 'terminal jobs are immutable' USING ERRCODE = '23514';
            END IF;
            IF OLD.status IN ('PENDING','RETRY_WAIT') THEN
                IF NEW.status <> 'RUNNING' OR NEW.attempts <> OLD.attempts+1
                   OR NEW.execution_version <> OLD.execution_version+1 THEN
                    RAISE EXCEPTION 'invalid job claim' USING ERRCODE = '23514';
                END IF;
            ELSIF NEW.attempts <> OLD.attempts OR NEW.execution_version <> OLD.execution_version
                  OR NEW.status NOT IN ('RUNNING','RETRY_WAIT','SUCCEEDED','FAILED')
                  OR (NEW.status='RUNNING' AND NEW.worker_id IS DISTINCT FROM OLD.worker_id)
            THEN
                RAISE EXCEPTION 'invalid execution transition' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER protect_jobs BEFORE UPDATE OR DELETE ON processing_jobs "
        "FOR EACH ROW EXECUTE FUNCTION protect_job_contract()"
    )
    op.execute("""
        CREATE FUNCTION protect_segment_origin() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.created_by_job_id IS DISTINCT FROM OLD.created_by_job_id THEN
                RAISE EXCEPTION 'segment origin is immutable' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER protect_segment_origin BEFORE UPDATE ON signal_segments "
        "FOR EACH ROW EXECUTE FUNCTION protect_segment_origin()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER protect_segment_origin ON signal_segments")
    op.execute("DROP FUNCTION protect_segment_origin()")
    op.execute("DROP TRIGGER protect_jobs ON processing_jobs")
    op.execute("DROP FUNCTION protect_job_contract()")
    for table in ("job_events", "job_requests", "segment_quality_results"):
        op.execute(f"DROP TRIGGER protect_{table} ON {table}")
    op.drop_constraint(
        op.f("fk_signal_segments_created_by_job_id_processing_jobs"),
        "signal_segments",
        type_="foreignkey",
    )
    op.drop_column("signal_segments", "created_by_job_id")
    op.drop_table("segment_quality_results")
    op.drop_index(op.f("ix_job_requests_job_id"), table_name="job_requests")
    op.drop_table("job_requests")
    op.drop_index("ix_job_events_history", table_name="job_events")
    op.drop_table("job_events")
    op.drop_index("ix_jobs_segment", table_name="processing_jobs")
    op.drop_index("ix_jobs_raw_file", table_name="processing_jobs")
    op.drop_index(
        "ix_jobs_expired",
        table_name="processing_jobs",
        postgresql_where=sa.text("status = 'RUNNING'"),
    )
    op.drop_index("ix_jobs_created", table_name="processing_jobs")
    op.drop_index(
        "ix_jobs_claim",
        table_name="processing_jobs",
        postgresql_where=sa.text("status IN ('PENDING','RETRY_WAIT')"),
    )
    op.drop_table("processing_jobs")
