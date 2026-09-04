"""Shared lightweight text-similarity helpers (deterministic, stdlib only)."""

import re

_STOPWORDS = {
    "a", "as", "ao", "com", "da", "das", "de", "do", "dos", "e", "em",
    "for", "from", "is", "it", "na", "nas", "no", "nos", "o", "os", "para",
    "que", "this", "the", "to", "um", "uma", "use", "used", "usar", "usa",
}

_TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)


def meaningful_tokens(text: str) -> set:
    """Content-bearing tokens for lightweight similarity matching."""
    tokens = _TOKEN_PATTERN.findall(text.casefold())
    return {token for token in tokens if len(token) > 2 and token not in _STOPWORDS}


def jaccard(a: set, b: set) -> float:
    """Token-set overlap in [0, 1]; 1.0 means identical token sets."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
