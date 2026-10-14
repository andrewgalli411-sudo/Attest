"""Reading a repository as the Attest App: installation tokens, the head commit, and a size-capped archive download."""

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fakegithub import FakeGitHub

from auditor.scm.base import RepoError
from auditor.scm.github_app import GitHubAppAuth
from auditor.scm.github_repo import GitHubRepoClient


def private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


@pytest.fixture
def gh():
    g = FakeGitHub()
    g.repos["alice/web"] = {"sha": "a" * 40, "tarball": b"TARBALL-BYTES"}
    return g


def client(gh, **kw):
    auth = GitHubAppAuth("123", private_key_pem(), 7, api_base="https://api.github.com", transport=gh.transport)
    return GitHubRepoClient("alice/web", auth, transport=gh.transport, **kw), auth


async def test_the_head_commit_and_the_archive_are_read_with_one_installation_token(gh):
    c, _ = client(gh)
    assert await c.head_sha("main") == "a" * 40
    assert await c.archive("a" * 40) == b"TARBALL-BYTES"
    assert gh.token_requests == 1, "the installation token is minted once and reused"


async def test_a_missing_repository_or_branch_is_a_readable_error(gh):
    c, _ = client(gh)
    c.repo = "alice/gone"
    with pytest.raises(RepoError, match="not found, or the Attest app cannot read it"):
        await c.head_sha("main")
    with pytest.raises(RepoError, match="not found"):
        await c.archive("a" * 40)


async def test_the_download_redirect_goes_to_githubs_host_without_our_token(gh):
    gh.repos["alice/web"]["redirect"] = True
    c, _ = client(gh)
    assert await c.archive("a" * 40) == b"TARBALL-BYTES"
    assert ("CODELOAD-AUTH", "") in gh.calls, "the installation token must never be sent to the download host"


@pytest.mark.parametrize("target", ["https://evil.example/steal", "http://codeload.github.com/x", "https://github.com.evil.example/x",
                                    "https://169.254.169.254/latest"])
async def test_a_download_redirected_anywhere_else_is_refused(gh, target):
    gh.repos["alice/web"]["redirect_to"] = target
    c, _ = client(gh)
    with pytest.raises(RepoError, match="somewhere unexpected"):
        await c.archive("a" * 40)
    assert not any(p.startswith("/codeload") for _, p in gh.calls)


async def test_a_huge_archive_is_cut_off(gh):
    gh.repos["alice/web"]["tarball"] = b"x" * 5_000_000
    c, _ = client(gh, max_archive_bytes=1_000_000)
    with pytest.raises(RepoError, match="larger than Attest will read"):
        await c.archive("a" * 40)


async def test_a_malformed_commit_id_is_not_trusted(gh):
    gh.repos["alice/web"]["sha"] = "not-a-sha; rm -rf /"
    c, _ = client(gh)
    with pytest.raises(RepoError, match="unexpected commit id"):
        await c.head_sha("main")
