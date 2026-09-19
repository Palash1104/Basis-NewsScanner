"""Check every symbol in config/assets.yaml against Yahoo Finance (SPEC §8).

Downloads 5 days of daily prices per symbol and reports any that fail (no data, stale data or
errors), plus symbols whose Yahoo name, type, currency or exchange looks off. Results are stored
in the ticker_checks table (the app warns about unvalidated symbols) and written to
data/ticker_report.md. Failing symbols are never replaced automatically.

Same as `uv run newsdesk validate-tickers`.

Usage:
    uv run python scripts/validate_tickers.py
"""

import sys

from app.cli import app

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    app(["validate-tickers"])
