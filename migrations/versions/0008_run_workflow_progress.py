"""run workflow progress (ongoing / done / blocked / continued)

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-16 20:30:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('workflow_runs', sa.Column('progress', sa.String(length=16), nullable=True))
    op.add_column('workflow_runs', sa.Column('continued_by_run_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('workflow_runs', 'continued_by_run_id')
    op.drop_column('workflow_runs', 'progress')
