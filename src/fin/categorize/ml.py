"""ML fallback for transactions no rule catches. Local scikit-learn only; nothing leaves the machine.

Text = normalised merchant + description (char 2-5-grams, robust to truncation and accents),
plus one-hot descriptor type / account kind / direction / amount bucket. Logistic regression
on rule- and manually-labelled history (manual labels weighted 3x). Predictions above
`ACCEPT` are applied; below, they are kept as a suggestion for the review queue.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

ACCEPT = 0.7
MIN_PER_CLASS = 3
MIN_TRAIN = 50


@dataclass
class Prediction:
    category_id: int
    confidence: float


def _amount_bucket(cents: pd.Series) -> pd.Series:
    a = (cents.abs() / 100).clip(lower=0.01)
    return pd.cut(np.log10(a), bins=[-3, 0, 1, 1.5, 2, 2.5, 3, 9], labels=False).astype(str)


def features(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({
        "text": (df["merchant"].fillna("") + " | " + df["description"].fillna("")).str.lower(),
        "type": df["type"].fillna("other"),
        "account_kind": df["account_kind"].fillna("current"),
        "direction": np.where(df["amount_cents"] < 0, "out", "in"),
        "bucket": _amount_bucket(df["amount_cents"]),
    }, index=df.index)


def build() -> Pipeline:
    pre = ColumnTransformer([
        ("text", TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), min_df=1, sublinear_tf=True), "text"),
        ("cat", OneHotEncoder(handle_unknown="ignore"), ["type", "account_kind", "direction", "bucket"]),
    ], transformer_weights={"text": 1.0, "cat": 0.3})  # context nudges; text decides
    return Pipeline([("pre", pre), ("clf", LogisticRegression(max_iter=2000, C=4.0))])


def train_and_predict(labelled: pd.DataFrame, unlabelled: pd.DataFrame) -> dict[int, Prediction]:
    """labelled: rows with category_id + source; unlabelled: rows to predict. Index = tx id."""
    if len(labelled) < MIN_TRAIN or unlabelled.empty:
        return {}
    counts = labelled.category_id.value_counts()
    labelled = labelled[labelled.category_id.isin(counts[counts >= MIN_PER_CLASS].index)]
    if labelled.category_id.nunique() < 2:
        return {}
    weights = np.where(labelled.source == "manual", 3.0, 1.0)
    model = build()
    model.fit(features(labelled), labelled.category_id, clf__sample_weight=weights)
    proba = model.predict_proba(features(unlabelled))
    best = proba.argmax(axis=1)
    classes = model.classes_
    return {int(i): Prediction(int(classes[b]), float(proba[k, b])) for k, (i, b) in enumerate(zip(unlabelled.index, best))}


def evaluate(labelled: pd.DataFrame, test_frac: float = 0.2) -> dict:
    """Time-split accuracy: train on the oldest 80%, test on the newest 20% (merchants unseen in
    training are reported separately — that's where the fallback matters)."""
    labelled = labelled.sort_values("booking_date")
    cut = int(len(labelled) * (1 - test_frac))
    train, test = labelled.iloc[:cut], labelled.iloc[cut:]
    preds = train_and_predict(train, test)
    if not preds:
        return {}
    y = test.category_id
    p = pd.Series({i: v.category_id for i, v in preds.items()})
    conf = pd.Series({i: v.confidence for i, v in preds.items()})
    unseen = ~test.merchant.isin(set(train.merchant))
    acc = (p == y)
    return {
        "n_test": len(test), "accuracy": float(acc.mean()),
        "accuracy_unseen_merchants": float(acc[unseen].mean()) if unseen.any() else None, "n_unseen": int(unseen.sum()),
        "coverage_at_accept": float((conf >= ACCEPT).mean()),
        "accuracy_at_accept": float(acc[conf >= ACCEPT].mean()) if (conf >= ACCEPT).any() else None,
    }
