"""cycle-time: split work-item cycle time into spans and export Perfetto traces.

See docs/specs/cycle-time.md. The package is stdlib-only and offline; it reads
normalized input files (today: synthetic scenarios).
"""

from workflow_automations.cycle_time.cli import main

__all__ = ["main"]
