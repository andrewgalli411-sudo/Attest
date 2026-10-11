import pytest

from auditor.fix.safety import POLICY, UnsafeEdit, check_edit_shape, parse_attrs
from auditor.fix.verify import EditError, apply_and_verify
from auditor.triage.schema import SAFE_RULES

INPUT = '<input name="email" type="email" autoComplete="email" />'
BTN = '<button className="icon-btn" type="button" onClick={open}>'
WITH_ID = INPUT.replace("<input ", '<input id="email" ')
HTML = "<html>"


def ok(rule, anchor, replacement):
    check_edit_shape(rule, anchor, replacement)


def bad(rule, anchor, replacement, match):
    with pytest.raises(UnsafeEdit, match=match):
        check_edit_shape(rule, anchor, replacement)


# ---------------- what legitimate fixes look like: all allowed ----------------
@pytest.mark.parametrize("rule,anchor,repl", [
    ("label", INPUT, INPUT.replace("/>", 'aria-label="Email address" />')),
    ("label", INPUT, INPUT.replace("<input ", '<input id="email" ').replace("/>", 'aria-labelledby="l" />')),
    ("label", INPUT, '<label htmlFor="email">Email address</label>\n' + WITH_ID),
    ("html-has-lang", HTML, '<html lang="en">'),
    ("html-lang-valid", '<html lang="english">', '<html lang="en">'),
    ("button-name", BTN, BTN.replace(">", ' aria-label="Notifications">')),  # existing onClick is preserved, fine
    ("link-name", '<a href="/x" className="c">', '<a href="/x" className="c" title="Home">'),
    ("label", INPUT, INPUT.replace("/>", "aria-label={t('email')} />")),  # i18n call in a value is normal
])
def test_legitimate_fixes_are_allowed(rule, anchor, repl):
    ok(rule, anchor, repl)


# ---------------- what an attacker would try: all blocked ----------------
@pytest.mark.parametrize("repl,match", [
    (INPUT.replace("/>", 'aria-label="x" onFocus={steal} />'), "event handler"),
    (INPUT.replace("/>", 'aria-label="x" onfocus="alert(1)" />'), "event handler"),
    (INPUT + '<script src="/x.js"></script>', "disallowed element"),
    (INPUT.replace("/>", 'aria-label="x" />') + "<iframe src=a />", "disallowed element"),
    (INPUT.replace("/>", 'aria-label="x" dangerouslySetInnerHTML={{__html: x}} />'), "raw-HTML"),
    (INPUT.replace("/>", 'aria-label="javascript:alert(1)" />'), "script URL"),
    (INPUT.replace("/>", 'aria-label="https://evil.example/collect" />'), "URL"),
    (INPUT.replace("/>", 'aria-label={eval("x")} />'), "code execution"),
    (INPUT.replace("/>", 'aria-label={fetch("x")} />'), "code execution"),
    (INPUT.replace("/>", "aria-label={document.cookie} />"), "browser globals"),
    (INPUT.replace("/>", "aria-label={localStorage.token} />"), "browser globals"),
    (INPUT.replace("/>", 'aria-label="x" /><!-- hidden -->'), "comment"),
    (INPUT.replace("/>", 'aria-label="x' + chr(0x202E) + '" />'), "control or bidi"),
    (INPUT.replace("/>", 'aria-label="x' + chr(7) + '" />'), "control or bidi"),
    (INPUT.replace("/>", 'aria-label="x' + chr(0x2066) + '" />'), "control or bidi"),
    (INPUT.replace("/>", 'aria-label="x" /><a href="x">y</a>'), "does not allow"),
    (INPUT.replace('type="email"', 'type="password"'), "changes attribute 'type'"),
    (INPUT.replace('name="email"', 'name="other"').replace("/>", 'aria-label="x" />'), "changes attribute 'name'"),
    (INPUT.replace('autoComplete="email" ', "").replace("/>", 'aria-label="x" />'), "removes attribute"),
    (INPUT.replace("/>", 'aria-label="x" disabled />'), "adds attribute 'disabled'"),
    (INPUT.replace("/>", 'aria-label="x" {...stolen} />'), "spread"),
    (INPUT.replace("<input", "<textarea"), "removes or renames|does not allow"),
    ("", "removes or renames"),
])
def test_attacks_and_overreach_are_blocked_for_label(repl, match):
    bad("label", INPUT, repl, match)


