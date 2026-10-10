"""GitHub App trust model: PRs/issues opened with a short-lived installation token.

Source never has to reach our servers: this provider is meant to run where the code already is (the
customer's CI runner, or their machine). It differs from LocalGitProvider only in how outward actions
authenticate: no personal credentials and no `gh` CLI - an installation token scoped to one repository,
minted from the App's private key and valid for about an hour.
"""

from __future__ import annotations

import base64
import os
import subprocess
import time
from pathlib import Path

import httpx
import jwt

from ..disclaimers import contains_banned_claim
from .base import PushNotAllowed, RepoError
from .local_git import LocalGitProvider

API = "https://api.github.com"
JWT_TTL_S = 540  # GitHub caps App JWTs at 10 minutes
TOKEN_SKEW_S = 120


class GitHubAppAuth:
    def __init__(self, app_id: str | int, private_key_pem: str, installation_id: str | int, *,
                 api_base: str = API, transport: httpx.BaseTransport | None = None, clock=time.time):
        self.app_id, self.key, self.installation_id = str(app_id), private_key_pem, str(installation_id)
        self.api_base, self._clock = api_base.rstrip("/"), clock
        self._http = httpx.Client(base_url=self.api_base, transport=transport, timeout=30)
        self._token: tuple[str, float] | None = None

    def app_jwt(self) -> str:
        now = int(self._clock())
        return jwt.encode({"iat": now - 30, "exp": now + JWT_TTL_S, "iss": self.app_id}, self.key, algorithm="RS256")

    def token(self) -> str:
        if self._token and self._token[1] - TOKEN_SKEW_S > self._clock():
            return self._token[0]
        r = self._http.post(f"/app/installations/{self.installation_id}/access_tokens", headers={
            "Authorization": f"Bearer {self.app_jwt()}", "Accept": "application/vnd.github+json"})
        if r.status_code != 201:
            raise RepoError(f"could not mint installation token ({r.status_code}): {r.text[:200]}")
        body = r.json()
        # `expires_at` is ISO-8601 Z; fall back to ~1h if absent
        exp = self._clock() + 3300
        self._token = (body["token"], exp)
        return self._token[0]

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}


class GitHubAppProvider(LocalGitProvider):
    def __init__(self, path: Path, repo: str, auth: GitHubAppAuth, *, allow_push: bool = False,
                 push_url: str | None = None, transport: httpx.BaseTransport | None = None):
        super().__init__(path, allow_push=allow_push)
        self.repo, self.auth = repo, auth
        self.push_url = push_url or f"https://github.com/{repo}.git"
        self._http = httpx.Client(base_url=auth.api_base, transport=transport, timeout=30)

    def _api(self, method: str, path: str, **kw) -> httpx.Response:
        r = self._http.request(method, path, headers=self.auth.headers(), **kw)
        if r.status_code >= 400:
            raise RepoError(f"GitHub API {method} {path} failed ({r.status_code}): {r.text[:300]}")
        return r

    def push(self, branch: str) -> None:
        self._require_push("push")
        env = dict(os.environ)
        token = ""
        if self.push_url.startswith("https://"):
            # Token goes to git via the environment as an auth header: never on the command line (visible in
            # `ps`), never in the URL, never written to .git/config.
            token = self.auth.token()
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            env.update({"GIT_CONFIG_COUNT": "1", "GIT_TERMINAL_PROMPT": "0",
                        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}"})
        r = subprocess.run(["git", "push", self.push_url, f"{branch}:refs/heads/{branch}"], cwd=self.path, env=env,
                           capture_output=True, text=True, check=False)
        if r.returncode != 0:
            err = r.stderr.replace(token, "***") if token else r.stderr
            raise RepoError(f"git push failed: {err[:400]}")

    def find_existing_pr(self, branch: str) -> str | None:
        owner = self.repo.split("/")[0]
        rows = self._api("GET", f"/repos/{self.repo}/pulls", params={"head": f"{owner}:{branch}", "state": "all"}).json()
        return rows[0]["html_url"] if rows else None

    def open_pr(self, branch: str, base: str, title: str, body: str) -> str:
        self._require_push("opening a pull request")
        self._require_clean_text(title, body)
        existing = self.find_existing_pr(branch)
        if existing:
            return existing
        return self._api("POST", f"/repos/{self.repo}/pulls",
                         json={"title": title, "body": body, "head": branch, "base": base, "draft": False}).json()["html_url"]

    def open_issue(self, title: str, body: str, labels: list[str] | None = None) -> str:
        self._require_push("opening an issue")
        self._require_clean_text(title, body)
        return self._api("POST", f"/repos/{self.repo}/issues",
                         json={"title": title, "body": body, "labels": labels or []}).json()["html_url"]


__all__ = ["GitHubAppAuth", "GitHubAppProvider", "PushNotAllowed", "contains_banned_claim"]
