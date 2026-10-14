"""A small stateful fake of the parts of GitHub that Attest uses, as an httpx MockTransport.

State is plain Python so a test can set it up and inspect it: who exists, which installations they can reach, and (as
later milestones add them) repository files, branches and pull requests.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
from urllib.parse import parse_qs

import httpx


class FakeRepo:
    """A repository's git objects, enough for Attest's Git Data API calls. Content-addressed like git, so identical
    content gets identical ids and a test can ask 'what is at src/App.jsx on that branch?'."""

    def __init__(self, tarball: bytes, head_sha: str, *, modes: dict[str, str] | None = None):
        self.objects: dict[str, dict] = {}
        self.refs: dict[str, str] = {}
        self.pulls: list[dict] = []
        self.comments: list[dict] = []
        self.writable = True
        files: dict[str, tuple[str, str]] = {}
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as t:
            for m in t:
                if m.isfile():
                    rel = "/".join(m.name.split("/")[1:])
                    files[rel] = ((modes or {}).get(rel, "100644"), t.extractfile(m).read().decode("utf-8", "replace"))
        root = self._build({k: v for k, v in files.items()})
        self.objects[head_sha] = {"type": "commit", "tree": root, "parents": [], "message": "initial"}
        self.refs["refs/heads/main"] = head_sha

    def _put(self, obj: dict) -> str:
        sha = hashlib.sha1(json.dumps(obj, sort_keys=True).encode()).hexdigest()
        self.objects[sha] = obj
        return sha

    def _build(self, files: dict[str, tuple[str, str]]) -> str:
        tree: dict[str, dict] = {}
        subdirs: dict[str, dict] = {}
        for path, (mode, text) in files.items():
            head, _, rest = path.partition("/")
            if rest:
                subdirs.setdefault(head, {})[rest] = (mode, text)
            else:
                tree[head] = {"mode": mode, "type": "blob", "sha": self._put({"type": "blob", "content": text})}
        for d, sub in subdirs.items():
            tree[d] = {"mode": "040000", "type": "tree", "sha": self._build(sub)}
        return self._put({"type": "tree", "entries": tree})

    def set_path(self, tree_sha: str, parts: list[str], entry: dict) -> str:
        entries = dict(self.objects[tree_sha]["entries"])
        if len(parts) == 1:
            entries[parts[0]] = entry
        else:
            sub = entries.get(parts[0])
            entries[parts[0]] = {"mode": "040000", "type": "tree", "sha": self.set_path(sub["sha"], parts[1:], entry) if sub
                                 else self.set_path(self._put({"type": "tree", "entries": {}}), parts[1:], entry)}
        return self._put({"type": "tree", "entries": entries})

    def read(self, ref: str, path: str) -> tuple[str, str] | None:
        """(mode, text) of a file on a branch or commit, or None."""
        sha = self.refs.get(f"refs/heads/{ref}", ref)
        node = self.objects[self.objects[sha]["tree"]]
        parts = path.split("/")
        for i, part in enumerate(parts):
            e = node["entries"].get(part)
            if e is None:
                return None
            if i == len(parts) - 1:
                return e["mode"], self.objects[e["sha"]]["content"]
            node = self.objects[e["sha"]]
        return None

    def changed_paths(self, branch: str, base: str = "main") -> list[str]:
        out = []

        def walk(a, b, prefix=""):
            names = set((a or {}).get("entries", {})) | set((b or {}).get("entries", {}))
            for n in sorted(names):
                ea, eb = (a or {}).get("entries", {}).get(n), (b or {}).get("entries", {}).get(n)
                if ea == eb:
                    continue
                if (ea or eb)["type"] == "tree" and ea and eb:
                    walk(self.objects[ea["sha"]], self.objects[eb["sha"]], f"{prefix}{n}/")
                else:
                    out.append(f"{prefix}{n}")
        walk(self.objects[self.objects[self.refs[f"refs/heads/{base}"]]["tree"]], self.objects[self.objects[self.refs[f"refs/heads/{branch}"]]["tree"]])
        return out


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

    def repo_obj(self, full: str) -> FakeRepo | None:
        entry = self.repos.get(full)
        if entry is None:
            return None
        if "obj" not in entry:
            entry["obj"] = FakeRepo(entry["tarball"], entry["sha"], modes=entry.get("modes"))
        return entry["obj"]

    def handle_installation(self, req: httpx.Request, path: str) -> httpx.Response:
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/(.*)", path)
        if not m or m.group(1) not in self.repos:
            return httpx.Response(404, json={"message": f"fake GitHub has no {req.method} {path}"})
        full, rest = m.group(1), m.group(2)
        data = json.loads(req.content) if req.content else {}
        if rest.startswith("commits/"):
            return httpx.Response(200, json={"sha": self.repos[full]["sha"]})
        if rest.startswith("tarball/"):
            e = self.repos[full]
            if e.get("redirect_to"):
                return httpx.Response(302, headers={"location": e["redirect_to"]})
            if e.get("redirect"):
                return httpx.Response(302, headers={"location": f"https://codeload.github.com/codeload/{full.replace('/', '__')}/tar/{rest.split('/')[1]}"})
            return httpx.Response(200, content=e["tarball"])
        repo = self.repo_obj(full)  # only the git-data endpoints below need the object store, so it is built on first use
        if req.method != "GET" and not repo.writable:
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        mm = re.fullmatch(r"git/commits/(\w+)", rest)
        if mm and req.method == "GET":
            c = repo.objects.get(mm.group(1))
            return httpx.Response(200, json={"sha": mm.group(1), "tree": {"sha": c["tree"]}}) if c else httpx.Response(404, json={})
        mm = re.fullmatch(r"git/trees/(\w+)", rest)
        if mm and req.method == "GET":
            t = repo.objects.get(mm.group(1))
            return httpx.Response(200, json={"sha": mm.group(1), "tree": [{"path": k, **v} for k, v in t["entries"].items()]}) if t else httpx.Response(404, json={})
        if rest == "git/blobs" and req.method == "POST":
            return httpx.Response(201, json={"sha": repo._put({"type": "blob", "content": data["content"]})})
        if rest == "git/trees" and req.method == "POST":
            root = data["base_tree"]
            for e in data["tree"]:
                root = repo.set_path(root, e["path"].split("/"), {"mode": e["mode"], "type": "blob", "sha": e["sha"]})
            return httpx.Response(201, json={"sha": root})
        if rest == "git/commits" and req.method == "POST":
            return httpx.Response(201, json={"sha": repo._put({"type": "commit", "tree": data["tree"], "parents": data["parents"], "message": data["message"]})})
        if rest == "git/refs" and req.method == "POST":
            if data["ref"] in repo.refs:
                return httpx.Response(422, json={"message": "Reference already exists"})
            repo.refs[data["ref"]] = data["sha"]
            return httpx.Response(201, json={"ref": data["ref"], "object": {"sha": data["sha"]}})
        mm = re.fullmatch(r"git/ref/(heads/.+)", rest)
        if mm and req.method == "GET":
            sha = repo.refs.get(f"refs/{mm.group(1)}")
            return httpx.Response(200, json={"object": {"sha": sha}}) if sha else httpx.Response(404, json={"message": "Not Found"})
        if rest == "pulls" and req.method == "GET":
            head = req.url.params.get("head", "").split(":")[-1]
            return httpx.Response(200, json=[p for p in repo.pulls if p["head"]["ref"] == head])
        mm = re.fullmatch(r"issues/(\d+)/comments", rest)
        if mm and req.method == "POST":
            repo.comments.append({"number": int(mm.group(1)), "body": data["body"]})
            return httpx.Response(201, json={"id": len(repo.comments)})
        if rest == "pulls" and req.method == "POST":
            if getattr(repo, "fail_pulls", 0) > 0:  # a hiccup on GitHub's side, for retry tests
                repo.fail_pulls -= 1
                return httpx.Response(500, json={"message": "Server Error"})
            n = len(repo.pulls) + 1
            pr = {"number": n, "html_url": f"https://github.com/{full}/pull/{n}", "title": data["title"], "body": data["body"],
                  "head": {"ref": data["head"]}, "base": {"ref": data["base"]}, "state": "open"}
            repo.pulls.append(pr)
            return httpx.Response(201, json=pr)
        return httpx.Response(404, json={"message": f"fake GitHub has no {req.method} {path}"})

def body(r: httpx.Response) -> dict:
    return json.loads(r.text)
