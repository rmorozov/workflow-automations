"""
ADO Graph & Export CLI Tool
============================
Two modes:
  export  – Fetches Work Items from an on-premise Azure DevOps instance via
             Kerberos auth (using the official azure-devops-python-api SDK)
             and exports nodes/edges to parquet, CSV, or Excel.
  analyze – Loads those files and runs NetworkX graph analysis, writing results
             to a multi-sheet Excel workbook.  It needs only the offline
             `ado-graph-analyze` extra; export needs the `ado-graph` extra.

Auth:
  Kerberos (requests-kerberos, OPTIONAL mutual authentication) on an
  azure.devops Connection, built by ado_common.session.connect().

Certificates:
  When export starts, a merged CA bundle is built: certifi's default bundle is
  copied to a temp file, then every .pem/.crt file found under --cert-dir (or the
  env var ADO_CERT_DIR) is appended to it.  Only the export's connection verifies
  against it; REQUESTS_CA_BUNDLE is not changed.  When export ends, also on
  failure, the file is deleted.  Importing this package configures neither
  logging nor certificates.

Work-item filtering (export mode):
  --skip-types        comma-separated WorkItemType values to mark out of scope.
                      Default: Task, Bug, Test Case, Test Suite, Test Plan,
                      Test Run, Shared Steps, Shared Parameter.
                      Pass --skip-types "" to disable type filtering.
  --include-projects  comma-separated project names to keep in scope
                      (case-insensitive, System.TeamProject). Omit for all.

  Filtering decides output scope.  With the default --filtered-relations
  stop, relations of filtered items are not followed and queued IDs are
  classified once via WIQL so filtered items are usually not fetched at all.
  With --filtered-relations follow, every fetched work item's relations are
  traversed and filtered items are exported as bridge nodes
  (In_Scope=False), so `analyze` can keep accepted items connected through
  them; this can reach many unrelated projects.

Export statistics:
  After each BFS batch a live line is logged showing visited / added /
  skipped / excluded / pruned / omitted / queued counts.
  A formatted per-type and per-project table is printed when BFS completes.
"""

from workflow_automations.ado_graph.cli import main

__all__ = ["main"]
