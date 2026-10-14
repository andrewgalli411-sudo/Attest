"""Which preview deployments Attest will audit for a pull request.

The preview address arrives inside a webhook. The webhook itself is authentic (signed by GitHub), but the address in it is
chosen by whoever configured that repository's deployments, so it is still untrusted: without a rule, anyone could install
the app on a repository of their own, announce a "preview" at somebody else's site, and have Attest audit it. So a preview
is accepted only if it is clearly part of the project:

  * on the verified site's own domain (or a subdomain of it), or
  * matching the preview pattern the owner wrote down for the project, or
  * on a hosting platform the operator listed as a place previews live (vercel.app, netlify.app, ...).

Whatever passes is still fetched through the network guard, which refuses private addresses.
"""

from __future__ import annotations

import re

from .netguard import UnsafeAddress, parse_site_url

_PLACEHOLDERS = {"{pr}": r"\d{1,7}", "{sha}": r"[0-9a-f]{7,40}", "{branch}": r"[a-z0-9][a-z0-9-]{0,62}"}


def _pattern(template: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(template):
        for token, rx in _PLACEHOLDERS.items():
            if template.startswith(token, i):
                out.append(rx)
                i += len(token)
                break
        else:
            out.append(re.escape(template[i]))
            i += 1
    return re.compile("".join(out))


def preview_allowed(url: str, *, site_url: str, template: str | None, suffixes: list[str], allow_insecure: bool,
                    ports: tuple[int, ...]) -> tuple[str, str]:
    """Returns (normalised preview url, why it was accepted) or raises UnsafeAddress with the reason it was refused."""
    preview = parse_site_url(url, allow_insecure=allow_insecure, allowed_ports=ports)
    own = parse_site_url(site_url, allow_insecure=True, allowed_ports=(80, 443, 8080, 8443, *ports))
    if preview.host == own.host or preview.host.endswith("." + own.host):
        return preview.url, "on the project's own domain"
    if template and _pattern(template).fullmatch(preview.origin):
        return preview.url, "matches the project's preview pattern"
    if any(preview.host.endswith(s if s.startswith(".") else "." + s) for s in suffixes):
        return preview.url, "on a preview hosting platform"
    raise UnsafeAddress("the preview address is not on this project's domain, its preview pattern, or a known preview platform")
