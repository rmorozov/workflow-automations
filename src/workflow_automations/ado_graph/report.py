"""Output writers: export files (Parquet/CSV/Excel) and the analysis workbook."""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

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


def _export_dataframes(
    df_nodes: pd.DataFrame,
    df_edges: pd.DataFrame,
    output_dir: Path,
    formats: list[str],
    overwrite: bool = True,
) -> None:
    """Publish every requested format as one staged bundle.

    ``overwrite`` defaults to True because the CLI asks for consent before the run.
    """
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
        df_nodes.to_excel(writer, sheet_name="Nodes", index=False)
        df_edges.to_excel(writer, sheet_name="Edges", index=False)


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
