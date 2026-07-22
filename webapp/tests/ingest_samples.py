"""Real sample files for the ingest tests, generated rather than committed.

Every adapter is exercised against a file this module *builds*, not against a
mock. A parser tested with a hand-written dict passes forever and still fails on
the first real export, because the interesting part of every one of these formats
is the encoding — draw.io's raw-deflate payload, the PNG ``tEXt`` chunk, the PDF
xref table, the VSDX ``<Connect FromCell="BeginX">`` pair.

Nothing binary is checked into the repository: each helper writes into a
``tmp_path`` the test owns. The PDF is assembled byte by byte here (with a real
cross-reference table) because no PDF *writer* is a dependency of this project —
only a reader is, and adding reportlab just to test would be adding a production
dependency for a test's convenience.
"""

from __future__ import annotations

import base64
import sqlite3
import urllib.parse
import zlib
from pathlib import Path

# --------------------------------------------------------------------------- #
# draw.io
# --------------------------------------------------------------------------- #
DRAWIO_MODEL = """<mxGraphModel dx="800" dy="600" grid="0" page="1">
  <root>
    <mxCell id="0" />
    <mxCell id="1" parent="0" />
    <mxCell id="n1" value="Checkout API" style="rounded=1;whiteSpace=wrap;" vertex="1" parent="1">
      <mxGeometry x="40" y="40" width="160" height="60" as="geometry" />
    </mxCell>
    <mxCell id="n2" value="Orders DB" style="shape=cylinder;whiteSpace=wrap;" vertex="1" parent="1">
      <mxGeometry x="320" y="40" width="160" height="80" as="geometry" />
    </mxCell>
    <mxCell id="n3" value="Kafka" style="shape=queue;" vertex="1" parent="1">
      <mxGeometry x="180" y="220" width="160" height="60" as="geometry" />
    </mxCell>
    <mxCell id="e1" value="POST /orders" style="edgeStyle=none;" edge="1" parent="1" source="n1" target="n2" />
    <mxCell id="e2" value="evento PedidoCriado" style="dashed=1;" edge="1" parent="1" source="n1" target="n3" />
  </root>
</mxGraphModel>"""


def _compress_diagram(model_xml: str) -> str:
    """draw.io's default payload encoding: URL-encode, raw deflate, base64."""
    quoted = urllib.parse.quote(model_xml, safe="~()*!.'")
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    raw = compressor.compress(quoted.encode("utf-8")) + compressor.flush()
    return base64.b64encode(raw).decode("ascii")


def write_drawio(path: Path, *, compressed: bool = True) -> Path:
    """A two-tab .drawio; the first tab is compressed exactly as the app writes it."""
    if compressed:
        payload = _compress_diagram(DRAWIO_MODEL)
        first = f'<diagram id="d1" name="Context">{payload}</diagram>'
    else:
        first = f'<diagram id="d1" name="Context">{DRAWIO_MODEL}</diagram>'
    second = (
        '<diagram id="d2" name="Empty">'
        "<mxGraphModel><root><mxCell id=\"0\"/><mxCell id=\"1\" parent=\"0\"/></root>"
        "</mxGraphModel></diagram>"
    )
    path.write_text(
        '<mxfile host="app.diagrams.net" version="24.7.5">' + first + second + "</mxfile>",
        encoding="utf-8",
    )
    return path


def write_drawio_png(path: Path) -> Path:
    """A PNG carrying its own draw.io source in a tEXt chunk, like the real export."""
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo

    mxfile = (
        '<mxfile host="Electron"><diagram id="d1" name="Context">'
        + DRAWIO_MODEL
        + "</diagram></mxfile>"
    )
    meta = PngInfo()
    meta.add_text("mxfile", urllib.parse.quote(mxfile, safe=""))
    Image.new("RGB", (640, 400), (250, 250, 250)).save(path, "PNG", pnginfo=meta)
    return path


