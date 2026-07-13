"""repair: ensure scheduled_scans columns exist (scan_type, last_run_*)

Revision ID: e5f6a7b8c9d0
Revises: 648f88c8e46e
Create Date: 2026-07-12 00:00:00.000000

Idempotent repair migration: adds scan_type and last_run_* columns to
scheduled_scans if they are missing. Uses IF NOT EXISTS so this is safe
to run regardless of whether the prior migration (b2c3d4e5f6a7) was
applied to the live database.
"""
from alembic import op

revision = 'e5f6a7b8c9d0'
down_revision = '648f88c8e46e'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE scheduled_scans
            ADD COLUMN IF NOT EXISTS scan_type     VARCHAR(50)  DEFAULT 'full',
            ADD COLUMN IF NOT EXISTS last_run_hot  INTEGER      DEFAULT 0,
            ADD COLUMN IF NOT EXISTS last_run_warm INTEGER      DEFAULT 0,
            ADD COLUMN IF NOT EXISTS last_run_cold INTEGER      DEFAULT 0
    """)


def downgrade():
    pass
