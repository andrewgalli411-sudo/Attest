from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from auditor import models  # noqa: F401
from auditor.db import Base
from auditor.main import create_app
from auditor.seed import seed_criteria


def test_health_carries_draft_notice():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    r = TestClient(create_app(sessionmaker(engine))).get("/api/health")
    assert r.status_code == 200
    assert "requires expert review" in r.json()["notice"]


def test_seed_is_idempotent_and_complete():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(engine)() as db:
        assert seed_criteria(db) == 50
        assert seed_criteria(db) == 0
