"""Alembic environment for the evaluation database."""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, event, pool
from sqlalchemy.engine import make_url

from internal.evaluation.store import Base
from internal.application import models as _application_models  # noqa: F401
from internal.experimentation import models as _experimentation_models  # noqa: F401


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    attribute_override = str(config.attributes.get("database_url_override", "")).strip()
    override = attribute_override or os.getenv("AGI_EVAL_DATABASE_URL", "").strip()
    if override:
        # ConfigParser treats '%' as interpolation syntax.
        config.set_main_option("sqlalchemy.url", override.replace("%", "%%"))
    database_url = config.get_main_option("sqlalchemy.url")
    if not database_url:
        raise RuntimeError("evaluation database URL is not configured")
    _prepare_sqlite_parent(database_url)
    return database_url


def _prepare_sqlite_parent(database_url: str) -> None:
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite") or not url.database:
        return
    if url.database == ":memory:" or url.database.startswith("file:"):
        return
    Path(url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    if connectable.dialect.name == "sqlite":

        @event.listens_for(connectable, "connect")
        def _prepare_sqlite_batch_migration(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                # Alembic batch operations DROP and recreate parent tables.
                # Enforcing ON DELETE CASCADE here deletes unrelated child
                # data when e.g. users gains columns. This dedicated migration
                # connection has no application writes; runtime connections
                # still enforce foreign_keys=ON in evaluation.store.
                cursor.execute("PRAGMA foreign_keys=OFF")
                cursor.execute("PRAGMA busy_timeout=5000")
            finally:
                cursor.close()

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            render_as_batch=connection.dialect.name == "sqlite",
            transaction_per_migration=True,
        )

        with context.begin_transaction():
            context.run_migrations()
            if connection.dialect.name == "sqlite":
                if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
                    raise RuntimeError("Migration left invalid foreign-key references")
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
