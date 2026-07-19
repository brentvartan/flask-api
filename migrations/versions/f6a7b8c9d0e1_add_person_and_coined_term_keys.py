"""add person_keys and coined_term_keys to signal_events

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Branch Labels: None
Depends On: None
"""
from alembic import op
import sqlalchemy as sa

revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('signal_events', sa.Column('person_keys',      sa.Text(), nullable=True))
    op.add_column('signal_events', sa.Column('coined_term_keys', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('signal_events', 'coined_term_keys')
    op.drop_column('signal_events', 'person_keys')
