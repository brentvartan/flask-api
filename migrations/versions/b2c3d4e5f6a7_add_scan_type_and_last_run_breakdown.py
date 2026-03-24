"""add scan_type and last_run breakdown to scheduled_scans

Revision ID: b2c3d4e5f6a7
Revises: 3a6fdf87a1da
Create Date: 2026-03-24 06:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b2c3d4e5f6a7'
down_revision = '3a6fdf87a1da'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('scheduled_scans', schema=None) as batch_op:
        batch_op.add_column(sa.Column('scan_type',     sa.String(length=50), nullable=True, server_default='full'))
        batch_op.add_column(sa.Column('last_run_hot',  sa.Integer(),         nullable=True, server_default='0'))
        batch_op.add_column(sa.Column('last_run_warm', sa.Integer(),         nullable=True, server_default='0'))
        batch_op.add_column(sa.Column('last_run_cold', sa.Integer(),         nullable=True, server_default='0'))


def downgrade():
    with op.batch_alter_table('scheduled_scans', schema=None) as batch_op:
        batch_op.drop_column('last_run_cold')
        batch_op.drop_column('last_run_warm')
        batch_op.drop_column('last_run_hot')
        batch_op.drop_column('scan_type')
