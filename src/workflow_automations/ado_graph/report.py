"""Output writers: export files (Parquet/CSV/Excel) and the analysis workbook."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

try:
    import openpyxl
    from openpyxl.utils import get_column_letter
except ImportError:
    openpyxl = None

log = logging.getLogger("ado_tool")


# Files each export format writes into the output directory.
EXPORT_FILES: dict[str, tuple[str, ...]] = {
    "parquet": ("nodes.parquet", "edges.parquet"),
    "csv": ("nodes.csv", "edges.csv"),
    "excel": ("work_items.xlsx",),
}


def known_formats(formats: list[str]) -> list[str]:
    """Return the known formats once each, in order, warning about unknown ones."""
    known: list[str] = []
    for fmt in formats:
        if fmt not in EXPORT_FILES:
            log.warning("Unknown export format '%s' – skipping.", fmt)
        elif fmt not in known:
            known.append(fmt)
    return known


def export_paths(output_dir: Path, formats: list[str]) -> list[Path]:
    """Paths an export with these formats publishes; unknown formats add none."""
    names = dict.fromkeys(name for fmt in formats for name in EXPORT_FILES.get(fmt, ()))
    return [output_dir / name for name in names]


def publish_all(targets: Sequence[tuple[Path, Callable[[Path], None]]], overwrite: bool) -> None:
    """Write every target to a sibling temp file, then rename them all into place.

    The same approach as ``ado-triage``: a failure while writing (permissions, full
    disk, a serialization error) leaves every existing target unchanged.
    """
    for path, _ in targets:
        if path.exists() and not overwrite:
            raise FileExistsError(f"{path} exists; pass --overwrite to replace it")
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


NODE_KEY = "WI_ID"
EDGE_COLUMNS = ("Source_ID", "Target_ID", "Link_Type")

# An ISO-8601 timestamp that names its offset, as the ADO REST API returns dates.
_ZONED_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})")


def _identity_text(value: dict) -> str | None:
    """``Display Name <unique name>`` for an ADO identity reference, else None."""
    name, unique = value.get("displayName"), value.get("uniqueName")
    if not isinstance(name, str) and not isinstance(unique, str):
        return None
    if isinstance(name, str) and isinstance(unique, str) and name != unique:
        return f"{name} <{unique}>"
    return name if isinstance(name, str) else unique


def _scalar(value: Any) -> Any:
    """Identities become text; other dicts and lists become sorted JSON."""
    if isinstance(value, dict):
        identity = _identity_text(value)
        if identity is not None:
            return identity
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, default=str)
    return value


def _is_zoned_date(value: Any) -> bool:
    if isinstance(value, dt.datetime):
        return value.tzinfo is not None
    return isinstance(value, str) and _ZONED_TIMESTAMP.fullmatch(value) is not None


def _normalize_column(column: pd.Series) -> pd.Series:
    if isinstance(column.dtype, pd.DatetimeTZDtype):
        return column.dt.tz_convert("UTC").dt.tz_localize(None)
    if not (pd.api.types.is_object_dtype(column) or pd.api.types.is_string_dtype(column)):
        return column
    present = column.dropna()
    if present.empty:
        return column
    if all(_is_zoned_date(v) for v in present):
        return pd.to_datetime(column, utc=True, format="ISO8601").dt.tz_localize(None)
    column = column.map(_scalar, na_action="ignore")
    present = column.dropna()
    if any(isinstance(v, str) for v in present) and not all(isinstance(v, str) for v in present):
        # Mixed text and numbers cannot share a Parquet column; keep them as text.
        column = column.map(str, na_action="ignore")
    return column


def _require_ids(frame: pd.DataFrame, name: str, columns: Sequence[str]) -> pd.DataFrame:
    for col in columns:
        if frame.empty:
            frame[col] = frame[col].astype("int64")
        elif not pd.api.types.is_integer_dtype(frame[col]) or frame[col].isna().any():
            raise ValueError(f"{name}: column {col} must hold integer work-item IDs")
    return frame


def prepare_export_frames(
    df_nodes: pd.DataFrame, df_edges: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Validate the export schema and normalize values every format can store.

    Raises ValueError before anything is written when a frame breaks the schema.
    """
    nodes = df_nodes.copy()
    if nodes.empty and NODE_KEY not in nodes.columns:
        nodes = pd.DataFrame(
            {NODE_KEY: pd.Series(dtype="int64"), "In_Scope": pd.Series(dtype=bool)}
        )
    if nodes.columns.duplicated().any():
        dupes = sorted(set(nodes.columns[nodes.columns.duplicated()]))
        raise ValueError(f"nodes: duplicate columns {dupes}")
    if NODE_KEY not in nodes.columns:
        raise ValueError(f"nodes: missing column {NODE_KEY}")
    nodes = _require_ids(nodes, "nodes", [NODE_KEY])
    if nodes[NODE_KEY].duplicated().any():
        dupes = sorted(nodes.loc[nodes[NODE_KEY].duplicated(), NODE_KEY].unique().tolist())
        raise ValueError(f"nodes: duplicate {NODE_KEY} values {dupes[:20]}")
    for col in nodes.columns:
        nodes[col] = _normalize_column(nodes[col])

    if tuple(df_edges.columns) != EDGE_COLUMNS:
        raise ValueError(
            f"edges: columns must be {list(EDGE_COLUMNS)}, got {list(df_edges.columns)}"
        )
    edges = _require_ids(df_edges.copy(), "edges", EDGE_COLUMNS[:2])
    edges["Link_Type"] = edges["Link_Type"].astype(str)
    return nodes, edges


