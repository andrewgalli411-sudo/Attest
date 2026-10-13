"""What the signed-in person can reach on GitHub, asked with THEIR token (never the App's), so a user can only attach
repositories they genuinely have access to through an installation of the Attest App."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

MAX_PAGES = 5  # 500 repositories per installation is more than a first screen needs


class GitHubAuthExpired(RuntimeError):
    """GitHub no longer accepts the user's token; they have to sign in again."""


class GitHubError(RuntimeError):
    pass


@dataclass(frozen=True)
class Repo:
    installation_id: int
    full_name: str
    default_branch: str
    private: bool
    homepage: str | None


class GitHubUser:
    def __init__(self, token: str, *, api_base: str, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(base_url=api_base.rstrip("/"), transport=transport, timeout=20, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _pages(self, path: str, key: str) -> list[dict]:
        out: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            r = await self._http.get(path, params={"per_page": 100, "page": page})
            if r.status_code == 401:
                raise GitHubAuthExpired("GitHub no longer accepts your sign-in")
            if r.status_code != 200:
                raise GitHubError(f"GitHub answered {r.status_code} for {path}")
            items = r.json().get(key, [])
            out += items
            if len(items) < 100:
                break
        return out

    async def installations(self) -> list[dict]:
        return await self._pages("/user/installations", "installations")

    async def repos(self) -> list[Repo]:
        out: list[Repo] = []
        for inst in await self.installations():
            for r in await self._pages(f"/user/installations/{inst['id']}/repositories", "repositories"):
                out.append(Repo(int(inst["id"]), r["full_name"], r.get("default_branch") or "main", bool(r.get("private")),
                                r.get("homepage") or None))
        return out
