"""Static checks of the deployment files against the real project. Docker is not available in every environment,
so these catch the mistakes that otherwise only show up on the first image build."""

import re
import tomllib
from pathlib import Path

import pytest
import yaml

from auditor.config import Settings

ROOT = Path(__file__).resolve().parents[2]
BACKEND, FRONTEND = ROOT / "backend", ROOT / "frontend"
SETTING_FIELDS = {f.upper() for f in Settings.model_fields}


def lines(path: Path) -> list[str]:
    return [ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.strip().startswith("#")]


def locked_version(pkg: str) -> str:
    lock = tomllib.loads((BACKEND / "uv.lock").read_text())
    return next(p["version"] for p in lock["package"] if p["name"] == pkg)


# ---------------------------------------------------------------- Dockerfiles
def test_backend_image_uses_the_playwright_build_that_matches_the_locked_library():
    base = next(ln for ln in lines(BACKEND / "Dockerfile") if ln.startswith("FROM "))
    m = re.search(r"playwright/python:v([\d.]+)-", base)
    assert m, base
    assert m.group(1) == locked_version("playwright"), "the browser build in the image must match uv.lock"


@pytest.mark.parametrize("df", ["backend/Dockerfile", "frontend/Dockerfile"])
def test_dockerfile_hygiene(df):
    ls = lines(ROOT / df)
    froms = [ln for ln in ls if ln.startswith("FROM ")]
    assert froms and all(":" in f.split()[1] and not f.split()[1].endswith(":latest") for f in froms), "pin image tags"
    users = [i for i, ln in enumerate(ls) if ln.startswith("USER ")]
    cmd = next(i for i, ln in enumerate(ls) if ln.startswith("CMD "))
    assert users and users[-1] < cmd and "root" not in ls[users[-1]], "must drop root before running"
    assert not any(re.match(r"ADD\s+https?://", ln) for ln in ls), "no remote ADD"
    assert any(ln.startswith("HEALTHCHECK") for ln in ls)
    assert not any("curl" in ln and "| sh" in ln.replace("| bash", "| sh") and "nodesource" not in ln for ln in ls)


@pytest.mark.parametrize("context,df", [(BACKEND, "Dockerfile"), (FRONTEND, "Dockerfile")])
def test_every_COPY_source_exists_inside_the_build_context(context, df):
    for ln in lines(context / df):
        if ln.startswith("COPY ") and "--from=" not in ln:
            parts = [p for p in ln.split()[1:] if not p.startswith("--")]
            for src in parts[:-1]:
                assert not src.startswith(".."), f"{ln!r} reaches outside the build context, which Docker forbids"
                if src != ".":
                    assert (context / src).exists(), f"{ln!r}: {src} does not exist in {context.name}/"


def test_the_backend_start_command_runs_real_things():
    cmd = next(ln for ln in lines(BACKEND / "Dockerfile") if ln.startswith("CMD "))
    assert "alembic upgrade head" in cmd, "schema must be migrated, never auto-created, in production"
    for mod in re.findall(r"-m (auditor[\w.]*)", cmd) + re.findall(r"python -m (auditor[\w.]*)", cmd):
        assert (BACKEND / "src" / Path(*mod.split("."))).with_suffix(".py").exists() or (BACKEND / "src" / Path(*mod.split(".")) / "__init__.py").exists(), mod
    assert "auditor.main:create_app" in cmd and "--factory" in cmd
    assert (BACKEND / "alembic.ini").exists() and list((BACKEND / "alembic" / "versions").glob("*.py"))


@pytest.mark.parametrize("d,must", [(BACKEND, {".venv", "*.db", "evidence"}), (FRONTEND, {"node_modules", ".next"})])
def test_dockerignore_keeps_local_state_and_secrets_out_of_images(d, must):
    ignored = set(lines(d / ".dockerignore"))
    assert must <= ignored, must - ignored
    assert any(x.startswith(".env") for x in ignored) or d is BACKEND


