"""Adding and removing watchlist entries from the BASIS page (user, 2026-10-16).

config/watchlist.yaml is written by hand too, and its comments record why each alias is
there, so it is edited as text, never re-dumped: a new stock is a block appended after the
last stock, a removal takes out one entry's lines. Every edit is then checked the way the
scanner will read it - parsed and validated, with exactly the one entry added or gone - before
anything is written. The previous file is kept in data/backups/, and the new one replaces it
in one step (a temporary file, then a rename), so a reader never sees half a file.

An edit names the version of the file the page was showing. If the file changed since (an
edit by hand, or another tab), the edit is refused rather than applied to something the user
didn't see.
"""

import hashlib
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from app.config import ROOT_DIR, AssetConfig, WatchItem, WatchlistFile, watchlist_from_data

BACKUP_DIR = ROOT_DIR / "data" / "backups"
BACKUPS_KEPT = 20
_ENTRY = re.compile(r"^  - ")  # a top-level item of `watchlist:`, as the file is written
_TOP_LEVEL = re.compile(r"^\S")


class EditRefused(Exception):
    """The edit was not made; the message says why, in words for the page."""


@dataclass(frozen=True)
class Snapshot:
    text: str
    watchlist: WatchlistFile

    @property
    def version(self) -> str:
        return version_of(self.text)


def version_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def read(path: Path, assets: Sequence[AssetConfig] | None = None) -> Snapshot:
    text = path.read_text(encoding="utf-8")
    return Snapshot(text, _validate(text, assets))


def _validate(text: str, assets: Sequence[AssetConfig] | None) -> WatchlistFile:
    return watchlist_from_data(yaml.safe_load(text), assets)


def _q(value: str) -> str:
    """A YAML scalar that reads back as exactly `value`."""
    return json.dumps(value, ensure_ascii=False)


def _flow(values: Sequence[str]) -> str:
    return "[" + ", ".join(_q(v) for v in values) + "]"


def stock_block(item: WatchItem, note: str) -> str:
    """The YAML lines for a stock, in the file's own layout."""
    lines = [
        "  - type: stock",
        f"    symbol: {_q(item.symbol)}",
    ]
    if item.group:
        lines.append(f"    group: {_q(item.group)}")
    lines.append(f"    name: {_q(item.name or item.symbol)}")
    if item.nse_symbol:
        lines.append(f"    nse_symbol: {_q(item.nse_symbol)}")
    if item.nse_name:
        lines.append(f"    nse_name: {_q(item.nse_name)}")
    if item.isin:
        lines.append(f"    isin: {_q(item.isin)}")
    if item.bse_code:
        lines.append(f"    bse_code: {_q(item.bse_code)}")
    lines.append(f"    # {note}")
    lines.append("    aliases:")
    lines.append(f"      strong: {_flow(item.aliases.strong)}")
    if item.aliases.weak:
        lines.append(f"      weak: {_flow(item.aliases.weak)}")
    for field in ("vocab", "exclude", "noise"):
        values = getattr(item, field)
        if values:
            lines.append(f"    {field}: {_flow(values)}")
    return "\n".join(lines)


def _entries(lines: Sequence[str]) -> list[tuple[int, int, dict]]:
    """(first line, end line, parsed entry) for each item under `watchlist:`. An entry ends
    at the next entry, at a blank line or comment that comes before the next entry, or at
    the next top-level key."""
    start = next((n for n, line in enumerate(lines) if line.rstrip() == "watchlist:"), None)
    if start is None:
        raise EditRefused("config/watchlist.yaml has no `watchlist:` list")
    heads = [n for n in range(start + 1, len(lines)) if _ENTRY.match(lines[n])]
    found = []
    for index, head in enumerate(heads):
        limit = heads[index + 1] if index + 1 < len(heads) else len(lines)
        for n in range(head + 1, limit):
            if _TOP_LEVEL.match(lines[n]):
                limit = n
                break
        end = limit
        # Blank lines and comments just before the next entry belong to that entry.
        while end > head + 1 and (
            not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")
        ):
            end -= 1
        parsed = yaml.safe_load(
            "\n".join(line[2:] if line.startswith("  ") else line for line in lines[head:end])
        )
        entry = parsed[0] if isinstance(parsed, list) and parsed else {}
        found.append((head, end, entry if isinstance(entry, dict) else {}))
    return found


