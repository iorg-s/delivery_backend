"""delivery collector name

Revision ID: 8a1c4e2f9b7d
Revises: 6f42c1e9d310
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8a1c4e2f9b7d"
down_revision: Union[str, Sequence[str], None] = "6f42c1e9d310"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Who collected the order ("Кто собирал" in the n8n import). Nullable and
    # skipped when present, so a restart or a hand-added column is harmless.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("deliveries")}
    if "collector_name" not in columns:
        op.add_column("deliveries", sa.Column("collector_name", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("deliveries", "collector_name")