@pytest.mark.parametrize("binding", ['@click="steal()"', '@mouseover="x"', 'v-on:focus="x"', 'v-html="payload"', 'x-on:click="x"',
                                      'x-html="p"', 'x-init="boot()"', 'on:click={steal}', '(click)="steal()"', '[innerHTML]="p"'])
def test_other_frameworks_handler_and_html_bindings_are_blocked(binding):
    bad("label", INPUT, INPUT.replace("/>", f'aria-label="x" {binding} />'), "framework event/HTML binding|adds attribute")


def test_each_rule_only_gets_its_own_attributes():
    bad("html-has-lang", HTML, '<html lang="en" id="x">', "adds attribute 'id'")
    bad("html-has-lang", HTML, '<html lang="en" dir="rtl">', "adds attribute 'dir'")
    bad("button-name", BTN, BTN.replace(">", ' id="x" aria-label="y">'), "adds attribute 'id'")
    bad("link-name", '<a href="/x">', '<a href="/evil" aria-label="y">', "changes attribute 'href'")
    bad("html-has-lang", HTML, '<html lang="en"><label>x</label>', "does not allow")


def test_preexisting_handlers_are_not_blamed_but_new_ones_are():
    ok("button-name", BTN, BTN.replace(">", ' aria-label="n">'))
    bad("button-name", BTN, BTN.replace(">", ' aria-label="n" onMouseOver={x}>'), "event handler")


def test_rules_without_a_policy_cannot_be_auto_fixed():
    bad("heading-order", "<h3>", "<h2>", "no edit policy")
    bad("color-contrast", "<p>", "<p>", "no edit policy")
    bad("", "<p>", "<p>", "no edit policy")


def test_every_rule_the_gate_can_approve_has_an_edit_policy():
    assert SAFE_RULES <= set(POLICY), f"gate allows rules with no policy: {SAFE_RULES - set(POLICY)}"


def test_deny_patterns_do_not_flag_ordinary_aria_values():
    for label in ("Email address", "Search the site", "Close dialog", "Naviguer vers l'accueil", "検索", "Price (USD)"):
        ok("label", INPUT, INPUT.replace("/>", f'aria-label="{label}" />'))


# ---------------- the attribute parser itself ----------------
@pytest.mark.parametrize("tag,expected", [
    ('<input name="a" disabled />', {"name": '"a"', "disabled": "true"}),
    ("<a href='x' onClick={() => go({a: 1})}>", {"href": "'x'", "onClick": "{() => go({a: 1})}"}),
    ('<div {...props} id="x">', {"...0": "{...props}", "id": '"x"'}),
    ("<input\n  name=\"e\"\n  type='t'\n/>", {"name": '"e"', "type": "'t'"}),
    ('<a title="a > b" href="/">', {"title": '"a > b"', "href": '"/"'}),
])
def test_parse_attrs(tag, expected):
    assert parse_attrs(tag) == expected


@pytest.mark.parametrize("tag", ['<input name="a', "<input {", "<input a=", "<input =x>"])
def test_parse_attrs_refuses_malformed_tags(tag):
    with pytest.raises(UnsafeEdit):
        parse_attrs(tag)


