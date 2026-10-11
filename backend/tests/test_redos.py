"""Every function that chews on untrusted text (page content, source files, model output) must be fast on
hostile input. They run synchronously inside the API's event loop, so one slow regex freezes the whole server.
A real finding drove this file: redact() took ~15 s on 200 KB of 'a'."""

import random
import string
import time

import pytest

from auditor.disclaimers import contains_banned_claim
from auditor.fix.safety import DENY, UnsafeEdit, check_edit_shape, parse_attrs
from auditor.fix.source_map import opening_tag_end
from auditor.privacy import MAX_CHARS, PLACEHOLDER, redact, redact_json

N = 200_000
LIMIT_S = 1.5  # ~50x the measured worst case, so a loaded CI machine does not flake, while a quadratic rule (15 s) fails

HOSTILE = {
    "letters": "a" * N, "dots": "." * N, "dashes": "-" * N, "digits": "1 " * (N // 2), "alnum": "A1" * (N // 2),
    "ats": "a@" * (N // 2), "pem-no-end": "-----BEGIN PRIVATE KEY-----\n" * (N // 28),
    "pem-fake-header": "-----BEGIN " * (N // 11), "lt": "<" * N, "lt-a": "<a " * (N // 3), "quotes": '"' * N,
    "braces": "{" * N, "open-tag": "<input " + "a=1 " * (N // 4), "eq": "=" * N, "urls": "http://" * (N // 7),
    "keywords": "password: " * (N // 10), "bidi": chr(0x202E) * N, "nul": "\x00" * N, "slashes": "/" * N,
    "nested": "<a b={" + "{" * 50_000 + "}" * 50_000 + "}>", "emoji": "\U0001f600" * (N // 4),
    "claims": "compliant " * (N // 10), "whitespace": " " * N + "x", "newlines": "\n" * N,
}


def timed(fn, text):
    t = time.perf_counter()
    try:
        fn(text)
    except (UnsafeEdit, RecursionError, ValueError):
        pass  # a clean refusal is fine; being slow is not
    return time.perf_counter() - t


@pytest.mark.parametrize("name", list(HOSTILE))
def test_redaction_is_fast_on_hostile_input(name):
    assert timed(redact, HOSTILE[name]) < LIMIT_S, name


@pytest.mark.parametrize("name", list(HOSTILE))
def test_every_deny_pattern_is_fast_on_hostile_input(name):
    assert timed(lambda t: [p.findall(t) for p, _ in DENY], HOSTILE[name]) < LIMIT_S, name
    assert timed(contains_banned_claim, HOSTILE[name]) < LIMIT_S, name
    assert timed(lambda t: PLACEHOLDER.findall(t), HOSTILE[name]) < LIMIT_S, name


@pytest.mark.parametrize("name", list(HOSTILE))
def test_the_html_tag_scanner_and_edit_policy_are_fast_on_hostile_input(name):
    text = HOSTILE[name]
    assert timed(lambda t: opening_tag_end(t, 0) if t.startswith("<") else None, text) < LIMIT_S, name
    assert timed(lambda t: parse_attrs(t) if t.startswith("<") else None, text) < LIMIT_S, name
    assert timed(lambda t: check_edit_shape("label", "<input name='a' />", t), text) < LIMIT_S, name
    assert timed(lambda t: check_edit_shape("label", t, "<input name='a' />"), text) < LIMIT_S, name


def test_redaction_has_a_hard_input_cap():
    out = redact("x" * (MAX_CHARS * 3))
    assert len(out) <= MAX_CHARS + 20 and out.endswith("[truncated]")


def test_unterminated_and_fake_pem_blocks_are_still_handled_correctly():
    assert "[private-key]" in redact("-----BEGIN RSA PRIVATE KEY-----\nMIIEow" + "A" * 50)  # unterminated: assume key
    real = "x -----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY----- y"
    assert redact(real) == "x [private-key] y"
    assert redact("-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----") == "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----"
    assert redact("a -----BEGIN  b") == "a -----BEGIN  b"


def test_redact_json_is_fast_on_deep_and_wide_structures():
    big = {"k" * 5: ["a@" * 5000] * 50, "nested": {"x": {"y": ["1 " * 5000] * 50}}}
    t = time.perf_counter()
    redact_json(big)
    assert time.perf_counter() - t < LIMIT_S * 2


# ---------------- the unlabeled-secret rule: catches random tokens, leaves identifiers alone ----------------
def _rand_token(seed, n, alphabet):
    rnd = random.Random(seed)
    return "".join(rnd.choice(alphabet) for _ in range(n))


def test_random_unlabeled_tokens_are_mostly_caught_and_the_miss_rate_is_known():
    """A statistical heuristic, so the honest test is a rate, not 'all'. Measured ~94%; assert it does not regress."""
    alpha = string.ascii_letters + string.digits
    samples = []
    for seed in range(600):
        tok = _rand_token(seed, 20 + seed % 12, alpha)
        if any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok):
            samples.append(tok)
    caught = sum("[token]" in redact(f'<div data-x="{t}">') for t in samples)
    assert caught / len(samples) >= 0.90, f"only {caught}/{len(samples)} random tokens redacted"


def test_a_caught_token_really_is_removed_from_the_text():
    tok = "q9Xk2LmZp4RtVb7NcYw3Hs"
    assert tok not in redact(f'<div data-x="{tok}">')


BENIGN = [
    "A11Y_TEST_POSTGRES_URL", "A11Y_REQUIRE_POSTGRES", "profile-form-email-input-2", "max_concurrent_runs=1",
    "public_exponent=65537", "listen_addresses=127", "handleSubmitButtonClick", "UserProfileSettingsPanel",
    "backgroundColorTransition", "aria-labelledby-email-label", "data-testid-primary-button",
    "ThisIsAVeryLongComponentName", "field-label-required-marker", "autoComplete-street-address",
    "sha256-integrity-attribute", "react-aria-focus-scope-1", "next-font-geist-sans-variable",
    "text-muted-foreground-hover", "accessibility-conformance-report", "frontend/components/ui.tsx:42",
    "Automated first pass requires review", "WCAG 2.1 Success Criterion 1.4.3",
]


@pytest.mark.parametrize("text", BENIGN)
def test_ordinary_identifiers_and_text_are_not_mangled(text):
    assert redact(text) == text


def test_the_unlabeled_secret_rules_do_not_mangle_this_projects_own_ui_code():
    """Real source, not toy strings: none of the heuristic rules may fire on the UI code or the demo app.
    (Keyed rules such as `token = ...` legitimately change backend code, so only the heuristics are checked.)"""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    offenders = []
    for rel in ("frontend/app", "frontend/components", "frontend/lib", "demo-app-template/src", "backend/src/auditor/api"):
        for p in (root / rel).rglob("*"):
            if p.is_file() and p.suffix in {".py", ".ts", ".tsx", ".jsx", ".css"}:
                text = p.read_text(errors="replace")
                if len(text) < MAX_CHARS and "[token]" in redact(text):
                    offenders.append(p.relative_to(root).as_posix())
    assert offenders == [], offenders


@pytest.mark.parametrize("tok", ["d41d8cd98f00b204e9800998ecf8427e", "da39a3ee5e6b4b0d3255bfef95601890afd80709",
                                 "3f2504e0-4f89-11d3-9a0c-0305e82c3301", "AbCdEfGhIjKlMnOpQrStUvWxYz012345",
                                 "xK9mP2vQ7nR4tY8wZ3bC6dF1gH5jL0sA"])
def test_long_hashes_ids_and_random_keys_are_redacted(tok):
    assert tok not in redact(f"id={tok}")


@pytest.mark.parametrize("ident", ["evidence_blob_api_evidence__evidence_id__get", "accessibility-conformance-report",
                                   "ThisIsAVeryLongComponentNameForTheProfilePanel", "frontend-components-ui-status-badge-label"])
def test_long_word_like_identifiers_are_left_alone(ident):
    assert redact(ident) == ident
