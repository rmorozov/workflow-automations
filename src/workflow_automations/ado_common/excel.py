"""Excel cell safety and staged publication shared by the ADO tools.

Layout (sheets, widths, styling) stays with each tool; this module only makes
cell values safe to write and publishes a run's files all-or-nothing.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

# The control characters openpyxl refuses to store (openpyxl.cell.cell.ILLEGAL_CHARACTERS_RE);
# they occur in ADO rich-text fields. Defined here so this module needs no openpyxl.
ILLEGAL_CHARACTERS_RE = re.compile(r"[\000-\010]|[\013-\014]|[\016-\037]")


def excel_safe(frame: pd.DataFrame) -> pd.DataFrame:
    """Return ``frame`` with values openpyxl can store, copying only when needed.

    Text loses the control characters openpyxl rejects. Timezone-aware datetimes,
    which openpyxl refuses, become naive UTC, whether a whole column or single
    values in a mixed one.
    """
    changed: dict[int, pd.Series] = {}
    for position in range(frame.shape[1]):
        series = frame.iloc[:, position]
        if isinstance(series.dtype, pd.DatetimeTZDtype):
            changed[position] = series.dt.tz_convert("UTC").dt.tz_localize(None)
        elif pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series):
            if any(_cell_safe(v) is not v for v in series.dropna()):
                changed[position] = series.map(_cell_safe, na_action="ignore")
    if not changed:
        return frame
    frame = frame.copy()
    for position, series in changed.items():
        frame.isetitem(position, series)
    return frame


def _cell_safe(value: Any) -> Any:
    """``value`` itself when openpyxl can store it, else a safe equivalent."""
    if isinstance(value, str):
        # A clean string is returned as is, so callers can tell nothing changed.
        return (
            value
            if not ILLEGAL_CHARACTERS_RE.search(value)
            else ILLEGAL_CHARACTERS_RE.sub("", value)
        )
    if isinstance(value, dt.datetime) and value.tzinfo is not None:
        return pd.Timestamp(value).tz_convert("UTC").tz_localize(None)
    return value


def _strip(text: str) -> str:
    return ILLEGAL_CHARACTERS_RE.sub("", text) if ILLEGAL_CHARACTERS_RE.search(text) else text


def keep_text_literal(worksheet: Any, min_row: int = 2) -> None:
    """Store text that openpyxl would read as a formula (``=...``) as a string cell.

    ``min_row`` is the first data row; headings above it are left alone.
    """
    for row in worksheet.iter_rows(min_row=min_row):
        for cell in row:
            if cell.data_type == "f":
                cell.data_type = "s"
                cell.quotePrefix = True


def publish_all(
    targets: Sequence[tuple[Path, Callable[[Path], None]]],
    overwrite: bool,
    exists_error: Callable[[str], Exception] = FileExistsError,
) -> None:
    """Write every target to a sibling temp file, then rename them all into place.

    An existing target without ``overwrite`` raises ``exists_error(message)``
    before anything is written, so each tool keeps its own error type. A failure
    while writing (permissions, full disk, a serialization error) leaves every
    existing target unchanged and removes the temp files.
    """
    for path, _ in targets:
        if path.exists() and not overwrite:
            raise exists_error(f"{path} exists; pass --overwrite to replace it")
    staged: list[tuple[Path, Path]] = []
    try:
        for path, write in targets:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=path.suffix, dir=path.parent)
            os.close(fd)
            staged.append((Path(tmp), path))
            write(Path(tmp))
        for tmp, path in staged:
            os.replace(tmp, path)
    finally:
        for tmp, _ in staged:
            if tmp.exists():
                tmp.unlink()
