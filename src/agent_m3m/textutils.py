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


# Markers that flip meaning. The guard trips only on ASYMMETRY (one side has
# a marker, the other has none), so symmetric paraphrases such as
# "must not ..." vs "must never ..." still merge. Tripping never deletes or
# merges anything: it only routes the pair to the LLM reconcile path.
_NEGATION_MARKERS = frozenset({
    "não", "nao", "nunca", "jamais", "not", "never", "don", "sem",
})
_CONTRAST_MARKERS = frozenset({
    "instead", "vez", "mas", "but",
})
_NUMBER_PATTERN = re.compile(r"\d+")

# Minimal antonym seed list (exact-token, swapped-presence check). A miss only
# costs one bounded LLM call; the list stays small on purpose.
_ANTONYM_PAIRS = (
    ("allow", "forbid"),
    ("enable", "disable"),
    ("permit", "deny"),
    ("accept", "reject"),
    ("on", "off"),
    ("true", "false"),
    ("before", "after"),
    ("add", "remove"),
    ("start", "stop"),
)
# Timeframe words: "day" vs "month" flips meaning while keeping Jaccard high.
# Any asymmetric timeframe vocabulary across the two sides trips the guard.
_TIMEFRAME_WORDS = frozenset({
    "day", "days", "daily", "week", "weeks", "weekly",
    "month", "months", "monthly", "year", "years", "yearly",
    "hour", "hours", "hourly", "minute", "minutes", "second", "seconds",
})


def _raw_numbers(text: str) -> set:
    """Digit runs from the raw text (tokenizer drops short numbers)."""
    return set(_NUMBER_PATTERN.findall(text or ""))


def _content_tokens(text: str) -> set:
    """Raw casefolded tokens (no length/stopword filter) for guard checks."""
    return set(_TOKEN_PATTERN.findall((text or "").casefold()))


def contradiction_guard_tripped(a: str, b: str) -> bool:
    """Return True when a near-duplicate pair may be a contradiction.

    Two cheap deterministic checks, both fail-safe (a false trip only costs
    one bounded LLM call; a missed trip keeps today's behavior):
    1. Asymmetric negation/contrast markers: "Use X" vs "Don't use X".
       Swapped antonyms ("allow" vs "forbid") count as asymmetric too.
    2. Divergent raw numbers: "24 hours" vs "48 hours" (the tokenizer drops
       short numbers, so these look identical to Jaccard). Any difference
       trips when both sides carry numbers ("3.12" vs "3.13" share "3").
    """
    tokens_a = _content_tokens(a)
    tokens_b = _content_tokens(b)
    for markers in (_NEGATION_MARKERS, _CONTRAST_MARKERS):
        if bool(tokens_a & markers) != bool(tokens_b & markers):
            return True
    for word_a, word_b in _ANTONYM_PAIRS:
        a_has_a, a_has_b = word_a in tokens_a, word_b in tokens_a
        b_has_a, b_has_b = word_a in tokens_b, word_b in tokens_b
        if (a_has_a and not a_has_b and b_has_b and not b_has_a) or (
            b_has_a and not b_has_b and a_has_b and not a_has_a
        ):
            return True
    numbers_a = _raw_numbers(a)
    numbers_b = _raw_numbers(b)
    if numbers_a and numbers_b and numbers_a != numbers_b:
        return True
    timeframe_a = tokens_a & _TIMEFRAME_WORDS
    timeframe_b = tokens_b & _TIMEFRAME_WORDS
    if timeframe_a - timeframe_b and timeframe_b - timeframe_a:
        return True
    return False
