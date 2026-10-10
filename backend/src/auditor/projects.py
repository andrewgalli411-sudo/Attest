"""Operator-defined projects. The API refers to them by name; it never accepts commands from callers.

File format (JSON):
{"projects": {"web": {"repo_path": "/srv/repos/web", "dev_command": "pnpm dev --port {port}",
                      "check_commands": ["pnpm build"], "pages": [{"path": "/", "label": "home"}],
                      "auth_cookies": [{"name": "session", "value": "<dev seed>"}],
                      "base_branch": "main", "allow_push": false}}}
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator


class Project(BaseModel):
    repo_path: Path
    dev_command: str
    check_commands: list[str] = Field(default_factory=list)
    pages: list[dict] = Field(default_factory=lambda: [{"path": "/", "label": "home"}])
    auth_cookies: list[dict] = Field(default_factory=list)  # secrets: stay server-side, never returned by the API
    base_branch: str = "main"
    allow_push: bool = False  # whether runs on this project may publish branches / PRs / issues

    @field_validator("dev_command")
    @classmethod
    def _needs_port(cls, v: str) -> str:
        if "{port}" not in v:
            raise ValueError("dev_command must contain {port}")
        return v

    @field_validator("pages")
    @classmethod
    def _pages_are_paths(cls, v: list[dict]) -> list[dict]:
        for p in v:
            if not str(p.get("path", "")).startswith("/"):
                raise ValueError(f"page path must start with '/': {p!r}")
        return v


class ProjectConfigError(RuntimeError):
    pass


def load_projects(path: str | Path | None) -> dict[str, Project]:
    if not path:
        return {}
    p = Path(path)
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise ProjectConfigError(f"cannot read projects file {p}: {e}") from e
    out: dict[str, Project] = {}
    for name, body in (raw.get("projects") or {}).items():
        try:
            out[name] = Project(**body)
        except ValidationError as e:
            raise ProjectConfigError(f"project {name!r} is invalid: {e.errors()[0]['msg']}") from e
    return out
