import json

import pytest
from fakes import FakeTriage
from sqlalchemy import select

from auditor.fix.author import author_fix
from auditor.fix.source_map import SourceHit
from auditor.models import FindingEvidence
from auditor.privacy import PLACEHOLDER, redact, redact_json
from auditor.triage.triage import triage_one

SECRETS = [
    "jane.doe@example.com", "+1 (415) 555-0134", "415-555-0134", "4111 1111 1111 1111",
    "sk-live-abcdefghijklmnop1234", "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUV", "AKIAIOSFODNN7EXAMPLE",
    "ghp_abcdefghijklmnopqrstuvwxyz0123", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "d41d8cd98f00b204e9800998ecf8427e",
]
# A bare word has no signature and cannot be recognised as a secret by any pattern. It is only caught when a key
# name gives it away (password: "...", token=...). This is a stated limit of the redactor, not an oversight.
KEYED_ONLY = "hunter2-super-secret"


@pytest.mark.parametrize("secret", SECRETS)
def test_secrets_and_personal_data_are_removed(secret):
    for wrap in ("{}", 'const x = "{}";', "<p>{}</p>", 'password: "{}"', "token={}", "Contact {} today"):
        out = redact(wrap.format(secret))
        assert secret not in out, (wrap, out)


@pytest.mark.parametrize("wrap", ['password: "{}"', "token={}", '{{"api_key": "{}"}}', "Authorization: {}", "const secret = '{}'"])
def test_keyed_secrets_are_removed_even_without_a_recognisable_shape(wrap):
    assert KEYED_ONLY not in redact(wrap.format(KEYED_ONLY))


def test_a_bare_word_with_no_key_name_is_a_known_limit():
    assert redact("hunter2-super-secret") == "hunter2-super-secret"


def test_input_values_are_redacted_but_structure_survives():
    html = '<div class="field"><span>Email address</span><input name="email" type="email" value="jane@x.org"></div>'
    out = redact(html)
    assert "jane@x.org" not in out and 'value="[redacted]"' in out
    assert '<span>Email address</span>' in out and 'name="email"' in out and 'type="email"' in out


def test_private_key_blocks_are_removed_whole():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\nabc\n-----END RSA PRIVATE KEY-----"
    assert "MIIEow" not in redact(f"const k = `{pem}`")


@pytest.mark.parametrize("text", ["Email address", "Search the site", "Save profile", "Price (USD)", "Step 2 of 3", "WCAG 2.1",
                                  "Naviguer vers l'accueil", '<button className="icon-btn" type="button">', "id=\"full-name\""])
def test_ordinary_ui_text_is_left_alone(text):
    assert redact(text) == text


def test_redact_json_walks_nested_structures():
    out = redact_json({"a": ["x@y.com", {"b": "ok", "c": 5}], "d": None})
    assert out == {"a": ["[email]", {"b": "ok", "c": 5}], "d": None}


# ---------------- nothing leaves in the actual model requests ----------------
async def test_triage_request_contains_none_of_the_secrets_and_no_image_by_default(sf, tmp_path):
    from test_triage import make_finding

    fid, store = make_finding(sf, tmp_path)
    with sf() as db:
        for e in db.scalars(select(FindingEvidence).where(FindingEvidence.finding_id == fid)):
            if e.kind == "dom_snippet":
                e.payload = {"html": f'<p>Hi {SECRETS[0]} call {SECRETS[1]}</p><input value="{KEYED_ONLY}">'}
            if e.kind == "source_hit":
                e.payload = {"unique": True, "reason": "x", "hit": {
                    "file": "a.jsx", "line": 1, "col": 0, "method": "grep", "anchor": "<input />",
                    "excerpt": f'const KEY = "{SECRETS[4]}"; // {SECRETS[7]}'}}
        db.commit()
    client = FakeTriage("good")
    await triage_one(sf, store, client, "m", fid)
    sent = json.dumps(client.calls[0]["messages"], default=str)
    for s in (SECRETS[0], SECRETS[1], SECRETS[4], SECRETS[7]):
        assert s not in sent, s
    assert [b["type"] for b in client.calls[0]["messages"][0]["content"]] == ["text"]  # no screenshot unless enabled


async def test_screenshots_are_sent_only_when_explicitly_enabled(sf, tmp_path):
    from test_triage import make_finding

    fid, store = make_finding(sf, tmp_path, key="img")
    client = FakeTriage("good")
    await triage_one(sf, store, client, "m", fid, send_image=True)
    assert [b["type"] for b in client.calls[0]["messages"][0]["content"]] == ["text", "image"]


async def test_fix_authoring_request_redacts_the_file_and_finding_context():
    file_text = f'const KEY = "{SECRETS[4]}";\nconst owner = "{SECRETS[0]}";\n<input name="email" />\n'
    hit = SourceHit("a.jsx", 3, 0, "grep", '<input name="email" />', "")

    class Spy:
        calls: list = []  # noqa: RUF012

        def __init__(self):
            self.messages = self

        async def create(self, **kw):
            Spy.calls.append(kw)
            raise RuntimeError("stop after capturing the request")

    with pytest.raises(RuntimeError):
        await author_fix(Spy(), "m", {"rule": "label", "dom": f"<b>{SECRETS[1]}</b>"}, hit, "plan", file_text)
    sent = Spy.calls[0]["messages"][0]["content"]
    for s in (SECRETS[4], SECRETS[0], SECRETS[1]):
        assert s not in sent
    assert '<input name=\\"email\\" />' in sent or '<input name=' in sent  # the structure the model needs is intact


async def test_an_edit_that_would_write_a_redaction_placeholder_into_the_page_is_rejected():
    from types import SimpleNamespace

    anchor = '<input name="email" />'
    file_text = f"<span>[email]</span>{anchor}\n"
    hit = SourceHit("a.jsx", 1, 0, "grep", anchor, "")
    bad = {"edits": [{"file": "a.jsx", "anchor_text": anchor, "replacement": '<input name="email" aria-label="[email]" />'}],
           "explanation": "Labels the field.", "confidence": 0.95}

    class C:
        def __init__(self):
            self.messages = self

        async def create(self, **kw):
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(bad))], stop_reason="end_turn",
                                   usage=SimpleNamespace(model_dump=dict))

    a = await author_fix(C(), "m", {"rule": "label"}, hit, "plan", file_text)
    assert not a.valid and "placeholder" in " ".join(a.notes)
    assert PLACEHOLDER.search("[email]") and not PLACEHOLDER.search("[Email]")
