"""BPMN 2.0 and ArchiMate — two XML dialects where the edge is an element.

BPMN
----
``<bpmn:sequenceFlow sourceRef="A" targetRef="B"/>`` *is* the arrow. Nothing has
to be inferred, and the ``id`` attribute is a citation a reviewer can grep for.
Layout, when the file carries it, lives in ``<bpmndi:BPMNDiagram>`` as a set of
``<dc:Bounds>`` keyed by ``bpmnElement`` — that is where the boxes' coordinates
come from, so a BPMN node lands on the overlay in the same place a vision node
would.

ArchiMate
---------
Two dialects exist in the wild and both are read here:

* the **Open Group Model Exchange** format — ``<elements><element identifier=…
  xsi:type="ApplicationComponent"><name>``, with ``<relationships>`` carrying
  ``source``/``target``;
* **Archi's native** file — ``<archimate:model>`` with ``<folder>`` trees whose
  elements carry ``id``/``name`` attributes and whose relationships carry
  ``source``/``target``.

Rather than branching on the dialect, the walker uses the shape of each element:
anything with both ``source`` and ``target`` is an edge, anything with an id and a
name is a node. That is dialect-proof and survives the next tool that invents a
third spelling.

Namespaces are stripped to local names throughout. Every one of these formats is
emitted with a different prefix by every tool that writes it, and matching on
``{http://...}sequenceFlow`` is how a parser works for Camunda and silently
returns nothing for Signavio.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

from ..errors import CorruptSource
from ..hints import kind_hint, protocol_hint
from ..models import BBox, GraphEdge, GraphNode, StructuredGraph
from .base import Adapter

__all__ = ["BpmnAdapter", "ArchimateAdapter"]

EXTRACTOR_BPMN = "ingest.adapters.xmlgraph.bpmn@1.0"
EXTRACTOR_ARCHIMATE = "ingest.adapters.xmlgraph.archimate@1.0"

_BPMN_NODE_TAGS = frozenset({
    "task", "usertask", "servicetask", "sendtask", "receivetask", "scripttask",
    "manualtask", "businessruletask", "callactivity", "subprocess", "transaction",
    "startevent", "endevent", "intermediatecatchevent", "intermediatethrowevent",
    "boundaryevent", "exclusivegateway", "parallelgateway", "inclusivegateway",
    "eventbasedgateway", "complexgateway", "dataobjectreference",
    "datastorereference", "participant", "lane", "textannotation",
})
_BPMN_EDGE_TAGS = frozenset({"sequenceflow", "messageflow", "association", "dataassociation"})


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _parse(path: Path, what: str, remedy: str) -> ET.Element:
    try:
        return ET.fromstring(path.read_text(encoding="utf-8", errors="replace"))
    except ET.ParseError as exc:
        raise CorruptSource(
            f"ingest_{what}_unparseable",
            f"That {what.upper()} file is not valid XML",
            str(exc),
            remedy=remedy,
        ) from exc


def _attr(element: ET.Element, *names: str) -> str:
    """First present attribute among ``names``, namespace-insensitively."""
    for name in names:
        value = element.get(name)
        if value:
            return value
    lowered = {k.rsplit("}", 1)[-1].lower(): v for k, v in element.attrib.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value:
            return value
    return ""


class BpmnAdapter(Adapter):
    id = "bpmn"
    label = "BPMN 2.0 model"
    produces_structure = True

    def extract(self, path: Path, pages: list[int] | None = None) -> StructuredGraph:
        root = _parse(
            path, "bpmn",
            "Re-export the process from the modelling tool (Camunda Modeler, Signavio, "
            "bpmn.io). If it was edited by hand, the file is probably missing a "
            "closing tag.",
        )
        graph = StructuredGraph(format="bpmn", extractor=EXTRACTOR_BPMN)

        # Pass 1: diagram interchange, so nodes can carry coordinates.
        bounds: dict[str, tuple[float, float, float, float]] = {}
        for element in root.iter():
            if _local(element.tag).lower() != "bpmnshape":
                continue
            ref = _attr(element, "bpmnElement")
            box = next((c for c in element if _local(c.tag).lower() == "bounds"), None)
            if not ref or box is None:
                continue
            try:
                bounds[ref] = (
                    float(box.get("x") or 0), float(box.get("y") or 0),
                    float(box.get("width") or 0), float(box.get("height") or 0),
                )
            except ValueError:
                continue
        boxes = _normalize(bounds)

        for element in root.iter():
            tag = _local(element.tag).lower()
            element_id = _attr(element, "id")
            if not element_id:
                continue
            name = " ".join((_attr(element, "name") or "").split())
            if tag in _BPMN_EDGE_TAGS or tag.endswith("association"):
                source = _attr(element, "sourceRef", "source")
                target = _attr(element, "targetRef", "target")
                if not source or not target:
                    continue
                graph.edges.append(
                    GraphEdge(
                        id=element_id,
                        source=source,
                        target=target,
                        label=name,
                        protocol_hint=protocol_hint(name or tag),
                        evidence=f"<{tag} id='{element_id}'>",
                        attrs={"bpmn_type": tag},
                    )
                )
            elif tag in _BPMN_NODE_TAGS:
                graph.nodes.append(
                    GraphNode(
                        id=element_id,
                        label=name,
                        kind_hint=_bpmn_kind(tag, name),
                        bbox=boxes.get(element_id),
                        evidence=f"<{tag} id='{element_id}'>",
                        attrs={"bpmn_type": tag},
                    )
                )

        if not bounds:
            graph.warnings.append(
                "This BPMN file has no <bpmndi:BPMNDiagram> section, so it carries the "
                "process but no layout. The graph is complete; the nodes simply have no "
                "coordinates to draw a box at."
            )
        return graph


def _bpmn_kind(tag: str, name: str) -> str:
    if tag.endswith("gateway"):
        return "decision"
    if tag.endswith("event"):
        return "event"
    if tag in {"datastorereference", "dataobjectreference"}:
        return "database"
    if tag in {"participant", "lane"}:
        return "actor"
    if tag == "servicetask":
        return "service"
    return kind_hint(tag, name, default="task")


class ArchimateAdapter(Adapter):
    id = "archimate"
    label = "ArchiMate model"
    produces_structure = True

    def extract(self, path: Path, pages: list[int] | None = None) -> StructuredGraph:
        root = _parse(
            path, "archimate",
            "Re-export the model from Archi (File > Export > Model To Open Exchange "
            "File) or save the .archimate file again. If only the view matters, "
            "File > Export > View As Image gives a PNG the vision path can read.",
        )
        graph = StructuredGraph(format="archimate", extractor=EXTRACTOR_ARCHIMATE)
        seen_nodes: set[str] = set()

        for element in root.iter():
            tag = _local(element.tag).lower()
            if tag in {"model", "folder", "elements", "relationships", "views",
                       "documentation", "name", "property", "propertydefinitions"}:
                continue
            element_id = _attr(element, "identifier", "id")
            if not element_id:
                continue
            xsi_type = _attr(element, "type") or tag
            xsi_type = xsi_type.split(":")[-1]
            name = _attr(element, "name")
            if not name:
                child = next((c for c in element if _local(c.tag).lower() == "name"), None)
                if child is not None and child.text:
                    name = child.text
            name = " ".join((name or "").split())

            source = _attr(element, "source", "sourceRef")
            target = _attr(element, "target", "targetRef")
            if source and target:
                graph.edges.append(
                    GraphEdge(
                        id=element_id,
                        source=source,
                        target=target,
                        label=name,
                        protocol_hint=protocol_hint(f"{name} {xsi_type}"),
                        evidence=f"<{tag} identifier='{element_id}'>",
                        attrs={"archimate_type": xsi_type},
                    )
                )
                continue
            if element_id in seen_nodes:
                continue
            seen_nodes.add(element_id)
            graph.nodes.append(
                GraphNode(
                    id=element_id,
                    label=name,
                    kind_hint=_archimate_kind(xsi_type, name),
                    evidence=f"<{tag} identifier='{element_id}'>",
                    attrs={"archimate_type": xsi_type},
                )
            )

        if not graph.nodes and not graph.edges:
            graph.warnings.append(
                "No ArchiMate elements were found. If this is an Archi workspace "
                "rather than a model file, use File > Export > Model To Open Exchange "
                "File and upload that."
            )
        return graph


def _archimate_kind(xsi_type: str, name: str) -> str:
    lowered = xsi_type.lower()
    for needle, kind in (
        ("applicationcomponent", "service"),
        ("applicationservice", "service"),
        ("technologyservice", "service"),
        ("node", "host"),
        ("device", "host"),
        ("systemsoftware", "runtime"),
        ("dataobject", "database"),
        ("artifact", "storage"),
        ("businessactor", "actor"),
        ("businessrole", "actor"),
        ("businessprocess", "task"),
    ):
        if needle in lowered:
            return kind
    return kind_hint(xsi_type, name, default=xsi_type or "unknown")


def _normalize(raw: dict[str, tuple[float, float, float, float]]) -> dict[str, BBox]:
    """Absolute layout coordinates into 0..1 against the drawing's own extent."""
    usable = {k: v for k, v in raw.items() if v[2] > 0 and v[3] > 0}
    if not usable:
        return {}
    min_x = min(v[0] for v in usable.values())
    min_y = min(v[1] for v in usable.values())
    span_x = (max(v[0] + v[2] for v in usable.values()) - min_x) or 1.0
    span_y = (max(v[1] + v[3] for v in usable.values()) - min_y) or 1.0
    return {
        key: BBox(
            x=round((x - min_x) / span_x, 5),
            y=round((y - min_y) / span_y, 5),
            w=round(w / span_x, 5),
            h=round(h / span_y, 5),
        )
        for key, (x, y, w, h) in usable.items()
    }
