"""add session_version and approval integrity constraints

Three changes, each closing a gap found in the Phase 1 review:

1. `users.session_version` — makes sign-out real. Session cookies are signed
   bearer tokens; deleting the browser's copy cannot revoke a copy taken
   elsewhere. Binding a token to this counter means bumping it invalidates every
   token issued before the bump.
2. `ck_approval_resolved_requires_actor` — an approval whose status is APPROVED
   or REJECTED must name a real user. Without it, any code path (including a
   future agent-driven one) could insert an approved row with no human behind
   it, which is precisely the guarantee `03_SECURITY_ACCESS.md` §4 makes.
3. `ck_approval_resolved_requires_timestamp` — a resolved approval must record
   when it was resolved, so the audit trail cannot have holes.

Alembic does not autogenerate CHECK constraints, so 2 and 3 are written by hand.

Revision ID: 7b63536eb4ba
Revises: 3e8cdd6bfd68
Create Date: 2026-09-10 08:33:19.755104
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7b63536eb4ba"
down_revision: str | Sequence[str] | None = "3e8cdd6bfd68"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # ActivityEvent.kind is now the closed ActivityEventKind vocabulary.
    with op.batch_alter_table("activity_events", schema=None) as batch_op:
        batch_op.alter_column(
            "kind",
            existing_type=sa.VARCHAR(length=128),
            type_=sa.String(length=64),
            existing_nullable=False,
        )

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("session_version", sa.Integer(), server_default="0", nullable=False)
        )

    # SQLite cannot add a CHECK in place; batch mode rebuilds the table, which
    # also validates the constraint against any existing rows.
    with op.batch_alter_table("approvals", schema=None) as batch_op:
        batch_op.create_check_constraint(
            "ck_approval_resolved_requires_actor",
            "status = 'pending' OR actor_user_id IS NOT NULL",
        )
        batch_op.create_check_constraint(
            "ck_approval_resolved_requires_timestamp",
            "status = 'pending' OR resolved_at IS NOT NULL",
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("approvals", schema=None) as batch_op:
        batch_op.drop_constraint("ck_approval_resolved_requires_timestamp", type_="check")
        batch_op.drop_constraint("ck_approval_resolved_requires_actor", type_="check")

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("session_version")

    with op.batch_alter_table("activity_events", schema=None) as batch_op:
        batch_op.alter_column(
            "kind",
            existing_type=sa.String(length=64),
            type_=sa.VARCHAR(length=128),
            existing_nullable=False,
        )
