"""Alembic environment.

The database URL always comes from application settings, never from
``alembic.ini``. That keeps one source of truth for connection details and means
``alembic upgrade head`` behaves identically on a laptop, in Compose and in CI.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from supplylink.config import get_settings
from supplylink.warehouse.schema import METADATA

config = context.config
config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = METADATA

# Alembic must not try to manage extension-owned or system objects.
_MANAGED_SCHEMAS = {"meta", "staging", "core", "dq"}


def include_object(obj, name, type_, reflected, compare_to) -> bool:  # noqa: ARG001
    schema = getattr(obj, "schema", None)
    if type_ == "table":
        return schema in _MANAGED_SCHEMAS
    return True


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it (``alembic upgrade head --sql``)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        include_schemas=True,
        include_object=include_object,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            include_object=include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
