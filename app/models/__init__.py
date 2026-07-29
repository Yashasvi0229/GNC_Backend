"""
Models package.

Every ORM model must be imported here so Alembic autogenerate can see it
via `Base.metadata`. In Step 2 we'll add:
    from app.models.user import User
    from app.models.client import Client
    ...
"""
from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPKMixin

__all__ = ["Base", "UUIDPKMixin", "TimestampMixin", "SoftDeleteMixin"]