def _export_dataframes(
    df_nodes: pd.DataFrame,
    df_edges: pd.DataFrame,
    output_dir: Path,
    formats: list[str],
    overwrite: bool = True,
) -> None:
    """Validate, normalize and publish every requested format as one staged bundle.

    ``overwrite`` defaults to True because the CLI asks for consent before the run.
    """
    df_nodes, df_edges = prepare_export_frames(df_nodes, df_edges)
    writers = {
        "nodes.parquet": lambda p: df_nodes.to_parquet(p, index=False),
        "edges.parquet": lambda p: df_edges.to_parquet(p, index=False),
        "nodes.csv": lambda p: df_nodes.to_csv(p, index=False),
        "edges.csv": lambda p: df_edges.to_csv(p, index=False),
        "work_items.xlsx": lambda p: _write_excel(df_nodes, df_edges, p),
    }
    known = known_formats(formats)
    paths = export_paths(output_dir, known)
    publish_all([(path, writers[path.name]) for path in paths], overwrite)
    for fmt in known:
        log.info("Exported %s.", fmt)


def _write_excel(df_nodes: pd.DataFrame, df_edges: pd.DataFrame, path: Path) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for sheet, frame in (("Nodes", df_nodes), ("Edges", df_edges)):
            frame.to_excel(writer, sheet_name=sheet, index=False)
            _keep_text_literal(writer.sheets[sheet])


def _keep_text_literal(worksheet: Any) -> None:
    """Store text that openpyxl would read as a formula (``=...``) as a string cell."""
    for row in worksheet.iter_rows(min_row=2):
        for cell in row:
            if cell.data_type == "f":
                cell.data_type = "s"
                cell.quotePrefix = True


def write_analysis_report(
    results: dict[str, pd.DataFrame], output_path: Path, overwrite: bool = True
) -> None:
    """Publish one sheet per analysis step, with column widths sized to content.

    ``overwrite`` defaults to True because the CLI asks for consent before the run.
    """
    publish_all([(output_path, lambda p: _write_workbook(results, p))], overwrite)
    log.info("Analysis report written to %s", output_path)


def _write_workbook(results: dict[str, pd.DataFrame], path: Path) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for sheet_name, df in results.items():
            safe_name = sheet_name[:31]
            df.to_excel(writer, sheet_name=safe_name, index=False)
            # Auto-adjust column widths for better UX
            worksheet = writer.sheets[sheet_name]
            for column in worksheet.columns:
                max_length = 0
                column_letter = get_column_letter(column[0].column)
                for cell in column:
                    try:
                        if len(str(cell.value)) > max_length:
                            max_length = len(str(cell.value))
                    except Exception:
                        pass
                adjusted_width = (max_length + 2) if max_length < 50 else 50
                worksheet.column_dimensions[column_letter].width = adjusted_width
            log.info("  → sheet '%s' (%d rows)", safe_name, len(df))
