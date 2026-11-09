"""Org approval policy: protect the main branch

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-10 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    if 'protect_main' not in {c['name'] for c in sa.inspect(op.get_bind()).get_columns('org_approval_policy')}:
        op.add_column('org_approval_policy',
                      sa.Column('protect_main', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('org_approval_policy') as batch:
        batch.drop_column('protect_main')
