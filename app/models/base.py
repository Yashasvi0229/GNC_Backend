"""
Base SQLAlchemy model.

All ORM models inherit from `Base` and typically also from `TimestampMixin`
and/or `SoftDeleteMixin` to get audit columns for free.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""

    # Naming convention keeps Alembic autogenerate deterministic and
    # produces predictable constraint names in migrations.
    # (See https://alembic.sqlalchemy.org/en/latest/naming.html)
    pass


class UUIDPKMixin:
    """Adds a UUID primary key column, generated on the database side."""

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )


class TimestampMixin:
    """Adds `created_at` and `updated_at` timestamp columns (timezone-aware)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SoftDeleteMixin:
    """Adds a `deleted_at` column for soft-deletable rows."""

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
