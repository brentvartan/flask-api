"""add item_type indexed column to items table

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-04-07 08:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'd4e5f6a7b8c9'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade():
    # Add item_type column
    op.add_column('items', sa.Column('item_type', sa.String(50), nullable=True))
    op.create_index('ix_items_item_type', 'items', ['item_type'])

    # Backfill from JSON stored in description
    op.execute("""
        UPDATE items
        SET item_type = 'signal'
        WHERE description LIKE '%"_type":"signal"%'
    """)
    op.execute("""
        UPDATE items
        SET item_type = 'watchlist'
        WHERE description LIKE '%"_type":"watchlist"%'
           OR description LIKE '%"_type": "watchlist"%'
    """)
    op.execute("""
        UPDATE items
        SET item_type = 'settings'
        WHERE title = '__bullish_settings__'
    """)


def downgrade():
    op.drop_index('ix_items_item_type', table_name='items')
    op.drop_column('items', 'item_type')
