"""PlantUML and Mermaid — the formats where the edge is literally a line of text.

``A --> B : publishes`` needs no library, no renderer and no inference. Neither
graphviz nor a JRE is installed for this, and neither should be: rendering these
to a picture and then looking at the picture with a vision model would be paying
tokens to un-read something already written down.

Scope, stated rather than implied
---------------------------------
These are line-based parsers over the connector grammar, not full front-ends for
two large languages. They read declarations and relations; they do not evaluate
``!include``, preprocessor variables, ``%%{init}%%`` directives or C4 macro
libraries. Anything the parser skipped that looked meaningful becomes a warning
on the graph, so an under-read is visible rather than silent — the failure mode
that matters is a diagram that silently arrives with four of its nine edges.

Every node and edge carries its source line number as evidence, so any claim
downstream can be checked against the file with one jump.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..hints import kind_hint, protocol_hint
from ..models import GraphEdge, GraphNode, StructuredGraph
from .base import Adapter

__all__ = ["PlantUmlAdapter", "MermaidAdapter", "ProseAdapter"]

EXTRACTOR_PUML = "ingest.adapters.textual.plantuml@1.0"
EXTRACTOR_MMD = "ingest.adapters.textual.mermaid@1.0"

_IDENT = r'(?:"[^"]+"|\[[^\]]+\]|[A-Za-z_][\w./:$-]*)'


# --------------------------------------------------------------------------- #
# PlantUML
# --------------------------------------------------------------------------- #
_PUML_DECL_RE = re.compile(
    r'^\s*(?P<kw>participant|actor|boundary|control|entity|collections|database|'
    r'queue|component|interface|class|abstract\s+class|enum|node|rectangle|folder|'
    r'frame|cloud|storage|card|agent|usecase|object|state|person|system|container)\s+'
    r'(?P<rest>.+?)\s*(?:\{\s*)?$',
    re.IGNORECASE,
)
_PUML_ALIAS_RE = re.compile(r'^(?P<a>"[^"]+"|\S+)(?:\s+as\s+(?P<b>"[^"]+"|\S+))?', re.IGNORECASE)
_PUML_EDGE_RE = re.compile(
    rf'^\s*(?P<src>{_IDENT})\s*'
    r'(?P<arrow><\|?[-.]+|[-.=]+(?:\[[^\]]*\])?[-.=]*(?:\|>|>>|>|\*|o)?|'
    r'<[-.=]+(?:\[[^\]]*\])?[-.=]*>?)'
    rf'\s*(?P<dst>{_IDENT})\s*(?::\s*(?P<label>.*?))?\s*$'
)
_PUML_SKIP = re.compile(
    r"^\s*(@start|@end|!|'|/'|skinparam|title\b|header\b|footer\b|legend\b|note\b|"
    r"end note|hide\b|show\b|scale\b|autonumber\b|alt\b|else\b|opt\b|loop\b|par\b|"
    r"break\b|critical\b|group\b|end\b|activate\b|deactivate\b|destroy\b|return\b|"
    r"newpage\b|together\b|package\b|namespace\b|\}|\{)",
    re.IGNORECASE,
)


def _unquote(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] == '"':
        return token[1:-1]
    if len(token) >= 2 and token[0] == "[" and token[-1] == "]":
        return token[1:-1]
    return token


class _TextGraphBuilder:
    """Nodes are created on first mention, so an undeclared endpoint still exists."""

    def __init__(self, fmt: str, extractor: str) -> None:
        self.graph = StructuredGraph(format=fmt, extractor=extractor)
        self._index: dict[str, GraphNode] = {}
        self._edges = 0

    def node(self, key: str, label: str = "", *, line: int = 0, kind: str = "") -> GraphNode:
        existing = self._index.get(key)
        if existing is not None:
            if label and (not existing.label or existing.label == key):
                existing.label = label
                existing.kind_hint = kind or kind_hint(label, default=existing.kind_hint)
            return existing
        node = GraphNode(
            id=key,
            label=label or key,
            kind_hint=kind or kind_hint(label or key),
            evidence=f"line {line}" if line else "",
        )
        self._index[key] = node
        self.graph.nodes.append(node)
        return node

    def edge(self, src: str, dst: str, label: str = "", *, line: int = 0,
             directed: bool = True) -> None:
        self._edges += 1
        self.graph.edges.append(
            GraphEdge(
                id=f"e{self._edges}",
                source=src,
                target=dst,
                label=label,
                directed=directed,
                protocol_hint=protocol_hint(label),
                evidence=f"line {line}",
            )
        )


class PlantUmlAdapter(Adapter):
    id = "plantuml"
    label = "PlantUML source"
    produces_structure = True

    def extract(self, path: Path, pages: list[int] | None = None) -> StructuredGraph:
        builder = _TextGraphBuilder("plantuml", EXTRACTOR_PUML)
        text = path.read_text(encoding="utf-8", errors="replace")
        includes = 0

        for lineno, raw in enumerate(text.splitlines(), start=1):
            line = raw.rstrip()
            if not line.strip():
                continue
            if line.lstrip().startswith("!include"):
                includes += 1
                continue

            edge = _PUML_EDGE_RE.match(line)
            if edge and any(c in edge.group("arrow") for c in "-.="):
                arrow = edge.group("arrow")
                src = _unquote(edge.group("src"))
                dst = _unquote(edge.group("dst"))
                if src.lower() in {"note", "as"} or dst.lower() == "as":
                    continue
                label = (edge.group("label") or "").strip()
                builder.node(src, line=lineno)
                builder.node(dst, line=lineno)
                points_right = arrow.endswith((">", ">>", "|>", "*", "o"))
                points_left = arrow.startswith(("<", "<|"))
                if points_left and not points_right:
                    src, dst = dst, src
                builder.edge(src, dst, label, line=lineno,
                             directed=points_left or points_right)
                continue

            if _PUML_SKIP.match(line):
                continue

            decl = _PUML_DECL_RE.match(line)
            if decl:
                alias = _PUML_ALIAS_RE.match(decl.group("rest").strip())
                if not alias:
                    continue
                first = _unquote(alias.group("a"))
                second = _unquote(alias.group("b") or "")
                key, label = (second, first) if second else (first, first)
                builder.node(key, label, line=lineno,
                             kind=kind_hint(decl.group("kw"), label))
                continue

        if includes:
            builder.graph.warnings.append(
                f"{includes} !include directive(s) were not followed: this parser reads "
                "the file as given and does not resolve the PlantUML preprocessor. If "
                "the architecture lives in the included files, run "
                "`plantuml -preproc diagram.puml` and upload the flattened output."
            )
        if not builder.graph.edges:
            builder.graph.warnings.append(
                "No relations were recognized. If this diagram uses C4-PlantUML macros "
                "(Rel, Container, System), the relations are macro calls rather than "
                "arrows — render it to SVG or PNG and upload that instead."
            )
        return builder.graph


# --------------------------------------------------------------------------- #
# Mermaid
# --------------------------------------------------------------------------- #
_MM_HEADER_RE = re.compile(
    r"^\s*(?P<kind>graph|flowchart|sequenceDiagram|classDiagram(?:-v2)?|erDiagram|"
    r"stateDiagram(?:-v2)?|journey|gitGraph|mindmap|timeline|C4Context|C4Container|"
    r"C4Component|requirementDiagram|quadrantChart|block-beta)\b",
    re.MULTILINE,
)
#: ``A -- calls --> B``: a label sitting between two halves of the arrow.
_MM_MID_LABEL_RE = re.compile(
    r"^\s*(?P<src>[\w.:-]+)\s*(?:--|==|-\.)\s*(?P<label>[^->=|]+?)\s*(?:-{1,2}|={1,2}|\.-)>\s*"
    r"(?P<dst>[\w.:-]+)"
)
_MM_LINK_RE = re.compile(
    r"(?P<src>[\w.:-]+)\s*"
    r"(?P<link>(?:<-{2,}>|[ox<]?(?:-{2,}|={2,}|-\.{1,}-|~{3,})[ox>]?|--[>ox]))\s*"
    r"(?:\|(?P<label>[^|]*)\|\s*)?"
    r"(?P<dst>[\w.:-]+)"
)
_MM_SEQ_RE = re.compile(
    r"^\s*(?P<src>[\w.:-]+)\s*(?P<arrow>-{1,2}>>?|-{1,2}[)x])\s*(?P<dst>[\w.:-]+)"
    r"\s*(?::\s*(?P<label>.*))?$"
)
_MM_ER_RE = re.compile(
    r"^\s*(?P<src>[\w.:-]+)\s+(?P<link>[|}o][|o{-]*--[|o{-]*[|{o]?)\s+(?P<dst>[\w.:-]+)"
    r"\s*(?::\s*(?P<label>.*))?$"
)
_MM_PARTICIPANT_RE = re.compile(
    r"^\s*(?:participant|actor)\s+(?P<key>[\w.:-]+)(?:\s+as\s+(?P<label>.+?))?\s*$",
    re.IGNORECASE,
)

#: Opening bracket -> the closer that ends the label. Longest first when scanning.
_MM_BRACKETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("[(", (")]",)), ("((", ("))",)), ("[[", ("]]",)), ("[/", ("/]", "\\]")),
    ("[\\", ("\\]", "/]")), ("{{", ("}}",)), ("([", ("])",)),
    ("[", ("]",)), ("(", (")",)), ("{", ("}",)), (">", ("]",)),
)


def _strip_mermaid_labels(line: str) -> tuple[str, dict[str, str]]:
    """Pull ``A[Order Service]`` apart into ``A`` plus ``{"A": "Order Service"}``.

    Done by scanning rather than by regex because mermaid's bracket vocabulary
    nests (``[(``, ``((``, ``[[``) and labels may legally contain the closing
    character inside quotes. A regex here reads three edges out of a four-edge
    diagram and says nothing about the one it lost.
    """
    labels: dict[str, str] = {}
    out: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        match = re.compile(r"[A-Za-z_][\w.:-]*").match(line, i)
        if not match:
            out.append(line[i])
            i += 1
            continue
        ident = match.group()
        j = match.end()
        opener = next(
            (o for o, _ in _MM_BRACKETS if line.startswith(o, j)), None
        )
        if opener is None:
            out.append(ident)
            i = j
            continue
        closers = next(c for o, c in _MM_BRACKETS if o == opener)
        k = j + len(opener)
        in_quote = False
        end = -1
        while k < n:
            ch = line[k]
            if ch == '"':
                in_quote = not in_quote
                k += 1
                continue
            if not in_quote and any(line.startswith(c, k) for c in closers):
                end = k
                break
            k += 1
        if end < 0:
            out.append(ident)
            i = j
            continue
        label = line[j + len(opener) : end].strip().strip('"')
        labels[ident] = label
        out.append(ident)
        closer = next(c for c in closers if line.startswith(c, end))
        i = end + len(closer)
    return "".join(out), labels


class MermaidAdapter(Adapter):
    id = "mermaid"
    label = "Mermaid source"
    produces_structure = True

    def extract(self, path: Path, pages: list[int] | None = None) -> StructuredGraph:
        text = path.read_text(encoding="utf-8", errors="replace")
        builder = _TextGraphBuilder("mermaid", EXTRACTOR_MMD)
        header = _MM_HEADER_RE.search(text)
        kind = (header.group("kind") if header else "").lower()
        builder.graph.page_names = [kind or "mermaid"]

        flowchart = kind.startswith(("graph", "flowchart", "c4"))
        sequence = kind.startswith("sequence")
        er = kind.startswith("er")
        classdiag = kind.startswith("class") or kind.startswith("state")

        if not (flowchart or sequence or er or classdiag):
            builder.graph.warnings.append(
                f"Mermaid diagram type '{kind or 'unknown'}' has no structural parser "
                "here: only flowchart/graph, sequenceDiagram, classDiagram, "
                "stateDiagram and erDiagram describe an architecture as nodes and "
                "edges. Render it with the mermaid CLI (`mmdc -i in.mmd -o out.svg`) "
                "and upload the SVG if the picture matters."
            )

        for lineno, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith(("%%", "---", "click ", "style ", "classDef",
                                            "linkStyle", "class ")):
                continue
            if line.startswith("subgraph"):
                name = line[len("subgraph") :].strip().strip('"')
                if name:
                    builder.node(name, name, line=lineno, kind="group")
                continue
            if line in {"end", "}"}:
                continue

            participant = _MM_PARTICIPANT_RE.match(line)
            if participant:
                key = participant.group("key")
                builder.node(key, (participant.group("label") or key).strip(), line=lineno)
                continue

            if er:
                match = _MM_ER_RE.match(line)
                if match:
                    builder.node(match.group("src"), line=lineno)
                    builder.node(match.group("dst"), line=lineno)
                    builder.edge(match.group("src"), match.group("dst"),
                                 (match.group("label") or "").strip(), line=lineno)
                    continue

            if sequence:
                match = _MM_SEQ_RE.match(line)
                if match:
                    builder.node(match.group("src"), line=lineno)
                    builder.node(match.group("dst"), line=lineno)
                    builder.edge(match.group("src"), match.group("dst"),
                                 (match.group("label") or "").strip(), line=lineno)
                    continue

            clean, labels = _strip_mermaid_labels(line)
            for key, label in labels.items():
                builder.node(key, label, line=lineno)

            mid = _MM_MID_LABEL_RE.match(clean)
            if mid:
                builder.node(mid.group("src"), line=lineno)
                builder.node(mid.group("dst"), line=lineno)
                builder.edge(mid.group("src"), mid.group("dst"),
                             mid.group("label").strip(), line=lineno)
                continue

            position = 0
            while True:
                match = _MM_LINK_RE.search(clean, position)
                if not match:
                    break
                src, dst = match.group("src"), match.group("dst")
                builder.node(src, line=lineno)
                builder.node(dst, line=lineno)
                link = match.group("link")
                builder.edge(
                    src, dst, (match.group("label") or "").strip(), line=lineno,
                    directed=link.endswith((">", "x", "o")) or link.startswith("<"),
                )
                # Resume at the target so `A --> B --> C` yields both edges.
                position = match.end("dst") - len(dst)

        if not builder.graph.edges and (flowchart or sequence or er or classdiag):
            builder.graph.warnings.append(
                "The diagram type was recognized but no relation line matched. Check "
                "the file is not a fragment: mermaid needs its header line "
                "(`flowchart LR`, `sequenceDiagram`, ...) as the first statement."
            )
        return builder.graph


# --------------------------------------------------------------------------- #
# Prose
# --------------------------------------------------------------------------- #
class ProseAdapter(Adapter):
    """Markdown/JSON/YAML/plain text: read verbatim, no graph inferred.

    There is deliberately no extraction here. Turning prose into components is a
    judgement call, and judgement belongs to the analyst stage with a human in the
    loop — not to a regex in an ingest adapter. What this adapter guarantees is
    that the text reaches that stage exactly as written, at zero token cost for
    the reading itself.
    """

    id = "prose"
    label = "text description"
    produces_structure = True

    def extract(self, path: Path, pages: list[int] | None = None) -> StructuredGraph | None:
        graph = StructuredGraph(format="prose", extractor="ingest.adapters.textual.prose@1.0")
        graph.warnings.append(
            "Text is passed through verbatim for the analyst stage to read. No nodes "
            "or edges are inferred here: deciding that a paragraph describes a "
            "'service' is a judgement call that belongs with a human, not with a "
            "parser."
        )
        return graph
