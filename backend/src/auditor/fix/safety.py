"""Edit policy: what a model-written edit is allowed to *do*, enforced in code.

Everything the model sees came from a web page, so it is untrusted input. A hostile page could try to steer
the edit ("add this script", "add an onfocus handler"). Defence in depth, in order:
  1. the edit must stay inside the single file the source mapper identified (verify.py);
  2. THIS module: per rule, an edit may only add/alter the specific attributes (and wrap-in elements) that fix
     that rule, may never remove or change anything else on the element, and may never add scripts, event
     handlers, URLs, or code-execution constructs;
  3. the repo's own checks must pass; 4. the re-test must pass; 5. a person merges the pull request.
A rule with no policy cannot be auto-fixed, however confident the model is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .source_map import opening_tag_end


class UnsafeEdit(ValueError):
    pass


@dataclass(frozen=True)
class Policy:
    attrs: frozenset[str]  # attributes this rule's fix may add or change on the element
    new_elements: frozenset[str] = field(default_factory=frozenset)  # elements the fix may add next to it
    description: str = ""


def _p(attrs: str, new: str = "", desc: str = "") -> Policy:
    return Policy(frozenset(attrs.split()), frozenset(new.split()), desc)


POLICY: dict[str, Policy] = {
    "label": _p("aria-label aria-labelledby id for htmlFor", "label", "name a form field"),
    "select-name": _p("aria-label aria-labelledby id for htmlFor", "label", "name a select"),
    "html-has-lang": _p("lang", "", "declare the page language"),
    "html-lang-valid": _p("lang", "", "correct the page language"),
    "button-name": _p("aria-label aria-labelledby title", "", "name a button"),
    "link-name": _p("aria-label aria-labelledby title", "", "name a link"),
    "aria-input-field-name": _p("aria-label aria-labelledby", "", "name an ARIA input"),
    "aria-toggle-field-name": _p("aria-label aria-labelledby", "", "name an ARIA toggle"),
    "aria-command-name": _p("aria-label aria-labelledby", "", "name an ARIA command"),
}

# Control characters and Unicode bidi overrides/isolates ("Trojan Source"): invisible in review, so never allowed.
# Built from code points (not written as literals or escapes) so that no formatter can expand them into raw
# invisible characters inside this file.
_INVISIBLE = "[" + "".join(
    chr(c) for r in (range(0x09), (0x0B, 0x0C), range(0x0E, 0x20), range(0x202A, 0x202F), range(0x2066, 0x206A))
    for c in r
) + "]"

# Constructs a fix for any of the rules above has no business adding. Counted: a pre-existing occurrence in
# the anchor (e.g. an existing onClick on a button) is fine; adding one is not.
DENY: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"<\s*(script|iframe|object|embed|style|link|meta|base|svg|img|video|audio|source|form|input\s+type\s*=\s*[\"']?hidden)\b", re.IGNORECASE),
     "adds a disallowed element"),
    (re.compile(r"\bon[a-z]{3,}\s*=", re.IGNORECASE), "adds an event handler"),
    # Vue (@click, v-on:), Alpine (x-on), Svelte (on:click), Angular ((click), [innerHTML]) handler/HTML bindings
    (re.compile(r"(?:^|[\s<])(?:@[\w:.-]+|v-on[\w:.-]*|v-html|x-on[\w:.-]*|x-html|x-init|on:[\w-]+|\([\w.-]+\)|\[innerHTML\]|\[outerHTML\]|\[srcdoc\])\s*="),
     "adds a framework event/HTML binding"),
    (re.compile(r"dangerouslySetInnerHTML|innerHTML|outerHTML|srcdoc", re.IGNORECASE), "adds raw-HTML injection"),
    (re.compile(r"javascript\s*:|data\s*:\s*text/html|vbscript\s*:", re.IGNORECASE), "adds a script URL"),
    (re.compile(r"https?://|//[a-z0-9.-]+\.[a-z]{2,}", re.IGNORECASE), "adds a URL"),
    (re.compile(r"\b(eval|Function|setTimeout|setInterval|fetch|XMLHttpRequest|import|require|WebSocket|sendBeacon)\s*\(", re.IGNORECASE),
     "adds code execution or network access"),
    (re.compile(r"\b(document|window|localStorage|sessionStorage|cookie|globalThis|process)\s*[.\[]", re.IGNORECASE),
     "touches browser globals"),
    (re.compile(r"<!--|-->|<!\[CDATA\["), "adds comment/CDATA markup"),
    (re.compile(_INVISIBLE), "adds control or bidi characters"),
]

_TAG = re.compile(r"</?\s*([A-Za-z][\w.:-]*)")
_NAME = re.compile(r"[A-Za-z_:@$][\w:.@$-]*")
JSX_ALIAS = {"htmlFor": "for", "className": "class"}


def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i].isspace():
        i += 1
    return i


def _balanced(s: str, i: int) -> int:
    """Index just past the {...} starting at s[i], respecting strings and nesting."""
    depth, quote = 0, None
    while i < len(s):
        c = s[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'`":
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise UnsafeEdit("unbalanced braces in tag")


def parse_attrs(tag: str) -> dict[str, str]:
    """Attributes of a single opening tag. Spread props ({...x}) are kept under '...<n>' keys."""
    m = re.match(r"<\s*[A-Za-z][\w.:-]*", tag)
    if not m:
        raise UnsafeEdit("not an opening tag")
    i, out, n_spread = m.end(), {}, 0
    while True:
        i = _skip_ws(tag, i)
        if i >= len(tag) or tag[i] in ">/":
            break
        if tag[i] == "{":
            j = _balanced(tag, i)
            out[f"...{n_spread}"] = tag[i:j]
            n_spread += 1
            i = j
            continue
        nm = _NAME.match(tag, i)
        if not nm:
            raise UnsafeEdit(f"cannot parse attribute near {tag[i:i + 12]!r}")
        name, i = nm.group(0), nm.end()
        j = _skip_ws(tag, i)
        if j < len(tag) and tag[j] == "=":
            j = _skip_ws(tag, j + 1)
            if j >= len(tag):
                raise UnsafeEdit("attribute without a value")
            if tag[j] in "\"'":
                k = tag.find(tag[j], j + 1)
                if k < 0:
                    raise UnsafeEdit("unterminated attribute value")
                out[name], i = tag[j:k + 1], k + 1
            elif tag[j] == "{":
                k = _balanced(tag, j)
                out[name], i = tag[j:k], k
            else:
                k = j
                while k < len(tag) and not tag[k].isspace() and tag[k] not in ">/":
                    k += 1
                out[name], i = tag[j:k], k
        else:
            out[name] = "true"  # boolean attribute
    return out


def _norm(name: str) -> str:
    return JSX_ALIAS.get(name, name)


def _first_tag(text: str, tag_name: str) -> str | None:
    m = re.search(rf"<\s*{re.escape(tag_name)}(?=[\s/>])", text)
    if not m:
        return None
    end = opening_tag_end(text, m.start())
    return text[m.start():end] if end else None


def check_edit_shape(rule_id: str, anchor: str, replacement: str) -> None:
    """Raise UnsafeEdit unless this edit is within the policy for `rule_id`."""
    pol = POLICY.get(rule_id)
    if pol is None:
        raise UnsafeEdit(f"rule {rule_id!r} has no edit policy, so it cannot be auto-fixed")

    for pat, why in DENY:
        if len(pat.findall(replacement)) > len(pat.findall(anchor)):
            raise UnsafeEdit(f"edit {why}")

    anchor_tags = {t.lower() for t in _TAG.findall(anchor)}
    repl_tags = [t for t in _TAG.findall(replacement)]
    allowed = anchor_tags | {t.lower() for t in pol.new_elements}
    for t in repl_tags:
        if t.lower() not in allowed:
            raise UnsafeEdit(f"edit adds a <{t}> element, which the {rule_id!r} policy does not allow")

    first = _TAG.search(anchor)
    if first is None:
        raise UnsafeEdit("anchor_text does not start an element")
    primary = first.group(1)
    old_tag, new_tag = _first_tag(anchor, primary), _first_tag(replacement, primary)
    if old_tag is None:
        raise UnsafeEdit("could not read the anchor element")
    if new_tag is None:
        raise UnsafeEdit(f"edit removes or renames the <{primary}> element")

    old, new = parse_attrs(old_tag), parse_attrs(new_tag)
    allowed_attrs = pol.attrs | {_norm(a) for a in pol.attrs}
    for name in old:
        if name not in new:
            raise UnsafeEdit(f"edit removes attribute {name!r}")
    for name, val in new.items():
        if name not in old:
            if name.startswith("..."):
                raise UnsafeEdit("edit adds a spread attribute")
            if name not in allowed_attrs and _norm(name) not in allowed_attrs:
                raise UnsafeEdit(f"edit adds attribute {name!r}, not allowed for {rule_id!r}")
        elif old[name] != val and name not in allowed_attrs and _norm(name) not in allowed_attrs:
            raise UnsafeEdit(f"edit changes attribute {name!r}, not allowed for {rule_id!r}")
