"""Two-way Jira sync: a per-org webhook secret (encrypted) on the Jira connection

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-04 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import archflow.db.types

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('org_jira_connections')}
    with op.batch_alter_table('org_jira_connections') as batch:
        if 'webhook_ciphertext' not in cols:
            batch.add_column(sa.Column('webhook_ciphertext', sa.Text(), server_default='', nullable=False))
        if 'webhook_created_at' not in cols:
            batch.add_column(sa.Column('webhook_created_at', archflow.db.types.UTCDateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('org_jira_connections') as batch:
        batch.drop_column('webhook_created_at')
        batch.drop_column('webhook_ciphertext')
