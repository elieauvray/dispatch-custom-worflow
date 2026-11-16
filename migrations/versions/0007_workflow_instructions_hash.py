"""workflow instructions hash (system instructions the design was generated with)

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-16 19:30:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('workflows', sa.Column('instructions_hash', sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column('workflows', 'instructions_hash')
