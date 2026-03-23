"""add signal_events and confluence_hits tables

Revision ID: a1b2c3d4e5f6
Revises: 3a6fdf87a1da
Create Date: 2026-03-23 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = '3a6fdf87a1da'
branch_labels = None
depends_on = None


def upgrade():
    # ── signal_events ─────────────────────────────────────────────────────────
    op.create_table(
        'signal_events',
        sa.Column('id',          sa.Integer(),     nullable=False),
        sa.Column('item_id',     sa.Integer(),     nullable=True),
        sa.Column('owner_id',    sa.Integer(),     nullable=False),
        sa.Column('brand_key',   sa.String(255),   nullable=False),
        sa.Column('brand_name',  sa.String(255),   nullable=False),
        sa.Column('signal_type', sa.String(50),    nullable=False),
        sa.Column('source_url',  sa.Text(),        nullable=True),
        sa.Column('detected_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('NOW()')),
        sa.ForeignKeyConstraint(['item_id'],  ['items.id'],  ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'],  ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_signal_events_owner_id',  'signal_events', ['owner_id'])
    op.create_index('ix_signal_events_brand_key', 'signal_events', ['brand_key'])
    op.create_index('ix_signal_events_item_id',   'signal_events', ['item_id'])

    # ── confluence_hits ───────────────────────────────────────────────────────
    op.create_table(
        'confluence_hits',
        sa.Column('id',            sa.Integer(),    nullable=False),
        sa.Column('owner_id',      sa.Integer(),    nullable=False),
        sa.Column('brand_key',     sa.String(255),  nullable=False),
        sa.Column('brand_name',    sa.String(255),  nullable=False),
        sa.Column('signal_count',  sa.Integer(),    nullable=False),
        sa.Column('signal_types',  sa.Text(),       nullable=False),
        sa.Column('bullish_score', sa.Integer(),    nullable=True),
        sa.Column('watch_level',   sa.String(20),   nullable=True),
        sa.Column('alert_sent',    sa.Boolean(),    nullable=False, server_default='false'),
        sa.Column('alert_sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at',    sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('NOW()')),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_confluence_hits_owner_id',  'confluence_hits', ['owner_id'])
    op.create_index('ix_confluence_hits_brand_key', 'confluence_hits', ['brand_key'])


def downgrade():
    op.drop_table('confluence_hits')
    op.drop_table('signal_events')
