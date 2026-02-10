from __future__ import annotations

import re
from typing import Iterable

_WS_RE = re.compile(r"\s+")
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")

# Values that mean "do not auto-fill; require a human / manual review".
_HUMAN_SENTINELS = {
    "__human__",
    "__human__ ",  # defensive (will be normalized anyway)
    "__ask__",
    "__manual__",
}


def normalize_text(text: str) -> str:
    return _WS_RE.sub(" ", str(text or "")).strip().lower()


def normalize_text_fuzzy(text: str) -> str:
    """Like normalize_text but also normalizes underscores, brackets, hyphens to spaces.
    
    Useful for matching DOM labels that may have IDs embedded (e.g. 'question_35207622002[]').
    """
    t = str(text or "").lower()
    t = re.sub(r"[\[\]_\-\(\)]+", " ", t)
    return _WS_RE.sub(" ", t).strip()


def is_human_sentinel(answer: str) -> bool:
    norm = normalize_text(answer)
    if not norm:
        return True
    if norm in _HUMAN_SENTINELS:
        return True
    return norm.startswith("__human")


def expand_placeholders(text: str, values: dict[str, str]) -> str:
    """
    Replace `{key}` placeholders with values from the provided mapping.
    Unknown placeholders are left untouched.
    """

    def _repl(match: re.Match[str]) -> str:
        key = match.group(1)
        return str(values.get(key, match.group(0)) or match.group(0))

    return _PLACEHOLDER_RE.sub(_repl, str(text or ""))


def match_question_bank(question_text: str, bank: Iterable[tuple[str, str]]) -> str | None:
    """
    Return the first matching answer from the bank.

    Matching rules:
    - Default: case-insensitive substring contains (after whitespace normalization).
    - Regex: patterns prefixed with `re:` are treated as Python regex (case-insensitive).
    - Fuzzy: if no exact match, retry with fuzzy normalization (underscores/brackets → spaces).
    """
    hay = normalize_text(question_text)
    hay_fuzzy = normalize_text_fuzzy(question_text)
    if not hay and not hay_fuzzy:
        return None

    best_answer: str | None = None
    best_score = -1

    bank_list = list(bank or [])

    for pattern, answer in bank_list:
        pat = str(pattern or "").strip()
        if not pat:
            continue

        if pat.lower().startswith("re:"):
            raw = pat[3:].strip()
            if not raw:
                continue
            try:
                if re.search(raw, question_text or "", flags=re.I):
                    score = 10_000 + len(raw)
                    if score > best_score:
                        best_score = score
                        best_answer = str(answer or "")
            except re.error:
                continue
            continue

        needle = normalize_text(pat)
        if needle and needle in hay:
            score = len(needle)
            if score > best_score:
                best_score = score
                best_answer = str(answer or "")

    # Fuzzy fallback: if no match found, retry with fuzzy normalization
    if best_answer is None and hay_fuzzy:
        for pattern, answer in bank_list:
            pat = str(pattern or "").strip()
            if not pat:
                continue
            if pat.lower().startswith("re:"):
                continue
            needle_fuzzy = normalize_text_fuzzy(pat)
            if needle_fuzzy and needle_fuzzy in hay_fuzzy:
                score = len(needle_fuzzy)
                if score > best_score:
                    best_score = score
                    best_answer = str(answer or "")

    return best_answer
