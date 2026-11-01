import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool
from sqlalchemy import text

from alembic import context
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

# Alembic Config object, provides access to values from alembic.ini
config = context.config

# Set up logging
if config.config_file_name is not None:
    # Keep uvicorn's loggers: migrations run inside the app at startup.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Set SQLAlchemy URL from environment variable
config.set_main_option('sqlalchemy.url', os.getenv('DATABASE_URL'))

# Import your Base metadata
from app.db import Base
from app import models
target_metadata = Base.metadata

# Run migrations offline or online
def run_migrations_offline():
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        if connection.dialect.name == "postgresql":
            # Fail with a visible error instead of hanging the deploy when
            # another connection holds a lock on a table being altered.
            connection.execute(text("SET lock_timeout = '30s'"))
            # SQLAlchemy 2 auto-began a transaction for the SET; end it so
            # alembic opens (and commits) its own. Otherwise alembic treats the
            # transaction as external, never commits, and the migration is
            # rolled back when the connection closes. The setting persists.
            connection.commit()
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
