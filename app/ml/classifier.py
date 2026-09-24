from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np
from sklearn.pipeline import Pipeline

from app.config import get_settings
from app.ml.preprocess import (
    detect_category_hint,
    detect_rule_block,
    is_incomplete_text,
    normalize_text,
)

# Тарифы читаются из БД (ТЗ этапа 2, 6.6): администратор правит цены из панели.
# Пока таблица не прочитана, сервис отдаёт значения из app/ml/categories.py.
from app.services.tariffs import get as get_category

logger = logging.getLogger("ml.classifier")


@dataclass
class ClassificationResult:
    category_code: str
    label: str
    price_per_chat: int
    confidence: float
    needs_review: bool
    blocked: bool
    reason: str = ""


class TariffClassifier:
    def __init__(self) -> None:
        self._pipeline: Pipeline | None = None
        self._meta: dict = {}
        self._model_path = Path(get_settings().ml_model_path)
        self._threshold = get_settings().ml_confidence_threshold
        self.reload()

    def reload(self) -> bool:
        if not self._model_path.exists():
            logger.warning("Model file not found: %s", self._model_path)
            self._pipeline = None
            return False
        self._pipeline = joblib.load(self._model_path)
        meta_path = self._model_path.with_suffix(".json")
        if meta_path.exists():
            self._meta = json.loads(meta_path.read_text(encoding="utf-8"))
        logger.info("ML model loaded (%s samples)", self._meta.get("samples"))
        return True

    @property
    def is_ready(self) -> bool:
        return self._pipeline is not None

    @property
    def meta(self) -> dict:
        return dict(self._meta)

    def classify(self, text: str) -> ClassificationResult:
        rule = detect_rule_block(text)
        if rule:
            cat = get_category(rule)
            return ClassificationResult(
                category_code=rule,
                label=cat.label,
                price_per_chat=cat.price_per_chat,
                confidence=1.0,
                needs_review=False,
                blocked=cat.blocked,
                reason="rule_block",
            )

        if is_incomplete_text(text):
            cat = get_category("INCOMPLETE")
            return ClassificationResult(
                category_code="INCOMPLETE",
                label=cat.label,
                price_per_chat=0,
                confidence=1.0,
                needs_review=False,
                blocked=False,
                reason="incomplete",
            )

        if not self._pipeline:
            cat = get_category("OTHER")
            return ClassificationResult(
                category_code="OTHER",
                label=cat.label,
                price_per_chat=cat.price_per_chat,
                confidence=0.0,
                needs_review=True,
                blocked=False,
                reason="model_missing",
            )

        normalized = normalize_text(text)
        proba = self._pipeline.predict_proba([normalized])[0]
        classes = list(self._pipeline.classes_)
        idx = int(np.argmax(proba))
        code = classes[idx]
        confidence = float(proba[idx])

        if confidence < self._threshold:
            hint = detect_category_hint(text)
            if hint:
                code = hint
                reason = "keyword_hint"
            else:
                code = "OTHER"
                reason = "low_confidence"
        else:
            reason = "ml"

        cat = get_category(code)
        return ClassificationResult(
            category_code=code,
            label=cat.label,
            price_per_chat=cat.price_per_chat,
            confidence=confidence,
            needs_review=cat.needs_review or code == "OTHER",
            blocked=cat.blocked,
            reason=reason,
        )

    def text_vector(self, text: str) -> np.ndarray:
        if not self._pipeline:
            raise RuntimeError("Model not loaded")
        tfidf = self._pipeline.named_steps["tfidf"]
        return tfidf.transform([normalize_text(text)])


_classifier: TariffClassifier | None = None


def get_classifier() -> TariffClassifier:
    global _classifier
    if _classifier is None:
        _classifier = TariffClassifier()
    return _classifier