# ---------------- enforced where it matters: at apply time, nothing is written ----------------
def test_apply_and_verify_rejects_unsafe_edits_and_leaves_the_file_untouched(tmp_path):
    import subprocess
    (tmp_path / "src").mkdir()
    f = tmp_path / "src" / "App.jsx"
    f.write_text(f"<div>\n  {INPUT}\n</div>\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    evil = {"file": "src/App.jsx", "anchor_text": INPUT, "replacement": INPUT.replace("/>", 'aria-label="x" onFocus={steal} />')}
    r = apply_and_verify(tmp_path, [evil], {"src/App.jsx"}, [], rule_id="label")
    assert not r.ok and "unsafe edit rejected" in r.error and "event handler" in r.error
    assert f.read_text() == f"<div>\n  {INPUT}\n</div>\n"
    good = {**evil, "replacement": INPUT.replace("/>", 'aria-label="Email" />')}
    assert apply_and_verify(tmp_path, [good], {"src/App.jsx"}, [], rule_id="label").ok
    with pytest.raises(EditError):
        from auditor.fix.verify import apply_edits
        apply_edits(tmp_path, [good], {"src/App.jsx"}, rule_id="heading-order")



# ---------------- multi-element anchors (found by running the real model) ----------------
SPAN = '<span className="field-label">Email address</span>'
PAIR = SPAN + "\n          " + INPUT
LABELLED_INPUT = INPUT.replace("<input ", '<input id="email" ')
GOOD_RETAG = '<label htmlFor="email" className="field-label">Email address</label>\n          ' + LABELLED_INPUT
FALLBACK = ('<span id="email-label" className="field-label">Email address</span>\n          '
            + INPUT.replace("/>", 'aria-labelledby="email-label" />'))
NAMED = 'aria-label="x" />'


@pytest.mark.parametrize("repl", [GOOD_RETAG, FALLBACK])
def test_the_edits_a_real_model_proposes_for_a_label_are_allowed(repl):
    ok("label", PAIR, repl)


@pytest.mark.parametrize("repl,match", [
    # retag without binding / binding to nothing
    (GOOD_RETAG.replace(' htmlFor="email"', ""), "must be bound"),
    (GOOD_RETAG.replace('htmlFor="email"', 'htmlFor="nope"'), "does not exist"),
    # retag to something that is not a label, or from something that is not a text container
    (GOOD_RETAG.replace("<label", "<button").replace("</label>", "</button>"), "removes or renames|does not allow"),
    ('<a href="x">Email address</a>\n' + LABELLED_INPUT, "removes or renames|does not allow"),
    # the SECOND element in the anchor must obey the same rules as the first (the gap the real run exposed)
    (SPAN + "\n" + INPUT.replace('type="email"', 'type="password"').replace("/>", NAMED), "changes attribute 'type'"),
    (SPAN + "\n" + INPUT.replace('name="email"', 'name="x"').replace("/>", NAMED), "changes attribute 'name'"),
    (SPAN + "\n" + INPUT.replace("/>", 'aria-label="x" disabled />'), "adds attribute 'disabled'"),
    (SPAN + "\n" + INPUT.replace("/>", 'aria-label="x" onFocus={steal} />'), "event handler"),
    (SPAN + "\n" + INPUT.replace('autoComplete="email" ', "").replace("/>", NAMED), "removes attribute"),
    # visible text is part of the page: it may not be reworded, removed, or added to
    (PAIR.replace("Email address", "Password").replace("/>", NAMED), "changes or removes existing text|changes or adds text"),
    ('<span className="field-label"></span>\n' + INPUT.replace("/>", NAMED), "changes or removes existing text|changes or adds text"),
    (PAIR.replace("</span>", " (required)</span>").replace("/>", NAMED), "changes or removes existing text|changes or adds text"),
    # structure: dropping an element, or leaving tags unbalanced
    (INPUT.replace("/>", NAMED), "removes or renames"),
    (GOOD_RETAG.replace("</label>", "</span>"), "unbalanced"),
])
def test_multi_element_edits_are_checked_element_by_element(repl, match):
    bad("label", PAIR, repl, match)


def test_a_new_label_may_carry_only_styling_never_arbitrary_attributes():
    ok("label", INPUT, '<label htmlFor="email" className="x">Email address</label>\n' + LABELLED_INPUT)
    bad("label", INPUT, '<label htmlFor="email" style={{display: "none"}}>Email address</label>\n' + LABELLED_INPUT, "carries attribute")
    bad("label", INPUT, '<label htmlFor="email" tabIndex="0">Email address</label>\n' + LABELLED_INPUT, "carries attribute")


def test_retagging_is_only_for_rules_that_name_form_fields():
    bad("button-name", "<span>Go</span>\n" + BTN, '<label htmlFor="x">Go</label>\n' + BTN.replace(">", ' aria-label="Go">'),
        "removes or renames|does not allow")
    bad("html-has-lang", "<html>", '<label htmlFor="x">y</label><html lang="en">', "removes or renames|does not allow")
