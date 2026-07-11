"""§7 isolation canary (extends test_attribution's
test_training_paths_never_read_attribution_tables to the league).

The Trader League is a discretion/meta layer: trader predictions, reviews,
method versions, and divergence must NEVER feed back into the clean,
backtestable base models -- otherwise human-visible outcomes silently
re-enter training data (CLAUDE.md section 7). Guard against a future edit
wiring any league table (or the league package) into a training/inference
path.
"""
from __future__ import annotations

from pathlib import Path

MODELS_DIR = Path(__file__).resolve().parents[2] / "src" / "stockmoney" / "models"

# Every table keyed off trader_id / the league's outputs.
LEAGUE_TABLES = [
    "trader_predictions",
    "trader_review_log",
    "trader_divergence_log",
    "trader_method_versions",
    "trader_method_proposals",
]

# The model files that make up the training/inference path.
TRAINING_FILES = [
    "feature_matrix.py",
    "walk_forward.py",
    "regime.py",
    "direction.py",
    "production.py",
    "ev_gate.py",
    "kelly.py",
]


def test_training_paths_never_read_league_tables():
    for name in TRAINING_FILES:
        path = MODELS_DIR / name
        if not path.exists():
            continue
        text = path.read_text()
        for table in LEAGUE_TABLES:
            assert table not in text, f"{name} must not reference {table}"


def test_models_never_import_the_league_package():
    """The dependency arrow points league -> models, never the reverse."""
    for path in MODELS_DIR.glob("*.py"):
        text = path.read_text()
        assert "stockmoney.league" not in text, f"{path.name} must not import stockmoney.league"
        assert "trader_predictions" not in text, f"{path.name} must not import trader_predictions"
