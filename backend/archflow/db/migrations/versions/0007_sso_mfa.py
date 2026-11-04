"""Single sign-on (OIDC) and require-MFA per org, TOTP factors per user, per-project role overrides, and the sign-in
method of each session

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
    # Like the baseline, skip what exists: a database without an Alembic stamp may already have these.
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())
    if 'auth_method' not in {c['name'] for c in inspector.get_columns('sessions')}:
        op.add_column('sessions', sa.Column('auth_method', sa.Text(), server_default='password', nullable=False))
    if 'org_auth_settings' not in existing:
        op.create_table(
            'org_auth_settings',
            sa.Column('org_id', sa.Text(), nullable=False),
            sa.Column('sso_enabled', sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column('issuer', sa.Text(), server_default='', nullable=False),
            sa.Column('client_id', sa.Text(), server_default='', nullable=False),
            sa.Column('ciphertext', sa.Text(), server_default='', nullable=False),
            sa.Column('last4', sa.Text(), server_default='', nullable=False),
            sa.Column('domains', _json(), server_default='[]', nullable=False),
            sa.Column('default_role', sa.Text(), server_default='developer', nullable=False),
            sa.Column('require_sso', sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column('require_mfa', sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column('updated_by', sa.Text(), nullable=True),
            sa.Column('updated_at', archflow.db.types.UTCDateTime(), nullable=False),
            sa.ForeignKeyConstraint(['org_id'], ['orgs.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('org_id'),
        )
    if 'user_mfa' not in existing:
        op.create_table(
            'user_mfa',
            sa.Column('user_id', sa.Text(), nullable=False),
            sa.Column('ciphertext', sa.Text(), nullable=False),
            sa.Column('enabled', sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column('recovery_hashes', _json(), server_default='[]', nullable=False),
            sa.Column('last_step', sa.BigInteger(), server_default='0', nullable=False),
            sa.Column('created_at', archflow.db.types.UTCDateTime(), nullable=False),
            sa.Column('enabled_at', archflow.db.types.UTCDateTime(), nullable=True),
            sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('user_id'),
        )
    if 'project_roles' not in existing:
        op.create_table(
            'project_roles',
            sa.Column('project_id', sa.Text(), nullable=False),
            sa.Column('user_id', sa.Text(), nullable=False),
            sa.Column('role', sa.Text(), nullable=False),
            sa.Column('granted_by', sa.Text(), nullable=True),
            sa.Column('created_at', archflow.db.types.UTCDateTime(), nullable=False),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('project_id', 'user_id'),
        )
        op.create_index('idx_project_roles_user', 'project_roles', ['user_id'])


def downgrade() -> None:
    op.drop_index('idx_project_roles_user', table_name='project_roles')
    op.drop_table('project_roles')
    op.drop_table('user_mfa')
    op.drop_table('org_auth_settings')
    with op.batch_alter_table('sessions') as batch:
        batch.drop_column('auth_method')
