"""live progress of the design generation (stage, characters written, past timings)

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-16 23:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0010'
down_revision: str | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('workflows', sa.Column('design_progress', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('workflows', 'design_progress')
