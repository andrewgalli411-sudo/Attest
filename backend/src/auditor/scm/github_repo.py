"""Reading (and, in the next milestone, changing) one repository through the GitHub API as the Attest App.

Authentication is an installation token: minted from the App's private key, scoped to the repositories the owner chose
when installing, and valid for about an hour. It is never the signed-in person's own token, so Attest can only do what the
installation allows. Nothing here clones or executes anything: the source arrives as an archive that `workspace.extract`
treats as hostile.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import httpx

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
