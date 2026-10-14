"""Reading (and, in the next milestone, changing) one repository through the GitHub API as the Attest App.

Authentication is an installation token: minted from the App's private key, scoped to the repositories the owner chose
when installing, and valid for about an hour. It is never the signed-in person's own token, so Attest can only do what the
installation allows. Nothing here clones or executes anything: the source arrives as an archive that `workspace.extract`
treats as hostile.
"""

from __future__ import annotations

import asyncio
from urllib.parse import quote, urlsplit

import httpx

from ..disclaimers import contains_banned_claim
from .base import RepoError
from .github_app import GitHubAppAuth

MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
DOWNLOAD_HOSTS = ("github.com", "githubusercontent.com")  # where GitHub sends archive downloads


class GitHubRepoClient:
    def __init__(self, repo: str, auth: GitHubAppAuth, *, transport: httpx.AsyncBaseTransport | None = None,
                 max_archive_bytes: int = MAX_ARCHIVE_BYTES, trust_redirects: bool = False):
        self.repo, self.auth, self.max_archive_bytes = repo, auth, max_archive_bytes
        self._transport, self._trust_redirects = transport, trust_redirects  # trust_redirects: tests and GitHub Enterprise

    async def _headers(self) -> dict[str, str]:
        return await asyncio.to_thread(self.auth.headers)  # may mint a token over the network

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.auth.api_base, transport=self._transport, timeout=30, follow_redirects=False)

    async def head_sha(self, branch: str) -> str:
        async with self._client() as http:
            r = await http.get(f"/repos/{self.repo}/commits/{branch}", headers=await self._headers())
        if r.status_code == 404:
            raise RepoError(f"{self.repo}: branch {branch!r} not found, or the Attest app cannot read it")
        if r.status_code != 200:
            raise RepoError(f"GitHub answered {r.status_code} while reading {self.repo}")
        sha = r.json().get("sha", "")
        if len(sha) < 7 or not all(c in "0123456789abcdef" for c in sha):
            raise RepoError("GitHub returned an unexpected commit id")
        return sha

    async def archive(self, ref: str) -> bytes:
        """The repository at `ref` as a gzip tarball, read with a hard size cap."""
        async with self._client() as http:
            url = f"/repos/{self.repo}/tarball/{ref}"
            headers = await self._headers()
            for _ in range(3):
                async with http.stream("GET", url, headers=headers) as r:
                    if r.status_code in (301, 302, 307, 308) and r.headers.get("location"):
                        url = r.headers["location"]
                        host = urlsplit(url).hostname or ""
                        if urlsplit(url).netloc:  # absolute: GitHub's download host. Never send our token there.
                            if not self._trust_redirects and (urlsplit(url).scheme != "https" or not host.endswith(DOWNLOAD_HOSTS)):
                                raise RepoError("GitHub sent the download somewhere unexpected")
                            headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
                        continue
                    if r.status_code == 404:
                        raise RepoError(f"{self.repo}@{ref[:10]} not found, or the Attest app cannot read it")
                    if r.status_code != 200:
                        raise RepoError(f"GitHub answered {r.status_code} while downloading {self.repo}")
                    body = bytearray()
                    async for chunk in r.aiter_bytes():
                        body += chunk
                        if len(body) > self.max_archive_bytes:
                            raise RepoError("the repository is larger than Attest will read")
                    return bytes(body)
        raise RepoError("too many redirects while downloading the repository")


    # ------------------------------------------------------------------------------------------ writing (Git Data API)
    async def _api(self, method: str, path: str, *, expect: tuple[int, ...] = (200, 201), **kw) -> httpx.Response:
        async with self._client() as http:
            r = await http.request(method, path, headers=await self._headers(), **kw)
        if r.status_code in (401, 403) and "rate limit" not in r.text.lower():
            raise RepoError(f"the Attest app is not allowed to do that on {self.repo} (GitHub answered {r.status_code}); check its permissions")
        if r.status_code not in expect:
            raise RepoError(f"GitHub answered {r.status_code} for {method} {path.split('?')[0]}: {r.text[:200]}")
        return r

    async def commit_tree(self, commit_sha: str) -> str:
        return (await self._api("GET", f"/repos/{self.repo}/git/commits/{commit_sha}")).json()["tree"]["sha"]

    async def file_mode(self, tree_sha: str, path: str) -> str:
        """The git mode of an existing file ("100644", "100755"), walking the tree one level at a time. A symlink,
        submodule or directory is refused: Attest edits ordinary files only."""
        parts = path.split("/")
        sha = tree_sha
        for i, part in enumerate(parts):
            entries = (await self._api("GET", f"/repos/{self.repo}/git/trees/{sha}")).json().get("tree", [])
            hit = next((e for e in entries if e["path"] == part), None)
            if hit is None:
                raise RepoError(f"{path} does not exist at that commit")
            if i < len(parts) - 1:
                if hit["type"] != "tree":
                    raise RepoError(f"{path} is not inside an ordinary directory")
                sha = hit["sha"]
            elif hit["type"] != "blob" or hit["mode"] not in ("100644", "100755"):
                raise RepoError(f"{path} is a link or special entry; Attest will not change it")
            else:
                return hit["mode"]
        raise RepoError(f"{path} is not a file")

    async def commit_files(self, parent_commit: str, parent_tree: str, files: dict[str, str], message: str) -> tuple[str, str]:
        """One commit on top of `parent_commit` that sets each path to its new text. Returns (commit sha, tree sha)."""
        if contains_banned_claim(message):
            raise RepoError("refusing to publish text containing a compliance/certification claim")
        entries = []
        for path, text in sorted(files.items()):
            mode = await self.file_mode(parent_tree, path)
            blob = (await self._api("POST", f"/repos/{self.repo}/git/blobs", json={"content": text, "encoding": "utf-8"})).json()["sha"]
            entries.append({"path": path, "mode": mode, "type": "blob", "sha": blob})
        tree = (await self._api("POST", f"/repos/{self.repo}/git/trees", json={"base_tree": parent_tree, "tree": entries})).json()["sha"]
        commit = (await self._api("POST", f"/repos/{self.repo}/git/commits",
                                  json={"message": message, "tree": tree, "parents": [parent_commit]})).json()["sha"]
        return commit, tree

    async def branch_sha(self, branch: str) -> str | None:
        r = await self._api("GET", f"/repos/{self.repo}/git/ref/heads/{quote(branch, safe='/')}", expect=(200, 404))
        return r.json()["object"]["sha"] if r.status_code == 200 else None

    async def create_branch(self, branch: str, sha: str) -> None:
        await self._api("POST", f"/repos/{self.repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha})

    async def find_pr(self, branch: str) -> tuple[str, int] | None:
        owner = self.repo.split("/")[0]
        rows = (await self._api("GET", f"/repos/{self.repo}/pulls", params={"head": f"{owner}:{branch}", "state": "all"})).json()
        return (rows[0]["html_url"], rows[0]["number"]) if rows else None

    async def open_pr(self, head: str, base: str, title: str, body: str) -> tuple[str, int]:
        if contains_banned_claim(title) or contains_banned_claim(body):
            raise RepoError("refusing to publish text containing a compliance/certification claim")
        r = (await self._api("POST", f"/repos/{self.repo}/pulls", json={"title": title, "body": body, "head": head, "base": base, "draft": False})).json()
        return r["html_url"], r["number"]

    async def comment(self, number: int, body: str) -> None:
        """A comment on a pull request (pull requests are issues for the purpose of comments)."""
        if contains_banned_claim(body):
            raise RepoError("refusing to publish text containing a compliance/certification claim")
        await self._api("POST", f"/repos/{self.repo}/issues/{number}/comments", json={"body": body})
