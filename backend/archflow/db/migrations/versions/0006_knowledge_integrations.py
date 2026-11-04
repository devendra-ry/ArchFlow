"""Knowledge base (documents and retrievable chunks with embeddings) and org integrations (GitHub, Confluence,
Slack, Microsoft Teams)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-01 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

import archflow.db.types

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def _json() -> sa.types.TypeEngine:
    return sa.JSON(none_as_null=True).with_variant(postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    # Like the baseline, skip what exists: a database without an Alembic stamp may already have these tables.
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if 'kb_documents' not in existing:
        op.create_table(
            'kb_documents',
            sa.Column('id', sa.Text(), nullable=False),
            sa.Column('project_id', sa.Text(), nullable=False),
            sa.Column('name', sa.Text(), nullable=False),
            sa.Column('type', sa.Text(), nullable=False),
            sa.Column('origin', sa.Text(), server_default='upload', nullable=False),
            sa.Column('chars', sa.Integer(), server_default='0', nullable=False),
            sa.Column('chunks', sa.Integer(), server_default='0', nullable=False),
            sa.Column('embedding_model', sa.Text(), server_default='', nullable=False),
            sa.Column('created_by', sa.Text(), server_default='', nullable=False),
            sa.Column('created_at', archflow.db.types.UTCDateTime(), nullable=False),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('idx_kb_documents_project', 'kb_documents', ['project_id', 'created_at'])
    if 'kb_chunks' not in existing:
        op.create_table(
            'kb_chunks',
            sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
            sa.Column('document_id', sa.Text(), nullable=False),
            sa.Column('project_id', sa.Text(), nullable=False),
            sa.Column('ord', sa.Integer(), nullable=False),
            sa.Column('section', sa.Text(), server_default='', nullable=False),
            sa.Column('page', sa.Integer(), nullable=True),
            sa.Column('locator', sa.Text(), server_default='', nullable=False),
            sa.Column('text', sa.Text(), nullable=False),
            sa.Column('embedding', _json(), nullable=True),
            sa.ForeignKeyConstraint(['document_id'], ['kb_documents.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('idx_kb_chunks_project', 'kb_chunks', ['project_id'])
        op.create_index('idx_kb_chunks_document', 'kb_chunks', ['document_id', 'ord'])
    if 'org_integrations' not in existing:
        op.create_table(
            'org_integrations',
            sa.Column('org_id', sa.Text(), nullable=False),
            sa.Column('kind', sa.Text(), nullable=False),
            sa.Column('config', _json(), server_default='{}', nullable=False),
            sa.Column('ciphertext', sa.Text(), server_default='', nullable=False),
            sa.Column('last4', sa.Text(), server_default='', nullable=False),
            sa.Column('updated_by', sa.Text(), nullable=True),
            sa.Column('updated_at', archflow.db.types.UTCDateTime(), nullable=False),
            sa.ForeignKeyConstraint(['org_id'], ['orgs.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('org_id', 'kind'),
        )


def downgrade() -> None:
    op.drop_table('org_integrations')
    op.drop_index('idx_kb_chunks_document', table_name='kb_chunks')
    op.drop_index('idx_kb_chunks_project', table_name='kb_chunks')
    op.drop_table('kb_chunks')
    op.drop_index('idx_kb_documents_project', table_name='kb_documents')
    op.drop_table('kb_documents')
