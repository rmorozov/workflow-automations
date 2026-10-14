"""Work-item scalar conversion, relation targets and web links.

Both ADO tools read a relation's target ID from the last segment of its URL and
link work items through the collection's ``_workitems/edit`` page. Standard
library only, so ado-triage can use it without the graph extras.
"""

from __future__ import annotations

from typing import Any


def safe_int(value: Any) -> int | None:
    """Return ``int(value)``, or ``None`` when the value has no integer form."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def relation_target_id(url: str | None) -> int | None:
    """Return the work-item ID a relation URL points to.

    ``None`` means the relation targets no work item (a hyperlink, attachment or
    a missing URL); callers skip it.
    """
    if not url:
        return None
    return safe_int(url.rstrip("/").split("/")[-1])


def work_item_url(base_url: str, wi_id: int) -> str:
    """Return the browser link to a work item under the collection ``base_url``."""
    return f"{base_url.rstrip('/')}/_workitems/edit/{wi_id}"
