"""Alembic environment. Pulls the database URL from haulmind.config so there is one source."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from haulmind.config import get_settings
from haulmind.db.engine import build_engine
from haulmind.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    override = config.get_main_option("sqlalchemy.url")
    if override:
        return override
    settings = get_settings()
    settings.ensure_dirs()
    return settings.sqlalchemy_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = build_engine(_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
