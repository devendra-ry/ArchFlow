"""Personal access tokens limited to some projects

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-10 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def _json() -> sa.types.TypeEngine:
    return sa.JSON(none_as_null=True).with_variant(postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    if 'project_ids' not in {c['name'] for c in sa.inspect(op.get_bind()).get_columns('api_tokens')}:
        op.add_column('api_tokens', sa.Column('project_ids', _json(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('api_tokens') as batch:
        batch.drop_column('project_ids')
