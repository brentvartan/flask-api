"""add scan_runs table and cumulative stats to scheduled_scans

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-03-24 07:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c3d4e5f6a7b8'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'scan_runs',
        sa.Column('id',              sa.Integer(),                  nullable=False),
        sa.Column('scan_id',         sa.Integer(),                  nullable=False),
        sa.Column('owner_id',        sa.Integer(),                  nullable=False),
        sa.Column('ran_at',          sa.DateTime(timezone=True),    nullable=True),
        sa.Column('new_saved',       sa.Integer(),                  nullable=True, server_default='0'),
        sa.Column('hot_found',       sa.Integer(),                  nullable=True, server_default='0'),
        sa.Column('warm_found',      sa.Integer(),                  nullable=True, server_default='0'),
        sa.Column('cold_found',      sa.Integer(),                  nullable=True, server_default='0'),
        sa.Column('founders_queued', sa.Integer(),                  nullable=True, server_default='0'),
        sa.Column('alert_sent',      sa.Boolean(),                  nullable=True, server_default='false'),
        sa.Column('alert_emails',    sa.String(length=500),         nullable=True),
        sa.Column('sources_ran',     sa.String(length=100),         nullable=True),
        sa.Column('error_message',   sa.String(length=500),         nullable=True),
        sa.ForeignKeyConstraint(['scan_id'], ['scheduled_scans.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_scan_runs_scan_id'), 'scan_runs', ['scan_id'], unique=False)

    with op.batch_alter_table('scheduled_scans', schema=None) as batch_op:
        batch_op.add_column(sa.Column('total_signals',         sa.Integer(), nullable=True, server_default='0'))
        batch_op.add_column(sa.Column('total_hot',             sa.Integer(), nullable=True, server_default='0'))
        batch_op.add_column(sa.Column('total_warm',            sa.Integer(), nullable=True, server_default='0'))
        batch_op.add_column(sa.Column('last_alert_sent',       sa.Boolean(), nullable=True, server_default='false'))
        batch_op.add_column(sa.Column('last_alert_emails',     sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column('last_founders_queued',  sa.Integer(), nullable=True, server_default='0'))


def downgrade():
    op.drop_index(op.f('ix_scan_runs_scan_id'), table_name='scan_runs')
    op.drop_table('scan_runs')

    with op.batch_alter_table('scheduled_scans', schema=None) as batch_op:
        batch_op.drop_column('last_founders_queued')
        batch_op.drop_column('last_alert_emails')
        batch_op.drop_column('last_alert_sent')
        batch_op.drop_column('total_warm')
        batch_op.drop_column('total_hot')
        batch_op.drop_column('total_signals')
