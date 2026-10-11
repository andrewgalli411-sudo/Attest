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
from collections import Counter
from dataclasses import dataclass, field

from .source_map import opening_tag_end


class UnsafeEdit(ValueError):
    pass


@dataclass(frozen=True)
class Policy:
    attrs: frozenset[str]  # attributes this rule's fix may add or change on the element
    new_elements: frozenset[str] = field(default_factory=frozenset)  # elements the fix may add next to it
    retag_to_label: bool = False  # may turn a sibling text element (span/div/p/...) into a <label> bound to the field
    description: str = ""


def _p(attrs: str, new: str = "", desc: str = "", retag: bool = False) -> Policy:
    return Policy(frozenset(attrs.split()), frozenset(new.split()), retag, desc)


POLICY: dict[str, Policy] = {
    "label": _p("aria-label aria-labelledby id for htmlFor", "label", "name a form field", retag=True),
    "select-name": _p("aria-label aria-labelledby id for htmlFor", "label", "name a select", retag=True),
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


RETAG_FROM = frozenset({"span", "div", "p", "strong", "b", "em", "i", "small"})  # plain text containers only
NEW_ELEMENT_EXTRA_ATTRS = frozenset({"class", "className"})  # styling only, so a new <label> can match its neighbours


@dataclass
class _Tag:
    kind: str  # "open" | "close"
    name: str
    attrs: dict[str, str] = field(default_factory=dict)
    selfclose: bool = False


def _scan(s: str) -> tuple[list[_Tag], list[str]]:
    """Tags in order, and the text between them (JSX {expressions} count as text)."""
    tags: list[_Tag] = []
    texts: list[str] = []
    buf: list[str] = []
    i = 0

    def flush() -> None:
        t = "".join(buf).strip()
        if t:
            texts.append(t)
        buf.clear()

    while i < len(s):
        if s[i] == "<" and i + 1 < len(s) and (s[i + 1].isalpha() or (s[i + 1] == "/" and i + 2 < len(s) and s[i + 2].isalpha())):
            flush()
            if s[i + 1] == "/":
                m = re.match(r"</\s*([A-Za-z][\w.:-]*)\s*>", s[i:])
                if not m:
                    raise UnsafeEdit("malformed closing tag")
                tags.append(_Tag("close", m.group(1).lower()))
                i += m.end()
            else:
                end = opening_tag_end(s, i)
                if end is None:
                    raise UnsafeEdit("unterminated tag")
                text = s[i:end]
                name = re.match(r"<\s*([A-Za-z][\w.:-]*)", text).group(1).lower()
                tags.append(_Tag("open", name, parse_attrs(text), text.rstrip().endswith("/>")))
                i = end
        else:
            buf.append(s[i])
            i += 1
    flush()
    return tags, texts


def _is_subsequence(needle: list[str], hay: list[str]) -> bool:
    it = iter(hay)
    return all(any(n == h for h in it) for n in needle)


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        return v[1:-1]
    if v.startswith("{") and v.endswith("}"):
        return _unquote(v[1:-1])
    return v


def check_edit_shape(rule_id: str, anchor: str, replacement: str) -> None:
    """Raise UnsafeEdit unless this edit is within the policy for `rule_id`.

    Every element of the anchor must survive (same tag, or the one allowed retag), may only gain/alter the
    attributes the rule allows, and keeps all of its text. New elements must be on the rule's list, bound,
    and carry only allowed attributes."""
    pol = POLICY.get(rule_id)
    if pol is None:
        raise UnsafeEdit(f"rule {rule_id!r} has no edit policy, so it cannot be auto-fixed")

    for pat, why in DENY:
        if len(pat.findall(replacement)) > len(pat.findall(anchor)):
            raise UnsafeEdit(f"edit {why}")

    a_tags, a_texts = _scan(anchor)
    r_tags, r_texts = _scan(replacement)
    A = [t for t in a_tags if t.kind == "open"]
    R = [t for t in r_tags if t.kind == "open"]
    if not A:
        raise UnsafeEdit("anchor_text does not start an element")

    def compatible(a: _Tag, r: _Tag) -> bool:
        return a.name == r.name or (pol.retag_to_label and a.name in RETAG_FROM and r.name == "label")

    matched: dict[int, int] = {}
    new: list[int] = []
    ai = 0
    for ri, r in enumerate(R):
        if ai < len(A) and compatible(A[ai], r):
            matched[ai] = ri
            ai += 1
        else:
            new.append(ri)
    if ai != len(A):
        raise UnsafeEdit("edit removes or renames an element")

    allowed_attrs = pol.attrs | {_norm(a) for a in pol.attrs}

    def attr_ok(name: str) -> bool:
        return name in allowed_attrs or _norm(name) in allowed_attrs

    for a_i, r_i in matched.items():
        a, r = A[a_i], R[r_i]
        for name in a.attrs:
            if name not in r.attrs:
                raise UnsafeEdit(f"edit removes attribute {name!r}")
        for name, val in r.attrs.items():
            if name not in a.attrs:
                if name.startswith("..."):
                    raise UnsafeEdit("edit adds a spread attribute")
                if not attr_ok(name):
                    raise UnsafeEdit(f"edit adds attribute {name!r}, not allowed for {rule_id!r}")
            elif a.attrs[name] != val and not attr_ok(name):
                raise UnsafeEdit(f"edit changes attribute {name!r}, not allowed for {rule_id!r}")
        if a.selfclose != r.selfclose:
            raise UnsafeEdit("edit changes whether an element is self-closing")

    for r_i in new:
        r = R[r_i]
        if r.name not in pol.new_elements:
            raise UnsafeEdit(f"edit adds a <{r.name}> element, which the {rule_id!r} policy does not allow")
        for name in r.attrs:
            if name.startswith("...") or not (attr_ok(name) or name in NEW_ELEMENT_EXTRA_ATTRS):
                raise UnsafeEdit(f"new <{r.name}> carries attribute {name!r}, not allowed for {rule_id!r}")

    # Closing tags: an anchor often holds only an opening tag (its closing tag is elsewhere in the file), so
    # expect exactly the anchor's own closing tags - renamed where an element was retagged - plus the closing
    # tag of every new element, which must be inside the replacement.
    expected = Counter(t.name for t in a_tags if t.kind == "close")
    for a_i, r_i in matched.items():
        if A[a_i].name != R[r_i].name and expected[A[a_i].name] > 0:
            expected[A[a_i].name] -= 1
            expected[R[r_i].name] += 1
    expected += Counter(R[i].name for i in new if not R[i].selfclose)
    if +expected != Counter(t.name for t in r_tags if t.kind == "close"):
        raise UnsafeEdit("edit leaves tags unbalanced or changes closing tags")

    # existing text is preserved; new text may appear only because a new element carries it
    if new:
        if not _is_subsequence(a_texts, r_texts):
            raise UnsafeEdit("edit changes or removes existing text")
    elif r_texts != a_texts:
        raise UnsafeEdit("edit changes or adds text")

    # a <label> only counts if it is bound to a field that exists in the same edit
    labels = [R[i] for i in new if R[i].name == "label"] + [R[ri] for ai2, ri in matched.items() if R[ri].name == "label" and A[ai2].name != "label"]
    ids = {_unquote(t.attrs["id"]) for t in R if "id" in t.attrs}
    for lab in labels:
        target = lab.attrs.get("htmlFor") or lab.attrs.get("for")
        if target is None:
            raise UnsafeEdit("a <label> must be bound with htmlFor/for")
        if _unquote(target) not in ids:
            raise UnsafeEdit("a <label> points at an id that does not exist in the edit")
