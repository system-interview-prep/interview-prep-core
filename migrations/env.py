from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from src.core.config import get_settings


def _sync_database_url(url: str) -> str:
    """Use the sync driver declared by this project for Alembic.

    Application traffic uses asyncpg through src.infrastructure.database.
    Alembic is synchronous and the project installs psycopg2-binary. SQLAlchemy
    2.1 changed the default PostgreSQL driver for a plain postgresql:// URL to
    psycopg (v3), so leaving the scheme implicit makes migrations depend on a
    package the project does not install.
    """

    if url.startswith("postgresql+psycopg2://"):
        return url
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg2://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg2://", 1)
    return url


config = context.config
config.set_main_option("sqlalchemy.url", _sync_database_url(get_settings().database_url))
if config.config_file_name:
    fileConfig(config.config_file_name)


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
