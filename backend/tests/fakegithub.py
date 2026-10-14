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
        self.repos: dict[str, dict] = {}  # full_name -> {"sha": str, "tarball": bytes, "redirect": bool}
        self.token_requests = 0
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
        # ---- the App itself (installation tokens) and repository content, as the Attest App
        m = re.fullmatch(r"/app/installations/(\d+)/access_tokens", path)
        if m and req.method == "POST":
            self.token_requests += 1
            return httpx.Response(201, json={"token": f"ghs_install_{m.group(1)}", "expires_at": "2099-01-01T00:00:00Z"})
        if req.headers.get("authorization", "").startswith("Bearer ghs_install_"):
            return self.handle_installation(req, path)
        if path.startswith("/codeload/"):  # where a tarball redirect lands; no Authorization header should ever arrive here
            repo = self.repos[path.removeprefix("/codeload/").split("/tar/")[0].replace("__", "/")]
            self.calls.append(("CODELOAD-AUTH", req.headers.get("authorization", "")))
            return httpx.Response(200, content=repo["tarball"])
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

    def handle_installation(self, req: httpx.Request, path: str) -> httpx.Response:
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/commits/([^/]+)", path)
        if m and m.group(1) in self.repos:
            return httpx.Response(200, json={"sha": self.repos[m.group(1)]["sha"]})
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/tarball/([^/]+)", path)
        if m and m.group(1) in self.repos:
            repo = self.repos[m.group(1)]
            if repo.get("redirect_to"):
                return httpx.Response(302, headers={"location": repo["redirect_to"]})
            if repo.get("redirect"):
                return httpx.Response(302, headers={"location": f"https://codeload.github.com/codeload/{m.group(1).replace('/', '__')}/tar/{m.group(2)}"})
            return httpx.Response(200, content=repo["tarball"])
        return httpx.Response(404, json={"message": f"fake GitHub has no {req.method} {path}"})


def body(r: httpx.Response) -> dict:
    return json.loads(r.text)
