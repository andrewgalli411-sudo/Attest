"""Single source of truth for draft / non-compliance wording."""

DRAFT_BANNER = (
    "Automated first pass — requires expert review. "
    "This draft is not a certification or statement of conformance."
)

import re

# Words the tool must never emit as a conclusion about a product.
BANNED_CLAIMS = re.compile(
    r"\b(non-?compliant|compliant|compliance|complies|certified|certification|conforms? to|fully conforms?)\b",
    re.IGNORECASE,
)


def contains_banned_claim(text: str) -> bool:
    return bool(BANNED_CLAIMS.search(text or ""))
