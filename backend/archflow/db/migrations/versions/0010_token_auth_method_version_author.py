"""How each API token's user signed in (for the org's require-SSO rule over MCP) and who saved each version (by
user id, for the self-approval rule)

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-09 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if 'auth_method' not in {c['name'] for c in inspector.get_columns('api_tokens')}:
        op.add_column('api_tokens', sa.Column('auth_method', sa.Text(), nullable=True))
    if 'author_id' not in {c['name'] for c in inspector.get_columns('versions')}:
        op.add_column('versions', sa.Column('author_id', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('versions') as batch:
        batch.drop_column('author_id')
    with op.batch_alter_table('api_tokens') as batch:
        batch.drop_column('auth_method')
