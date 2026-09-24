from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta

from sklearn.metrics.pairwise import cosine_similarity

from app.config import get_settings
from app.ml.classifier import get_classifier
from app.ml.preprocess import _PHONE_RE, _URL_RE, _USERNAME_RE, normalize_text

logger = logging.getLogger("fraud")


def text_fingerprint(text: str) -> str:
    normalized = normalize_text(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def strip_contacts(text: str) -> str:
    raw = text or ""
    raw = _URL_RE.sub(" ", raw)
    raw = _USERNAME_RE.sub(" ", raw)
    raw = _PHONE_RE.sub(" ", raw)
    return normalize_text(raw)


def similarity_score(approved_text: str, new_text: str) -> float:
    clf = get_classifier()
    if not clf.is_ready:
        a = strip_contacts(approved_text)
        b = strip_contacts(new_text)
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        from difflib import SequenceMatcher
        return SequenceMatcher(None, a, b).ratio()

    vec_a = clf.text_vector(approved_text)
    vec_b = clf.text_vector(new_text)
    return float(cosine_similarity(vec_a, vec_b)[0][0])


def check_publish_allowed(approved_text: str, approved_hash: str, new_text: str) -> tuple[bool, float, str]:
    settings = get_settings()
    new_hash = text_fingerprint(new_text)
    if new_hash == approved_hash:
        return True, 1.0, "hash_match"

    score = similarity_score(approved_text, new_text)
    if score >= settings.fraud_similarity_threshold:
        return True, score, "similarity_ok"

    return False, score, "text_changed"


def subscription_period() -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    return now, now + timedelta(days=30)
