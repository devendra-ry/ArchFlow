"""Compliance frameworks: the frameworks that apply to a project and people's evidence / notes per control

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-04 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

import archflow.db.types

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def _json() -> sa.types.TypeEngine:
    return sa.JSON(none_as_null=True).with_variant(postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    # Like the baseline, skip what exists: a database without an Alembic stamp may already have these tables.
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if 'compliance_settings' not in existing:
        op.create_table(
            'compliance_settings',
            sa.Column('project_id', sa.Text(), nullable=False),
            sa.Column('frameworks', _json(), server_default='[]', nullable=False),
            sa.Column('updated_by', sa.Text(), nullable=True),
            sa.Column('updated_at', archflow.db.types.UTCDateTime(), nullable=False),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('project_id'),
        )
    if 'compliance_evidence' not in existing:
        op.create_table(
            'compliance_evidence',
            sa.Column('project_id', sa.Text(), nullable=False),
            sa.Column('framework', sa.Text(), nullable=False),
            sa.Column('control_id', sa.Text(), nullable=False),
            sa.Column('status', sa.Text(), server_default='', nullable=False),
            sa.Column('note', sa.Text(), server_default='', nullable=False),
            sa.Column('updated_by', sa.Text(), nullable=True),
            sa.Column('updated_at', archflow.db.types.UTCDateTime(), nullable=False),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('project_id', 'framework', 'control_id'),
        )


def downgrade() -> None:
    op.drop_table('compliance_evidence')
    op.drop_table('compliance_settings')
