"""Output writers: export files (Parquet/CSV/Excel) and the analysis workbook."""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

try:
    import openpyxl
    from openpyxl.utils import get_column_letter
except ImportError:
    openpyxl = None

log = logging.getLogger("ado_tool")


def _export_dataframes(
    df_nodes: pd.DataFrame,
    df_edges: pd.DataFrame,
    output_dir: Path,
    formats: list[str],
) -> None:
    dispatch = {
        "parquet": _export_parquet,
        "csv": _export_csv,
        "excel": _export_excel,
    }
    for fmt in formats:
        fn = dispatch.get(fmt)
        if fn is None:
            log.warning("Unknown export format '%s' – skipping.", fmt)
            continue
        fn(df_nodes, df_edges, output_dir)
        log.info("Exported %s.", fmt)


def _export_parquet(df_nodes: pd.DataFrame, df_edges: pd.DataFrame, d: Path) -> None:
    df_nodes.to_parquet(d / "nodes.parquet", index=False)
    df_edges.to_parquet(d / "edges.parquet", index=False)


def _export_csv(df_nodes: pd.DataFrame, df_edges: pd.DataFrame, d: Path) -> None:
    df_nodes.to_csv(d / "nodes.csv", index=False)
    df_edges.to_csv(d / "edges.csv", index=False)


def _export_excel(df_nodes: pd.DataFrame, df_edges: pd.DataFrame, d: Path) -> None:
    with pd.ExcelWriter(d / "work_items.xlsx", engine="openpyxl") as writer:
        df_nodes.to_excel(writer, sheet_name="Nodes", index=False)
        df_edges.to_excel(writer, sheet_name="Edges", index=False)


def write_analysis_report(results: dict[str, pd.DataFrame], output_path: Path) -> None:
    """Write one sheet per analysis step, with column widths sized to content."""
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
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

    log.info("Analysis report written to %s", output_path)
