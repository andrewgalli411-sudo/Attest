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

    # ---- accounts (multi-user). "local": one person, API token or loopback. "accounts": sign in with GitHub, every
    # record owned by an account; operator projects and custom commands are switched off.
    auth_mode: str = "local"
    public_url: str = "http://localhost:3000"  # the address people use; OAuth redirects and cookie flags derive from it
    session_ttl_s: int = 7 * 24 * 3600
    # Encrypts secrets at rest (test-login cookies, GitHub user tokens). A urlsafe base64 32-byte key; required in
    # accounts mode. Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    secrets_key: str = ""
    github_app_id: str = ""
    github_app_private_key: str = ""  # PEM; \n escapes accepted
    github_client_id: str = ""
    github_client_secret: str = ""
    github_webhook_secret: str = ""
    github_app_slug: str = ""  # the App's URL name, for the "install on your repositories" link
    max_projects_per_account: int = 20
    # Which sites an account may attach. https on 443 only, unless the operator widens it (development and tests).
    allow_insecure_sites: bool = False
    allowed_site_ports: list[int] = [443]
    github_web_base: str = "https://github.com"
    github_api_base: str = "https://api.github.com"

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

    model_token_budget: int = 1_500_000  # per run; when spent, remaining findings go to a person
    max_findings_per_page: int = 200
    refusal_fallback: bool = True  # server-side retry on a fallback model when the primary declines a request
    max_request_bytes: int = 64 * 1024
    max_concurrent_runs: int = 1
    run_timeout_s: int = 3600


settings = Settings()
