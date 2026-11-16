"""workflow diagram (image or PDF describing the process)

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-16 21:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0009'
down_revision: str | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('workflows', sa.Column('diagram_media_type', sa.String(length=64), nullable=True))
    op.add_column('workflows', sa.Column('diagram_data', sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    op.drop_column('workflows', 'diagram_data')
    op.drop_column('workflows', 'diagram_media_type')
