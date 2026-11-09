"""Comments on requirements and connections (edges), besides the project, components and decisions

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-10 09:00:00.000000
"""

from __future__ import annotations

from alembic import op

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None

OLD = "target_type IN ('project', 'node', 'decision')"
NEW = "target_type IN ('project', 'node', 'decision', 'requirement', 'edge')"


def upgrade() -> None:
    # batch mode rebuilds the table on SQLite, which cannot alter a CHECK constraint in place
    with op.batch_alter_table('comments') as batch:
        batch.drop_constraint('ck_comments_target', type_='check')
        batch.create_check_constraint('ck_comments_target', NEW)


def downgrade() -> None:
    op.execute("DELETE FROM comments WHERE target_type IN ('requirement', 'edge')")
    with op.batch_alter_table('comments') as batch:
        batch.drop_constraint('ck_comments_target', type_='check')
        batch.create_check_constraint('ck_comments_target', OLD)
