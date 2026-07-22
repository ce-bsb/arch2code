"""Repository formats: StarUML ``.mdj`` and Sparx EA 16+ ``.qea``.

Both are the easiest structural reads in the whole table, and both were missing.

**StarUML** writes plain JSON. Every element has ``_id`` and ``_type``; a
relationship carries ``source``/``target`` (or an association's ``end1``/``end2``)
as ``{"$ref": "<id>"}``. A recursive walk over ``ownedElements`` is the entire
parser. Diagram/view objects are skipped: they are the *picture* of the model, and
counting them would double every node.

**Sparx Enterprise Architect 16+** stores its repository as SQLite, so
``sqlite3`` from the standard library reads it with no dependency at all:
``t_object`` is the node table, ``t_connector`` is the edge table, and
``t_diagramobjects`` holds the coordinates. The older ``.eap``/``.eapx`` files are
an Access/JET database that would need the ``mdbtools`` system binary; those are a
declared refusal in ``formats.py`` whose remedy is the one-step conversion inside
EA itself.

The database is opened **read-only** through a ``file:…?mode=ro`` URI. Opening a
user-supplied SQLite file read-write would let it be modified — including its
journal — by the act of looking at it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..errors import CorruptSource
from ..hints import kind_hint, protocol_hint
from ..models import BBox, GraphEdge, GraphNode, StructuredGraph
from .base import Adapter

__all__ = ["StarUmlAdapter", "SparxQeaAdapter"]

EXTRACTOR_MDJ = "ingest.adapters.modelfiles.staruml@1.0"
EXTRACTOR_QEA = "ingest.adapters.modelfiles.sparx_qea@1.0"


def _ref(value: Any) -> str:
    """``{"$ref": "AAAA"}`` -> ``"AAAA"``; anything else -> ``""``."""
    if isinstance(value, dict):
        return str(value.get("$ref") or "")
    if isinstance(value, str):
        return value
    return ""


class StarUmlAdapter(Adapter):
    id = "staruml"
    label = "StarUML model"
    produces_structure = True

    def extract(self, path: Path, pages: list[int] | None = None) -> StructuredGraph:
        try:
            document = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise CorruptSource(
                "ingest_mdj_unparseable",
                "That StarUML file is not valid JSON",
                f"line {exc.lineno}, column {exc.colno}: {exc.msg}",
                remedy=(
                    "Open the model in StarUML and use File > Save As to write a clean "
                    "copy. If it was merged by hand from version control, the conflict "
                    "markers are still in the file."
                ),
            ) from exc

        graph = StructuredGraph(format="staruml", extractor=EXTRACTOR_MDJ)
        self._walk(document, graph, path="$")
        if not graph.nodes:
            graph.warnings.append(
                "No named elements were found. If this is a StarUML *fragment* "
                "(.mfj) rather than a project, open it in StarUML and export the whole "
                "project with File > Save As."
            )
        return graph

    def _walk(self, node: Any, graph: StructuredGraph, path: str) -> None:
        if isinstance(node, list):
            for index, item in enumerate(node):
                self._walk(item, graph, f"{path}[{index}]")
            return
        if not isinstance(node, dict):
            return

        element_id = str(node.get("_id") or "")
        element_type = str(node.get("_type") or "")
        name = " ".join(str(node.get("name") or "").split())
        lowered = element_type.lower()
        is_view = lowered.endswith("view") or "diagram" in lowered

        if element_id and not is_view:
            source = _ref(node.get("source"))
            target = _ref(node.get("target"))
            if not (source and target):
                end1, end2 = node.get("end1"), node.get("end2")
                if isinstance(end1, dict) and isinstance(end2, dict):
                    source = _ref(end1.get("reference"))
                    target = _ref(end2.get("reference"))
            if source and target:
                graph.edges.append(
                    GraphEdge(
                        id=element_id,
                        source=source,
                        target=target,
                        label=name,
                        directed="association" not in lowered,
                        protocol_hint=protocol_hint(f"{name} {element_type}"),
                        evidence=f"{path} (_id={element_id}, _type={element_type})",
                        attrs={"uml_type": element_type},
                    )
                )
            elif name:
                graph.nodes.append(
                    GraphNode(
                        id=element_id,
                        label=name,
                        kind_hint=kind_hint(element_type, name, default=element_type or "unknown"),
                        evidence=f"{path} (_id={element_id}, _type={element_type})",
                        attrs={"uml_type": element_type},
                    )
                )

        for key, value in node.items():
            if key.startswith("_") or key in {"source", "target", "end1", "end2"}:
                continue
            if isinstance(value, (dict, list)):
                self._walk(value, graph, f"{path}.{key}")


class SparxQeaAdapter(Adapter):
    id = "sparx_qea"
    label = "Sparx EA repository"
    produces_structure = True

    _REQUIRED = ("t_object", "t_connector")

    def extract(self, path: Path, pages: list[int] | None = None) -> StructuredGraph:
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
        except sqlite3.Error as exc:
            raise CorruptSource(
                "ingest_qea_unreadable",
                "That Enterprise Architect repository could not be opened",
                f"{path.name}: {exc}",
                remedy=(
                    "Close the project in Enterprise Architect first — an open "
                    "repository holds a lock — then upload the file again."
                ),
            ) from exc

        try:
            connection.row_factory = sqlite3.Row
            tables = {
                row[0].lower()
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            missing = [t for t in self._REQUIRED if t not in tables]
            if missing:
                raise CorruptSource(
                    "ingest_qea_schema_unexpected",
                    "That SQLite file is not an Enterprise Architect repository",
                    f"It has no {', '.join(missing)} table.",
                    remedy=(
                        "Only Sparx EA 16+ .qea repositories are read here. In EA use "
                        "File > Save Project As and choose the .qea format, or publish "
                        "the diagram as XMI, SVG or PDF and upload that."
                    ),
                )

            graph = StructuredGraph(format="sparx-qea", extractor=EXTRACTOR_QEA)
            boxes = self._layout(connection, tables)
            for row in connection.execute(
                "SELECT Object_ID, Object_Type, Name, Stereotype FROM t_object"
            ):
                object_id = str(row["Object_ID"])
                object_type = row["Object_Type"] or ""
                stereotype = row["Stereotype"] or ""
                name = " ".join((row["Name"] or "").split())
                graph.nodes.append(
                    GraphNode(
                        id=object_id,
                        label=name,
                        kind_hint=kind_hint(object_type, stereotype, name,
                                            default=object_type or "unknown"),
                        bbox=boxes.get(object_id),
                        evidence=f"t_object.Object_ID={object_id}",
                        attrs={"ea_type": object_type, "stereotype": stereotype},
                    )
                )
            for row in connection.execute(
                "SELECT Connector_ID, Connector_Type, Name, Start_Object_ID, "
                "End_Object_ID, Direction FROM t_connector"
            ):
                name = " ".join((row["Name"] or "").split())
                connector_type = row["Connector_Type"] or ""
                graph.edges.append(
                    GraphEdge(
                        id=f"c{row['Connector_ID']}",
                        source=str(row["Start_Object_ID"]),
                        target=str(row["End_Object_ID"]),
                        label=name,
                        directed=(row["Direction"] or "").lower() != "unspecified",
                        protocol_hint=protocol_hint(f"{name} {connector_type}"),
                        evidence=f"t_connector.Connector_ID={row['Connector_ID']}",
                        attrs={"ea_type": connector_type},
                    )
                )
            return graph
        finally:
            connection.close()

    @staticmethod
    def _layout(connection: sqlite3.Connection, tables: set[str]) -> dict[str, BBox]:
        """Coordinates from the first diagram, when the repository has one.

        EA stores rectangles per *diagram*, and one object can appear on several.
        The first diagram wins and the rest are ignored rather than averaged: an
        averaged position is a coordinate that is true nowhere.
        """
        if "t_diagramobjects" not in tables:
            return {}
        try:
            rows = list(
                connection.execute(
                    "SELECT Object_ID, RectLeft, RectRight, RectTop, RectBottom, "
                    "Diagram_ID FROM t_diagramobjects ORDER BY Diagram_ID"
                )
            )
        except sqlite3.Error:
            return {}
        if not rows:
            return {}
        first_diagram = rows[0]["Diagram_ID"]
        raw: dict[str, tuple[float, float, float, float]] = {}
        for row in rows:
            if row["Diagram_ID"] != first_diagram:
                break
            try:
                left = float(row["RectLeft"])
                right = float(row["RectRight"])
                # EA's Y axis points down and is stored negative.
                top = -float(row["RectTop"])
                bottom = -float(row["RectBottom"])
            except (TypeError, ValueError):
                continue
            width, height = right - left, bottom - top
            if width <= 0 or height <= 0:
                continue
            raw[str(row["Object_ID"])] = (left, top, width, height)
        if not raw:
            return {}
        min_x = min(v[0] for v in raw.values())
        min_y = min(v[1] for v in raw.values())
        span_x = (max(v[0] + v[2] for v in raw.values()) - min_x) or 1.0
        span_y = (max(v[1] + v[3] for v in raw.values()) - min_y) or 1.0
        return {
            key: BBox(
                x=round((x - min_x) / span_x, 5),
                y=round((y - min_y) / span_y, 5),
                w=round(w / span_x, 5),
                h=round(h / span_y, 5),
            )
            for key, (x, y, w, h) in raw.items()
        }
