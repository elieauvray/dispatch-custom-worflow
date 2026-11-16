"""workflow sharing: private (default) or shared with every user

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-17 00:30:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0011'
down_revision: str | None = '0010'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('workflows', sa.Column('shared', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column('workflows', 'shared')
