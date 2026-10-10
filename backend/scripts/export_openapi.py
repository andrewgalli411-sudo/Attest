"""Write the API's OpenAPI schema for the frontend's generated types.  uv run python scripts/export_openapi.py"""

import json
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from auditor.main import create_app

out = Path(sys.argv[1] if len(sys.argv) > 1 else "../frontend/openapi.json")
app = create_app(sessionmaker(create_engine("sqlite://")))
out.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n")
print(f"wrote {out}")