# ---------------------------------------------------------------- compose
@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def test_compose_publishes_only_the_web_app(compose):
    svc = compose["services"]
    assert "ports" not in svc["api"] and "ports" not in svc["db"], "the API and database must not be reachable from outside"
    assert svc["web"]["ports"]
    assert set(svc) == {"db", "api", "web"}


def test_compose_requires_secrets_instead_of_shipping_defaults(compose):
    text = (ROOT / "docker-compose.yml").read_text()
    for var in ("POSTGRES_PASSWORD", "API_TOKEN", "REPOS_DIR"):
        assert f"${{{var}:?" in text, f"{var} must be mandatory"
    assert not re.search(r"(PASSWORD|TOKEN|KEY)\s*:\s*['\"]?[A-Za-z0-9]{6,}['\"]?\s*$", text, re.MULTILINE), "no literal secrets"


def test_compose_environment_names_are_real_settings(compose):
    env = compose["services"]["api"]["environment"]
    unknown = {k for k in env if k not in SETTING_FIELDS}
    assert not unknown, f"compose sets variables the app ignores: {unknown}"


def test_compose_services_have_healthchecks_ordering_and_restart_policy(compose):
    assert compose["services"]["db"]["healthcheck"] and compose["services"]["api"]["depends_on"]["db"]["condition"] == "service_healthy"
    assert all(compose["services"][s]["restart"] == "unless-stopped" for s in compose["services"])
    assert compose["services"]["db"]["image"].split(":")[1].isdigit(), "pin the postgres major version"
    # the web app reaches the API by service name, so that name must be an allowed host
    assert '"api"' in compose["services"]["api"]["environment"]["ALLOWED_HOSTS"]


# ---------------------------------------------------------------- .env.example and CI
def test_env_example_documents_only_real_settings_and_the_web_variables():
    web_only = {"API_INTERNAL_URL", "AUDITOR_API_TOKEN", "UI_BASIC_AUTH", "POSTGRES_PASSWORD", "REPOS_DIR"}
    keys = {ln.split("=")[0].strip() for ln in (ROOT / ".env.example").read_text().splitlines()
            if "=" in ln and not ln.lstrip().startswith("#")}
    unknown = {k for k in keys if k not in SETTING_FIELDS and k not in web_only}
    assert not unknown, f".env.example lists variables nothing reads: {unknown}"
    assert {"API_TOKEN", "DATABASE_URL", "ANTHROPIC_API_KEY"} <= keys
    assert "ANTHROPIC_API_KEY=sk" not in (ROOT / ".env.example").read_text()


def test_ci_workflow_runs_the_real_checks_and_requires_postgres():
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert set(wf["jobs"]) == {"backend", "frontend"}
    for needed in ("ruff check", "pytest", "pnpm lint", "pnpm typecheck", "pnpm build", "git diff --exit-code"):
        assert needed in text, needed
    assert "A11Y_REQUIRE_POSTGRES" in text, "CI must fail, not skip, when Postgres is missing"
    assert all(re.search(r"@v\d+$", s["uses"]) for j in wf["jobs"].values() for s in j["steps"] if "uses" in s), "pin action versions"
    assert wf["jobs"]["backend"]["services"]["postgres"]["image"] == "postgres:16"
    assert wf["permissions"] == {"contents": "read"}, "run CI with least privilege"
    assert "secrets." not in text, "the workflow needs no secrets"


def test_the_checked_in_generated_api_types_match_the_backend_schema():
    """CI regenerates the frontend types and fails on a diff; this is the same check, locally."""
    import json

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from auditor.main import create_app

    live = create_app(sessionmaker(create_engine("sqlite://")), config=Settings()).openapi()
    on_disk = json.loads((FRONTEND / "openapi.json").read_text())
    assert live == on_disk, "run: uv run python scripts/export_openapi.py && (cd ../frontend && pnpm gen:api)"
