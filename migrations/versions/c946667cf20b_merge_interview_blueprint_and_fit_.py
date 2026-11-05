"""merge interview blueprint and fit history migrations

Revision ID: c946667cf20b
Revises: 20261031_0039, 20261103_0040
Create Date: 2026-11-05 21:03:38.095924
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c946667cf20b'
down_revision: Union[str, None] = ('20261031_0039', '20261103_0040')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
