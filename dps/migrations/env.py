from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, inspect, pool, text
from sqlalchemy.engine import make_url

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
import dps.models  # noqa: E402,F401  (register tables)
from dps.db import Base  # noqa: E402
from dps.settings import get_settings  # noqa: E402

# Alembic keeps options in a configparser, which reads "%" as interpolation: escape it, or a URL-encoded
# password (%21 for "!") breaks every migration.
config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
target_metadata = Base.metadata
MYSQL_VERSION_TABLE = "dps_alembic_version"


def include_dps_names(name, type_, _):
    return type_ != "table" or name in target_metadata.tables

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    if make_url(url).get_backend_name() == "mysql":
        raise RuntimeError("Use the DPS MySQL bootstrap SQL for the initial schema or online Alembic for upgrades.")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        options = {}
        if connection.dialect.name == "mysql":
            if not inspect(connection).has_table(MYSQL_VERSION_TABLE):
                raise RuntimeError("Install the reviewed DPS MySQL bootstrap before running Alembic.")
            versions = connection.execute(text("SELECT version_num FROM dps_alembic_version")).scalars().all()
            if len(versions) != 1:
                raise RuntimeError("Incomplete DPS MySQL migration history. Check the installation before upgrading.")
            connection.rollback()  # clear the transaction opened by the metadata lookup
            options = {"version_table": MYSQL_VERSION_TABLE, "include_name": include_dps_names}
        context.configure(connection=connection, target_metadata=target_metadata, **options)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
