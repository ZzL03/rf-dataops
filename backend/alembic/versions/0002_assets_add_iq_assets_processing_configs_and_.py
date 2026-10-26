"""Add IQ assets, processing configs and segment references

Revision ID: 0002_assets
Revises: 0001_users
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_assets"
down_revision: str | Sequence[str] | None = "0001_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Tables and named constraints are frozen in this migration, independent of later ORM changes.
    op.create_table(
        "processing_configs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reader_version", sa.String(length=64), nullable=False),
        sa.Column("pipeline_version", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "config_hash ~ '^[a-f0-9]{64}$'", name=op.f("ck_processing_configs_valid_hash")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_processing_configs")),
        sa.UniqueConstraint("config_hash", name=op.f("uq_processing_configs_config_hash")),
    )
    op.create_table(
        "raw_files",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("root_key", sa.String(length=64), nullable=False),
        sa.Column("relative_path", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("metadata_hash", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("stat_identity", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("format", sa.String(length=8), nullable=False),
        sa.Column("iq_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sample_count", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=8), server_default="ACTIVE", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("format IN ('BIN', 'HDF5')", name=op.f("ck_raw_files_valid_format")),
        sa.CheckConstraint(
            "sha256 ~ '^[a-f0-9]{64}$' AND metadata_hash ~ '^[a-f0-9]{64}$'",
            name=op.f("ck_raw_files_valid_hashes"),
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'STALE', 'INVALID')", name=op.f("ck_raw_files_valid_status")
        ),
        sa.CheckConstraint(
            "byte_size > 0 AND sample_count > 0", name=op.f("ck_raw_files_positive_size")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_raw_files")),
        sa.UniqueConstraint(
            "root_key", "relative_path", "sha256", "metadata_hash", name="uq_raw_identity"
        ),
    )
    op.create_index("ix_raw_files_path", "raw_files", ["root_key", "relative_path"], unique=False)
    op.create_index("ix_raw_files_sha256", "raw_files", ["sha256"], unique=False)
    op.create_index(
        "uq_raw_active_path",
        "raw_files",
        ["root_key", "relative_path"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_table(
        "file_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("raw_file_id", sa.Uuid(), nullable=False),
        sa.Column("event", sa.String(length=40), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["raw_file_id"],
            ["raw_files.id"],
            name=op.f("fk_file_observations_raw_file_id_raw_files"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_file_observations")),
    )
    op.create_index(
        "ix_file_observations_file",
        "file_observations",
        ["raw_file_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "signal_segments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("raw_file_id", sa.Uuid(), nullable=False),
        sa.Column("config_id", sa.Uuid(), nullable=False),
        sa.Column("start_sample", sa.BigInteger(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("quality", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "start_sample >= 0 AND sample_count > 0 AND sample_count <= 1000000",
            name=op.f("ck_signal_segments_valid_interval"),
        ),
        sa.ForeignKeyConstraint(
            ["config_id"],
            ["processing_configs.id"],
            name=op.f("fk_signal_segments_config_id_processing_configs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["raw_file_id"],
            ["raw_files.id"],
            name=op.f("fk_signal_segments_raw_file_id_raw_files"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signal_segments")),
        sa.UniqueConstraint(
            "raw_file_id", "config_id", "start_sample", "sample_count", name="uq_segment_identity"
        ),
    )
    op.create_index("ix_signal_segments_config_id", "signal_segments", ["config_id"], unique=False)
    op.create_index(
        "ix_signal_segments_file",
        "signal_segments",
        ["raw_file_id", "start_sample", "id"],
        unique=False,
    )
    op.execute("""
        CREATE FUNCTION protect_asset_history() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_TABLE_NAME = 'raw_files' AND TG_OP = 'UPDATE' THEN
                IF (NEW.id, NEW.root_key, NEW.relative_path, NEW.sha256, NEW.metadata_hash,
                    NEW.byte_size, NEW.format, NEW.iq_metadata, NEW.sample_count, NEW.created_at)
                   IS DISTINCT FROM
                   (OLD.id, OLD.root_key, OLD.relative_path, OLD.sha256, OLD.metadata_hash,
                    OLD.byte_size, OLD.format, OLD.iq_metadata,
                    OLD.sample_count, OLD.created_at) THEN
                    RAISE EXCEPTION 'raw asset identity is immutable' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            ELSIF TG_TABLE_NAME = 'signal_segments' AND TG_OP = 'UPDATE' THEN
                IF (NEW.id, NEW.raw_file_id, NEW.config_id, NEW.start_sample, NEW.sample_count,
                    NEW.created_at) IS DISTINCT FROM
                   (OLD.id, OLD.raw_file_id, OLD.config_id, OLD.start_sample, OLD.sample_count,
                    OLD.created_at) THEN
                    RAISE EXCEPTION 'segment identity is immutable' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'asset history is append-only' USING ERRCODE = '23514';
        END $$
    """)
    for table in ("raw_files", "signal_segments", "processing_configs", "file_observations"):
        op.execute(
            f"CREATE TRIGGER protect_{table} BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION protect_asset_history()"
        )
    op.execute("""
        CREATE FUNCTION check_segment_source() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE source_samples bigint; source_status text; expected_window bigint;
        BEGIN
            SELECT sample_count, status INTO source_samples, source_status
            FROM raw_files WHERE id = NEW.raw_file_id FOR KEY SHARE;
            IF source_samples IS NULL THEN
                RAISE EXCEPTION 'source not found' USING ERRCODE = '23503';
            END IF;
            SELECT (parameters->>'window_samples')::bigint INTO expected_window
            FROM processing_configs WHERE id = NEW.config_id;
            IF source_status <> 'ACTIVE' OR NEW.start_sample > source_samples - NEW.sample_count
               OR NEW.sample_count IS DISTINCT FROM expected_window THEN
                RAISE EXCEPTION 'segment is outside source or mismatches config'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER check_segment_bounds BEFORE INSERT ON signal_segments "
        "FOR EACH ROW EXECUTE FUNCTION check_segment_source()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER check_segment_bounds ON signal_segments")
    op.execute("DROP FUNCTION check_segment_source()")
    for table in ("raw_files", "signal_segments", "processing_configs", "file_observations"):
        op.execute(f"DROP TRIGGER protect_{table} ON {table}")
    op.execute("DROP FUNCTION protect_asset_history()")
    op.drop_index("ix_signal_segments_file", table_name="signal_segments")
    op.drop_index("ix_signal_segments_config_id", table_name="signal_segments")
    op.drop_table("signal_segments")
    op.drop_index("ix_file_observations_file", table_name="file_observations")
    op.drop_table("file_observations")
    op.drop_index("ix_raw_files_sha256", table_name="raw_files")
    op.drop_index("uq_raw_active_path", table_name="raw_files")
    op.drop_index("ix_raw_files_path", table_name="raw_files")
    op.drop_table("raw_files")
    op.drop_table("processing_configs")
