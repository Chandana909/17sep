"""Rail 6: free text is untrusted data. Delimit it; never let it close its own fence."""

from __future__ import annotations

import unicodedata

OPEN = "<<<UNTRUSTED_DATA>>>"
CLOSE = "<<<END_UNTRUSTED_DATA>>>"
_ALLOWED_CONTROL = {"\n", "\t"}


def delimit(text: str) -> str:
    cleaned = "".join(
        ch for ch in text if ch in _ALLOWED_CONTROL or not unicodedata.category(ch).startswith("C")
    )
    cleaned = cleaned.replace("<<<", "< < <").replace(">>>", "> > >")
    return f"{OPEN}\n{cleaned}\n{CLOSE}"
