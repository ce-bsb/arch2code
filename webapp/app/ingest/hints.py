"""Shape/label heuristics shared by every structural adapter.

These are the same tables ``.bob/skills/diagram-intake/scripts/parse_drawio.py``
carries, lifted here so a ``.vsdx``, a ``.bpmn`` and a ``.drawio`` produce the same
vocabulary. Duplicating them per adapter is how three parsers end up calling the
same cylinder a ``database``, a ``datastore`` and a ``db``.

They are **hints and only hints**. The field is named ``kind_hint`` because
deciding that a box is a *service* is the analyst stage's job, with a human. A
parser sees a rounded rectangle; it does not see a bounded context.

The Portuguese alternative in the protocol table is deliberate and load-bearing:
the drawio fixture in this repo labels an edge "evento PedidoCriado", and dropping
it silently downgrades that edge to ``unknown``.
"""

from __future__ import annotations

import re

__all__ = ["kind_hint", "protocol_hint", "strip_html"]

_SHAPE_HINTS: tuple[tuple[str, str], ...] = (
    (r"mxgraph\.flowchart\.database|shape=cylinder|shape=datastore|\bdatabase\b|\bpostgres\b|\bmysql\b|\boracle\b|\bmongo\b", "database"),
    (r"mxgraph\.aws\d*\.|mxgraph\.azure|mxgraph\.gcp", "cloud_resource"),
    (r"shape=queue|mxgraph\.\w*\.queue|\bkafka\b|\brabbit\b|\bmq\b|\bsqs\b|\btopic\b", "queue"),
    (r"\bredis\b|\bmemcache\b|\bcache\b", "cache"),
    (r"\bs3\b|\bbucket\b|\bminio\b|\bblob storage\b", "storage"),
    (r"shape=cloud|\bexternal\b|\bthird[- ]party\b", "external"),
    (r"shape=actor|shape=umlActor|\bactor\b|\buser\b|\bcustomer\b", "actor"),
    (r"\bgateway\b|\bapi gateway\b|\bingress\b|\bload balancer\b|\bnginx\b", "gateway"),
    (r"ellipse", "event_or_actor"),
    (r"rhombus|\bdecision\b", "decision"),
    (r"shape=process|rounded=1|\bservice\b|\bapi\b|\bworker\b", "service"),
)

_PROTOCOL_HINTS: tuple[tuple[str, str], ...] = (
    (r"\bhttps\b", "https"),
    (r"\b(get|post|put|patch|delete)\s+/", "http"),
    (r"\bhttp\b|\brest\b|\bapi\b", "http"),
    (r"\bgrpc\b", "grpc"),
    (r"\bgraphql\b", "graphql"),
    (r"\bkafka\b|\bevento?\b|\bevent\b|\bpublish(es)?\b|\bsubscribe(s)?\b", "kafka"),
    (r"\bamqp\b|\brabbit\b|\bmq\b", "amqp"),
    (r"\bjdbc\b|\bsql\b|\bselect\b|\binsert\b|\bquery\b", "sql"),
    (r"\bs3\b|\bbucket\b", "s3"),
    (r"\bws\b|\bwebsocket\b", "websocket"),
)

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


def _match(table: tuple[tuple[str, str], ...], text: str, default: str) -> str:
    low = (text or "").lower()
    for pattern, value in table:
        if re.search(pattern, low):
            return value
    return default


def kind_hint(*texts: str, default: str = "unknown") -> str:
    """Guess a node kind from any mixture of style string, label and shape name."""
    return _match(_SHAPE_HINTS, " ".join(t for t in texts if t), default)


def protocol_hint(label: str, default: str = "unknown") -> str:
    """Guess an edge protocol from its label."""
    return _match(_PROTOCOL_HINTS, label, default)


def strip_html(text: str) -> str:
    """Flatten the HTML that draw.io, Visio and SVG all put inside labels.

    Line breaks become a single space: a two-line box label is one name, and
    keeping the newline turns ``Order\\nService`` into two tokens that never match
    the same node mentioned inline elsewhere.
    """
    text = _BR_RE.sub(" ", text or "")
    text = _TAG_RE.sub("", text)
    for entity, char in (
        ("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
        ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"),
    ):
        text = text.replace(entity, char)
    return _WS_RE.sub(" ", text).strip()
