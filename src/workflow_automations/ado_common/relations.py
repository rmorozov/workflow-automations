"""ADO relation reference names, their directions, and per-tool label schemas.

ADO reports every work-item link from each end: the emitting item carries the
``-Forward`` or ``-Reverse`` reference name of its own end, and the other
endpoint's ID is the last segment of the relation URL. ``LINK_TYPES`` is the one
registry of the reference names both tools understand and the direction of each.

The tools label the same relation differently, so each has an explicit adapter:

- ``GRAPH`` (ado-graph): ``Source_ID -> Target_ID`` with forward-reading labels
  such as ``Predecessor->Successor``.
- ``TRIAGE`` (ado-triage): ``Source_ID -> Destination_ID`` with labels relative
  to the emitting item, such as ``Predecessor`` (the destination is this item's
  predecessor).

Neither schema reads ``Hierarchy-Reverse``: the parent already emits the same
pair as ``Hierarchy-Forward``, and the child's parent is ``System.Parent``.
Reading it too would duplicate every parent-child edge, so leaving it out is a
policy each schema states, not an omission from the registry.

Standard library only, so ado-triage can use it without the graph extras.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from workflow_automations.ado_common.workitem import relation_target_id

FORWARD = "forward"
REVERSE = "reverse"

HIERARCHY_FORWARD = "System.LinkTypes.Hierarchy-Forward"  # this item is the parent
HIERARCHY_REVERSE = "System.LinkTypes.Hierarchy-Reverse"  # this item is the child
DEPENDENCY_FORWARD = "System.LinkTypes.Dependency-Forward"  # the target depends on this item
DEPENDENCY_REVERSE = "System.LinkTypes.Dependency-Reverse"  # this item depends on the target
BLOCKS_FORWARD = "Microsoft.VSTS.Common.Blocks-Forward"  # this item blocks the target
BLOCKS_REVERSE = "Microsoft.VSTS.Common.Blocks-Reverse"  # the target blocks this item
AFFECTS_FORWARD = "Microsoft.VSTS.Common.Affects-Forward"
AFFECTS_REVERSE = "Microsoft.VSTS.Common.Affects-Reverse"
IMPLEMENTS_FORWARD = "Microsoft.VSTS.Common.Implements-Forward"
IMPLEMENTS_REVERSE = "Microsoft.VSTS.Common.Implements-Reverse"


@dataclass(frozen=True)
class LinkType:
    """One end of an ADO link type, named by its reference name."""

    reference: str
    family: str  # the link type without its end: hierarchy, dependency, ...
    direction: str  # FORWARD or REVERSE

    @property
    def inverse(self) -> str:
        """The reference name the other endpoint reports for the same link."""
        other = "Reverse" if self.direction == FORWARD else "Forward"
        return self.reference.rsplit("-", 1)[0] + "-" + other


def _registry(*references: str) -> Mapping[str, LinkType]:
    types = {}
    for ref in references:
        stem, end = ref.rsplit("-", 1)
        family = stem.rsplit(".", 1)[-1].lower()
        types[ref] = LinkType(ref, family, FORWARD if end == "Forward" else REVERSE)
    return MappingProxyType(types)


LINK_TYPES: Mapping[str, LinkType] = _registry(
    HIERARCHY_FORWARD,
    HIERARCHY_REVERSE,
    DEPENDENCY_FORWARD,
    DEPENDENCY_REVERSE,
    BLOCKS_FORWARD,
    BLOCKS_REVERSE,
    AFFECTS_FORWARD,
    AFFECTS_REVERSE,
    IMPLEMENTS_FORWARD,
    IMPLEMENTS_REVERSE,
)


@dataclass(frozen=True)
class RelationSchema:
    """A tool's view of relations: which reference names it reads, and their labels.

    ``labels`` maps a registered reference name to the tool's label. ``excluded``
    names registered references the tool deliberately does not read.
    """

    name: str
    labels: Mapping[str, str]
    excluded: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        unknown = (set(self.labels) | self.excluded) - set(LINK_TYPES)
        if unknown:
            raise ValueError(f"{self.name}: unregistered reference names {sorted(unknown)}")
        if set(self.labels) & self.excluded:
            raise ValueError(f"{self.name}: a reference name is both labelled and excluded")
        object.__setattr__(self, "labels", MappingProxyType(dict(self.labels)))

    def __contains__(self, reference: object) -> bool:
        return reference in self.labels

    def label(self, reference: str | None) -> str | None:
        """The label of ``reference``, or ``None`` when this schema does not read it."""
        return self.labels.get(reference or "")

    def endpoint(self, reference: str | None, url: str | None) -> tuple[str, int] | None:
        """``(label, other endpoint ID)``, or ``None`` for a relation this schema skips.

        Skipped are reference names the schema does not read and URLs that point
        to no work item (hyperlinks, attachments, missing URLs).
        """
        label = self.label(reference)
        if label is None:
            return None
        target = relation_target_id(url)
        return None if target is None else (label, target)

    def targets(self, relations: Iterable[Mapping[str, str | None]]) -> set[int]:
        """Endpoint IDs of the ``{"rel", "url"}`` relations this schema reads."""
        return {
            hit[1]
            for rel in relations
            if (hit := self.endpoint(rel["rel"], rel["url"])) is not None
        }

    def inverse_labels(self) -> dict[str, str]:
        """Reverse-end label -> the label of its forward end, where both are read."""
        return {
            label: self.labels[LINK_TYPES[ref].inverse]
            for ref, label in self.labels.items()
            if LINK_TYPES[ref].direction == REVERSE and LINK_TYPES[ref].inverse in self.labels
        }


GRAPH = RelationSchema(
    "ado-graph",
    {
        HIERARCHY_FORWARD: "Parent->Child",
        AFFECTS_FORWARD: "Affects",
        AFFECTS_REVERSE: "Affected By",
        BLOCKS_FORWARD: "Blocks",
        BLOCKS_REVERSE: "Blocked By",
        DEPENDENCY_FORWARD: "Predecessor->Successor",
        DEPENDENCY_REVERSE: "Successor->Predecessor",
        IMPLEMENTS_FORWARD: "Implements",
        IMPLEMENTS_REVERSE: "Implemented By",
    },
    excluded=frozenset({HIERARCHY_REVERSE}),
)

TRIAGE = RelationSchema(
    "ado-triage",
    {
        HIERARCHY_FORWARD: "Child",
        DEPENDENCY_REVERSE: "Predecessor",
        DEPENDENCY_FORWARD: "Successor",
        BLOCKS_FORWARD: "Blocks",
        BLOCKS_REVERSE: "Blocked By",
    },
    excluded=frozenset({HIERARCHY_REVERSE}),
)
