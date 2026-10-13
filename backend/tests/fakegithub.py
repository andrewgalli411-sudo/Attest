"""A small stateful fake of the parts of GitHub that Attest uses, as an httpx MockTransport.

State is plain Python so a test can set it up and inspect it: who exists, which installations they can reach, and (as
later milestones add them) repository files, branches and pull requests.
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs

import httpx


class FakeGitHub:
    def __init__(self):
        self.users: dict[str, dict] = {}  # login -> profile
        self.installations: dict[int, dict] = {}  # id -> {"account": login, "repos": {full_name: {...}}}
        self.access: dict[str, list[int]] = {}  # login -> installation ids that person can reach
        self.expired_tokens: set[str] = set()  # user tokens GitHub no longer accepts
        self.calls: list[tuple[str, str]] = []
        self.transport = httpx.MockTransport(self.handle)

    # ---- set-up helpers
    def add_user(self, login: str, gh_id: int, name: str | None = None) -> None:
        self.users[login] = {"id": gh_id, "login": login, "name": name or login.title(), "avatar_url": None}
        self.access.setdefault(login, [])

    def add_installation(self, inst_id: int, account: str, repos: list[str], *, who: list[str] | None = None) -> None:
        self.installations[inst_id] = {"account": account, "repos": {
            r: {"full_name": r, "default_branch": "main", "private": True, "homepage": None} for r in repos}}
        for login in who if who is not None else [account]:
            self.access.setdefault(login, []).append(inst_id)

    # ---- the fake itself
    def handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append((req.method, path))
        if path == "/login/oauth/access_token":
            code = parse_qs(req.content.decode())["code"][0].removeprefix("code-")
            return httpx.Response(200, json={"access_token": f"gho_secret_{code}"} if code in self.users else {"error": "bad_verification_code"})
        token = req.headers.get("authorization", "").removeprefix("Bearer ")
        login = token.removeprefix("gho_secret_") if token.startswith("gho_secret_") else None
        if login not in self.users or token in self.expired_tokens:
            return httpx.Response(401, json={"message": "Bad credentials"})
        if path == "/user":
            return httpx.Response(200, json=self.users[login])
        if path == "/user/installations":
            return httpx.Response(200, json={"installations": [{"id": i, "account": {"login": self.installations[i]["account"]}}
                                                               for i in self.access.get(login, [])]})
        m = re.fullmatch(r"/user/installations/(\d+)/repositories", path)
        if m:
            inst = int(m.group(1))
            if inst not in self.access.get(login, []):
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"repositories": list(self.installations[inst]["repos"].values())})
        return httpx.Response(404, json={"message": f"fake GitHub has no {req.method} {path}"})


def body(r: httpx.Response) -> dict:
    return json.loads(r.text)