# --------------------------------------------------------------------------- #
# images
# --------------------------------------------------------------------------- #
def write_png(path: Path, size: tuple[int, int] = (2400, 1200)) -> Path:
    """Deliberately larger than MAX_EDGE so the downscale path is exercised."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 100, 700, 400), outline=(20, 20, 20), width=6)
    draw.line((700, 250, 1400, 250), fill=(20, 20, 20), width=6)
    draw.rectangle((1400, 100, 2000, 400), outline=(20, 20, 20), width=6)
    image.save(path, "PNG")
    return path


def write_heic(path: Path, size: tuple[int, int] = (1800, 1200)) -> Path:
    """A real HEIC, the format every iPhone whiteboard photo arrives in."""
    import pillow_heif
    from PIL import Image

    pillow_heif.register_heif_opener()
    image = Image.new("RGB", size, (240, 240, 235))
    heif = pillow_heif.from_pillow(image)
    heif.save(str(path), quality=60)
    return path


def write_multipage_tiff(path: Path) -> Path:
    from PIL import Image

    frames = [Image.new("RGB", (900, 600), shade) for shade in
              ((255, 255, 255), (245, 245, 245), (235, 235, 235))]
    frames[0].save(path, "TIFF", save_all=True, append_images=frames[1:])
    return path


# --------------------------------------------------------------------------- #
# PDF — assembled by hand, with a valid xref table
# --------------------------------------------------------------------------- #
_PAGE_STREAMS = (
    b"BT /F1 18 Tf 72 700 Td (Checkout API) Tj ET\n"
    b"BT /F1 18 Tf 350 700 Td (Orders DB) Tj ET\n"
    b"BT /F1 11 Tf 180 660 Td (POST /orders) Tj ET\n"
    b"2 w 72 660 m 210 660 l S\n"
    b"72 680 m 210 680 l 210 740 l 72 740 l h S\n"
    b"350 680 m 470 680 l 470 740 l 350 740 l h S\n"
    b"72 600 m 470 600 l S\n"
    b"72 560 m 470 560 l S\n",
    b"BT /F1 18 Tf 72 700 Td (Appendix: revision history) Tj ET\n"
    b"BT /F1 11 Tf 72 660 Td (No diagram on this page.) Tj ET\n",
)


def write_pdf(path: Path, pages: int = 2) -> Path:
    """A vector PDF with real text and real path operators, ``pages`` pages long."""
    objects: dict[int, bytes] = {}
    page_ids = [3 + 2 * i for i in range(pages)]
    content_ids = [4 + 2 * i for i in range(pages)]
    font_id = 3 + 2 * pages

    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
    objects[2] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, pages)
    for index, (page_id, content_id) in enumerate(zip(page_ids, content_ids)):
        objects[page_id] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (font_id, content_id)
        )
        stream = _PAGE_STREAMS[min(index, len(_PAGE_STREAMS) - 1)]
        objects[content_id] = b"<< /Length %d >>\nstream\n%s\nendstream" % (
            len(stream), stream,
        )
    objects[font_id] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += b"%d 0 obj\n" % number + objects[number] + b"\nendobj\n"

    xref_at = len(out)
    count = max(objects) + 1
    out += b"xref\n0 %d\n" % count
    out += b"0000000000 65535 f \n"
    for number in range(1, count):
        out += b"%010d 00000 n \n" % offsets.get(number, 0)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        count, xref_at,
    )
    path.write_bytes(bytes(out))
    return path


# --------------------------------------------------------------------------- #
# text and XML formats
# --------------------------------------------------------------------------- #
SVG_SOURCE = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 400" width="800" height="400">
  <rect x="40" y="40" width="200" height="80" fill="none" stroke="#161616"/>
  <text x="60" y="85" font-size="16">Checkout API</text>
  <line x1="240" y1="80" x2="520" y2="80" stroke="#161616"/>
  <rect x="520" y="40" width="200" height="80" fill="none" stroke="#161616"/>
  <text x="540" y="85" font-size="16">Orders DB</text>
  <text x="320" y="70" font-size="12" text-anchor="middle">POST /orders</text>
</svg>
"""

SVG_OUTLINED = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100">
  <path d="M10 10 L90 10 L90 60 L10 60 Z" fill="none" stroke="#000"/>
  <path d="M20 30 c5 -10 15 -10 20 0" fill="#000"/>
</svg>
"""

MERMAID_SOURCE = """flowchart LR
    subgraph Edge
    gw[API Gateway]
    end
    api[Checkout API] -->|POST /orders| db[(Orders DB)]
    api -.-> kafka{{Kafka}}
    gw --> api
    kafka -- consumes --> worker[Fulfilment Worker]
"""

PLANTUML_SOURCE = """@startuml
!theme plain
title Checkout context

component "Checkout API" as api
database "Orders DB" as db
queue "Kafka" as bus
actor Customer

