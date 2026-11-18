"""settings given at launch, kept on the run so the project can be continued with them

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-18 00:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0012'
down_revision: str | None = '0011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('workflow_runs', sa.Column('config_values', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('workflow_runs', 'config_values')
