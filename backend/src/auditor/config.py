from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./auditor.db"
    evidence_dir: str = "./evidence"
    anthropic_api_key: str = ""
    judge_model: str = "claude-sonnet-5-5"
    triage_model: str = "claude-opus-5-5"
    fix_model: str = "claude-opus-5-5"
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    # ---- access control
    # If set, every /api request (except /api/health) must carry `Authorization: Bearer <token>`.
    # If empty, only loopback clients are served (development mode).
    api_token: str = ""
    # Host header allow-list (blocks DNS-rebinding against the loopback-only mode). "*" disables the check.
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "[::1]"]

    # ---- what the API is allowed to make this server do
    # Path to a JSON file of operator-defined projects (repo path, dev command, checks, seeded session).
    projects_file: str = ""
    # DANGEROUS: lets API callers supply an arbitrary repo path and shell commands. Development only.
    allow_custom_runs: bool = False
    demo_enabled: bool = True

    # Interactive API docs and the OpenAPI schema are unauthenticated by nature; off unless explicitly enabled.
    enable_docs: bool = False

    # ---- run limits
    # ---- privacy: what may be sent to the language model
    send_screenshots_to_model: bool = False

    max_concurrent_runs: int = 1
    run_timeout_s: int = 3600


settings = Settings()