Customer --> api : HTTPS
api --> db : SQL insert
api --> bus : evento PedidoCriado
bus <-- worker : consumes
@enduml
"""

BPMN_SOURCE = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI"
                  xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Defs_1">
  <bpmn:process id="Process_1" isExecutable="true">
    <bpmn:startEvent id="Start_1" name="Order placed" />
    <bpmn:serviceTask id="Task_1" name="Reserve stock" />
    <bpmn:exclusiveGateway id="Gate_1" name="In stock?" />
    <bpmn:endEvent id="End_1" name="Order confirmed" />
    <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Task_1" />
    <bpmn:sequenceFlow id="Flow_2" sourceRef="Task_1" targetRef="Gate_1" />
    <bpmn:sequenceFlow id="Flow_3" name="yes" sourceRef="Gate_1" targetRef="End_1" />
  </bpmn:process>
  <bpmndi:BPMNDiagram id="Diagram_1">
    <bpmndi:BPMNPlane id="Plane_1" bpmnElement="Process_1">
      <bpmndi:BPMNShape id="S1" bpmnElement="Start_1">
        <dc:Bounds x="160" y="100" width="36" height="36" />
      </bpmndi:BPMNShape>
      <bpmndi:BPMNShape id="S2" bpmnElement="Task_1">
        <dc:Bounds x="260" y="80" width="100" height="80" />
      </bpmndi:BPMNShape>
      <bpmndi:BPMNShape id="S3" bpmnElement="Gate_1">
        <dc:Bounds x="420" y="95" width="50" height="50" />
      </bpmndi:BPMNShape>
      <bpmndi:BPMNShape id="S4" bpmnElement="End_1">
        <dc:Bounds x="540" y="100" width="36" height="36" />
      </bpmndi:BPMNShape>
    </bpmndi:BPMNPlane>
  </bpmndi:BPMNDiagram>
</bpmn:definitions>
"""

ARCHIMATE_SOURCE = """<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://www.opengroup.org/xsd/archimate/3.0/"
       xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" identifier="m1">
  <name>Checkout</name>
  <elements>
    <element identifier="e1" xsi:type="ApplicationComponent"><name>Checkout API</name></element>
    <element identifier="e2" xsi:type="DataObject"><name>Orders</name></element>
    <element identifier="e3" xsi:type="BusinessActor"><name>Customer</name></element>
  </elements>
  <relationships>
    <relationship identifier="r1" source="e1" target="e2" xsi:type="Access"><name>writes</name></relationship>
    <relationship identifier="r2" source="e3" target="e1" xsi:type="Serving"/>
  </relationships>
</model>
"""

STARUML_SOURCE = """{
  "_type": "Project",
  "_id": "AAAAAAF",
  "name": "Checkout",
  "ownedElements": [
    {
      "_type": "UMLModel",
      "_id": "M1",
      "name": "Model",
      "ownedElements": [
        {"_type": "UMLClass", "_id": "C1", "name": "CheckoutAPI"},
        {"_type": "UMLClass", "_id": "C2", "name": "OrdersDB"},
        {"_type": "UMLDependency", "_id": "D1", "name": "writes",
         "source": {"$ref": "C1"}, "target": {"$ref": "C2"}},
        {"_type": "UMLClassDiagram", "_id": "V1", "name": "Main"}
      ]
    }
  ]
}
"""


def write_text_sample(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Sparx EA 16+ (.qea) — a real SQLite repository with the two tables that matter
# --------------------------------------------------------------------------- #
def write_qea(path: Path) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE t_object (
                Object_ID INTEGER PRIMARY KEY, Object_Type TEXT, Name TEXT,
                Stereotype TEXT, Package_ID INTEGER);
            CREATE TABLE t_connector (
                Connector_ID INTEGER PRIMARY KEY, Connector_Type TEXT, Name TEXT,
                Start_Object_ID INTEGER, End_Object_ID INTEGER, Direction TEXT);
            CREATE TABLE t_diagramobjects (
                Diagram_ID INTEGER, Object_ID INTEGER,
                RectLeft INTEGER, RectRight INTEGER, RectTop INTEGER, RectBottom INTEGER);
            INSERT INTO t_object VALUES
                (1, 'Component', 'Checkout API', 'service', 2),
                (2, 'Component', 'Orders DB', 'database', 2);
            INSERT INTO t_connector VALUES
                (1, 'Association', 'writes', 1, 2, 'Source -> Destination');
            INSERT INTO t_diagramobjects VALUES
                (1, 1, 10, 210, -10, -90),
                (1, 2, 310, 510, -10, -90);
            """
        )
        connection.commit()
    finally:
        connection.close()
    return path


# --------------------------------------------------------------------------- #
# Visio (.vsdx) — built with the vsdx package's own bundled template
# --------------------------------------------------------------------------- #
def write_vsdx(path: Path) -> Path:
    """Copy the vsdx package's media template and glue two shapes with a connector.

    Using the library's own writer keeps the fixture a *genuine* OPC package with
    a real ``<Connect FromCell="BeginX">`` pair, which is the only part of the
    format this adapter actually depends on.
    """
    import vsdx
    from vsdx import Connect, VisioFile

    media = vsdx.Media()
    with VisioFile(media._media_vsdx.filename) as vis:
        page = vis.pages[0]
        shapes = [s for s in page.all_shapes if (s.text or "").strip()]
        if len(shapes) < 2:
            raise RuntimeError("the bundled vsdx template no longer has two text shapes")
        first, second = shapes[0], shapes[1]
        first.text = "Checkout API"
        second.text = "Orders DB"
        Connect.create(page=page, from_shape=first, to_shape=second)
        vis.save_vsdx(str(path))
    return path
