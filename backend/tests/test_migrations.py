import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from alembic import command
from auditor.db import Base


@pytest.fixture(params=["sqlite", "postgres"])
def url(request, tmp_path):
    if request.param == "sqlite":
        return f"sqlite:///{tmp_path}/m.db"
    u = request.getfixturevalue("pg_url")
    e = create_engine(u)
    with e.begin() as c:
        c.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    e.dispose()
    return u


def test_migration_matches_models(url, monkeypatch):
    from auditor import config

    monkeypatch.setattr(config.settings, "database_url", url)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    assert "evidence_bundle" in inspect(engine).get_table_names()
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], diff
    command.downgrade(cfg, "base")
    assert inspect(engine).get_table_names() == ["alembic_version"]
    engine.dispose()
