"""Add reliability, integrations, delivery, and audit records.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from testforge import models  # noqa: F401
from testforge.database import Base

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


NEW_TABLES = [
    "image_builds",
    "public_services",
    "service_releases",
    "service_instances",
    "artifact_blobs",
    "delivery_outbox",
    "audit_logs",
]


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "source_type" not in _columns("projects"):
        op.add_column(
            "projects",
            sa.Column("source_type", sa.String(length=20), nullable=False, server_default="git"),
        )
    if "callback_url" not in _columns("executions"):
        op.add_column(
            "executions", sa.Column("callback_url", sa.String(length=1000), nullable=True)
        )
    bind = op.get_bind()
    for name in NEW_TABLES:
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(NEW_TABLES):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
    if "callback_url" in _columns("executions"):
        op.drop_column("executions", "callback_url")
    if "source_type" in _columns("projects"):
        op.drop_column("projects", "source_type")
