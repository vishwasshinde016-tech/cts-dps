"""Share operator AI route pauses across the API and workers."""

import sqlalchemy as sa
from alembic import op

revision = "ae43796b724a"
down_revision = "e92fac1d13b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("dps_ai_route_pause", sa.Column("route", sa.String(64), primary_key=True),
                    sa.Column("paused_at", sa.DateTime(timezone=True), nullable=False))


def downgrade() -> None:
    op.drop_table("dps_ai_route_pause")
