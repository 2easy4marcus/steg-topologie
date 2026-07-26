# locality_dedup.py
"""
Resolves raw locality text from STEG notices to a stable canonical name.

Pipeline: normalize -> exact match on normalized form -> fuzzy match
(rapidfuzz >= 90%) -> new canonical locality if nothing matches. Matches
are recorded in locality_aliases so the same raw text resolves instantly
next time without re-running the fuzzy scan.
"""

import re
import unicodedata

from rapidfuzz import fuzz, process

from . import db

FUZZY_THRESHOLD = 90
NORMALIZATION_VERSION = "1"
_DIACRITICS_RE = re.compile(r"[\u0610-\u061A\u064B-\u0652\u0670\u06D6-\u06ED]")
_TATWEEL_RE = re.compile(r"ـ")
_ALEF_VARIANTS = "أإآ"


def normalize_locality(text: str) -> str:
    text = unicodedata.normalize("NFC", text.strip())
    text = re.sub(r"\s+", " ", text)
    text = _DIACRITICS_RE.sub("", text)
    text = _TATWEEL_RE.sub("", text)
    for variant in _ALEF_VARIANTS:
        text = text.replace(variant, "ا")
    text = text.replace("ة", "ه")
    return text


def resolve_locality(raw_text: str) -> str:
    existing_alias = db.resolve_alias(raw_text)
    if existing_alias:
        return existing_alias

    normalized = normalize_locality(raw_text)
    existing_names = db.list_locality_names()
    normalized_to_name = {normalize_locality(name): name for name in existing_names}

    if normalized in normalized_to_name:
        canonical = normalized_to_name[normalized]
        if canonical != raw_text:
            db.record_alias(raw_text, canonical)
        return canonical

    if normalized_to_name:
        match = process.extractOne(
            normalized, list(normalized_to_name.keys()), scorer=fuzz.token_sort_ratio,
        )
        if match is not None and match[1] >= FUZZY_THRESHOLD:
            canonical = normalized_to_name[match[0]]
            db.record_alias(raw_text, canonical)
            return canonical

    db.upsert_locality(raw_text)
    return raw_text
