import json
import subprocess
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from auditor.scm.base import PushNotAllowed, RepoError
from auditor.scm.github_app import GitHubAppAuth, GitHubAppProvider

REPO = "acme/widgets"


@pytest.fixture(scope="module")
def keys():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return priv.decode(), pub.decode()


class FakeGitHub:
    """Records every request; behaves like the few GitHub endpoints we use."""

    def __init__(self, pub, token_status=201):
        self.calls, self.pub, self.token_status, self.prs = [], pub, token_status, []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, req.url.path, dict(req.headers), body, dict(req.url.params)))
        if req.url.path.endswith("/access_tokens"):
            claims = jwt.decode(req.headers["authorization"].removeprefix("Bearer "), self.pub, algorithms=["RS256"],
                                options={"verify_exp": False, "verify_iat": False})  # tests inject a fake clock
            assert claims["iss"] == "123" and claims["exp"] - claims["iat"] <= 600
            return httpx.Response(self.token_status, json={"token": f"tok{len(self.calls)}", "expires_at": "x"})
        assert req.headers["authorization"].startswith("Bearer tok")  # never the App JWT / a personal token
        if req.method == "GET" and req.url.path.endswith("/pulls"):
            return httpx.Response(200, json=self.prs)
        if req.method == "POST" and req.url.path.endswith("/pulls"):
            self.prs = [{"html_url": f"https://github.com/{REPO}/pull/1"}]
            return httpx.Response(201, json=self.prs[0])
        if req.method == "POST" and req.url.path.endswith("/issues"):
            return httpx.Response(201, json={"html_url": f"https://github.com/{REPO}/issues/9"})
        return httpx.Response(404, json={"message": "nope"})


def make(tmp_path, keys, allow_push=True, **kw):
    gh = FakeGitHub(keys[1], kw.pop("token_status", 201))
    auth = GitHubAppAuth(123, keys[0], 456, transport=httpx.MockTransport(gh))
    return GitHubAppProvider(tmp_path, REPO, auth, allow_push=allow_push, transport=httpx.MockTransport(gh), **kw), gh


def test_app_jwt_is_signed_with_the_private_key_and_short_lived(keys):
    auth = GitHubAppAuth(123, keys[0], 456, transport=httpx.MockTransport(FakeGitHub(keys[1])))
    claims = jwt.decode(auth.app_jwt(), keys[1], algorithms=["RS256"])
    assert claims["iss"] == "123" and 0 < claims["exp"] - time.time() <= 600
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(auth.app_jwt(), rsa.generate_private_key(65537, 2048).public_key(), algorithms=["RS256"])


def test_installation_token_is_cached_then_refreshed_before_expiry(keys):
    gh, now = FakeGitHub(keys[1]), [1000.0]
    auth = GitHubAppAuth(123, keys[0], 456, transport=httpx.MockTransport(gh), clock=lambda: now[0])
    t1 = auth.token()
    assert auth.token() == t1 and len(gh.calls) == 1  # cached
    now[0] += 3300  # past the safety margin: must mint a new one, not use an about-to-expire token
    assert auth.token() != t1 and len(gh.calls) == 2


def test_token_failure_is_a_clear_error(tmp_path, keys):
    p, _ = make(tmp_path, keys, token_status=401)
    with pytest.raises(RepoError, match="installation token"):
        p.open_issue("t", "b")


def test_pr_is_opened_with_installation_token_and_is_idempotent(tmp_path, keys):
    p, gh = make(tmp_path, keys)
    u1 = p.open_pr("a11y/x", "main", "title", "body")
    u2 = p.open_pr("a11y/x", "main", "title", "body")
    assert u1 == u2 == f"https://github.com/{REPO}/pull/1"
    creates = [c for c in gh.calls if c[0] == "POST" and c[1].endswith("/pulls")]
    assert len(creates) == 1 and creates[0][3]["head"] == "a11y/x" and creates[0][3]["draft"] is False


def test_issue_creation_and_label_passthrough(tmp_path, keys):
    p, gh = make(tmp_path, keys)
    assert p.open_issue("work order", "body", ["accessibility"]).endswith("/issues/9")
    assert gh.calls[-1][3]["labels"] == ["accessibility"]


def test_nothing_is_sent_without_allow_push_and_banned_text_is_refused(tmp_path, keys):
    p, gh = make(tmp_path, keys, allow_push=False)
    for call in (lambda: p.push("b"), lambda: p.open_pr("b", "main", "t", "b"), lambda: p.open_issue("t", "b")):
        with pytest.raises(PushNotAllowed):
            call()
    assert gh.calls == []  # not even a token request
    q, gh2 = make(tmp_path, keys)
    with pytest.raises(RepoError, match="compliance"):
        q.open_pr("b", "main", "fully compliant", "x")
    assert not [c for c in gh2.calls if c[1].endswith("/pulls") and c[0] == "POST"]


def test_api_errors_surface_with_context(tmp_path, keys):
    p, _ = make(tmp_path, keys)
    with pytest.raises(RepoError, match="failed \\(404\\)"):
        p._api("GET", "/repos/acme/widgets/nope")


def test_push_to_a_real_remote_and_token_never_leaks(tmp_path, keys):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    work = tmp_path / "w"
    work.mkdir()
    g = lambda *a: subprocess.run(["git", *a], cwd=work, check=True, capture_output=True, text=True)
    g("init", "-q", "-b", "main")
    g("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "init")
    p, _ = make(work, keys, push_url=str(bare))
    p.push("main")
    assert "refs/heads/main" in subprocess.run(["git", "for-each-ref"], cwd=bare, capture_output=True, text=True).stdout


def test_https_push_sends_token_via_env_header_not_argv(tmp_path, keys, monkeypatch):
    p, _ = make(tmp_path, keys)
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["env"] = argv, kw["env"]
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    p.push("a11y/x")
    token = p.auth.token()
    assert token not in " ".join(seen["argv"]) and all("x-access-token" not in a for a in seen["argv"])
    assert seen["argv"][0] == "git" and "core.hooksPath=/dev/null" in seen["argv"]
    assert seen["argv"][-2:] == [f"https://github.com/{REPO}.git", "a11y/x:refs/heads/a11y/x"] and "push" in seen["argv"]
    assert seen["env"]["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert seen["env"]["GIT_CONFIG_VALUE_0"].startswith("AUTHORIZATION: basic ") and seen["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_push_failure_never_echoes_the_token(tmp_path, keys, monkeypatch):
    p, _ = make(tmp_path, keys)
    tok = p.auth.token()
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 1, "", f"fatal: Authentication failed for 'https://x-access-token:{tok}@github.com/'"))
    with pytest.raises(RepoError) as e:
        p.push("b")
    assert tok not in str(e.value) and "***" in str(e.value)