def add_stock(
    path: Path,
    item: WatchItem,
    expected_version: str,
    now: datetime,
    assets: Sequence[AssetConfig] | None = None,
    backup_dir: Path = BACKUP_DIR,
) -> WatchlistFile:
    """Append `item` after the last stock. Returns the new watchlist."""
    current = read(path, assets)
    if current.version != expected_version:
        raise EditRefused("the watchlist changed since this page loaded; reload and try again")
    if any(entry.symbol == item.symbol for entry in current.watchlist.watchlist):
        raise EditRefused(f"{item.symbol} is already on the watchlist")
    lines = current.text.split("\n")
    entries = _entries(lines)
    stocks = [(head, end) for head, end, entry in entries if entry.get("type") == "stock"]
    note = f"Added from the BASIS page on {now:%Y-%m-%d}; aliases checked in its preview."
    block = stock_block(item, note).split("\n")
    if stocks:
        at = stocks[-1][1]
    elif entries:
        at = entries[0][0]
    else:
        at = len(lines)
    after = [""] if at < len(lines) and lines[at].strip() else []
    text = "\n".join([*lines[:at], "", *block, *after, *lines[at:]])
    symbols = [e.symbol for e in current.watchlist.watchlist]
    stock_at = [n for n, e in enumerate(current.watchlist.watchlist) if e.type == "stock"]
    cut = stock_at[-1] + 1 if stock_at else 0
    expected = [*symbols[:cut], item.symbol, *symbols[cut:]]
    return _write(path, current, text, expected, now, assets, backup_dir)


def remove_entry(
    path: Path,
    symbol: str,
    expected_version: str,
    now: datetime,
    assets: Sequence[AssetConfig] | None = None,
    backup_dir: Path = BACKUP_DIR,
) -> WatchlistFile:
    """Take one entry out, with its own lines and comments. Returns the new watchlist."""
    current = read(path, assets)
    if current.version != expected_version:
        raise EditRefused("the watchlist changed since this page loaded; reload and try again")
    lines = current.text.split("\n")
    matches = [(head, end) for head, end, entry in _entries(lines) if entry.get("symbol") == symbol]
    if not matches:
        raise EditRefused(f"{symbol} is not on the watchlist")
    head, end = matches[0]
    # One blank line where the entry was, not two.
    if head > 0 and not lines[head - 1].strip() and end < len(lines) and not lines[end].strip():
        end += 1
    text = "\n".join([*lines[:head], *lines[end:]])
    expected = [e.symbol for e in current.watchlist.watchlist if e.symbol != symbol]
    return _write(path, current, text, expected, now, assets, backup_dir)


def _write(
    path: Path,
    current: Snapshot,
    text: str,
    expected: list[str],
    now: datetime,
    assets: Sequence[AssetConfig] | None,
    backup_dir: Path,
) -> WatchlistFile:
    try:
        edited = _validate(text, assets)
    except Exception as exc:
        raise EditRefused(f"the edited file would not load, so nothing was written: {exc}") from exc
    if [e.symbol for e in edited.watchlist] != expected:
        raise EditRefused("the edit touched more than one entry, so nothing was written")
    unchanged = {e.symbol: e for e in current.watchlist.watchlist}
    for entry in edited.watchlist:
        if entry.symbol in unchanged and entry != unchanged[entry.symbol]:
            raise EditRefused(f"the edit changed {entry.symbol} too, so nothing was written")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{now:%Y%m%d-%H%M%S}"
    (backup_dir / f"watchlist.{stamp}.yaml").write_text(current.text, encoding="utf-8")
    for old in sorted(backup_dir.glob("watchlist.*.yaml"))[:-BACKUPS_KEPT]:
        old.unlink(missing_ok=True)
    temporary = path.with_name(f".{path.name}.new")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
    return edited
