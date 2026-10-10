from sqlalchemy.orm import Session

from .models import Criterion
from .pipeline.judge.criteria import CRITERIA


def seed_criteria(db: Session) -> int:
    existing = {c.sc_id for c in db.query(Criterion.sc_id)}
    new = [Criterion(**c) for c in CRITERIA if c["sc_id"] not in existing]
    db.add_all(new)
    db.commit()
    return len(new)
