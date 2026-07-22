#!/usr/bin/env python3
"""
preflight.py — check the vision path BEFORE running the pipeline inside Bob.

    python3 mcp/arch_vision/preflight.py             # credential, region, model
    python3 mcp/arch_vision/preflight.py --catalog   # list multimodal models
    python3 mcp/arch_vision/preflight.py --extract   # real extraction + score vs ground truth

WHY THIS EXISTS
---------------
When vision fails inside Bob, the symptom is always the same error text, and you
cannot tell whether it was the apikey, the region, the model id, the image or the
prompt. Here each one fails on its own, with the fix printed next to it.

--extract is the only test that answers "does vision work in MY catalog?": it runs
a real extraction on the fixture and compares it against the ground truth. Without
a ground truth the success criterion degrades to "the model replied" — and it
always replies.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import httpx  # noqa: F401
except ImportError:
    print("!! httpx is not installed. Run:")
    print("   pip install -r mcp/arch_vision/requirements.txt --break-system-packages")
    print("   (or .venv/bin/pip install -r mcp/arch_vision/requirements.txt)")
    sys.exit(1)

import server as S  # loads .env and the config exactly like the real server does

ROOT = S.PROJECT_ROOT
# The fixture keeps its Portuguese file name on purpose: the labels drawn inside
# the PNG are Portuguese, and an English name would lie about the file.
FIXTURE = ROOT / ".arch/intake/inbox/exemplo-rascunho.png"
GABARITO = ROOT / "tests/ground-truth-example.json"

OK, BAD, WARN = "  \033[32mOK\033[0m  ", "  \033[31mFAIL\033[0m  ", "  \033[33mwarn\033[0m  "


# Preference for READING DIAGRAMS (best first). Substring, not exact id: the
# catalog varies the suffix (-fp8, -int4, dates) across regions and versions.
#
# Source: the IBM doc "Third-party foundation models" — llama-4-maverick and
# llama-4-scout are multimodal (MoE, early fusion), "optimized for visual
# recognition, image reasoning and captioning". Maverick has 128 experts against
# Scout's 16.
PREFERENCIA = [
    ("llama-4-maverick", "Llama 4 Maverick — natively multimodal, 128 experts; the best "
                         "one for a dense diagram"),
    ("llama-4-scout",    "Llama 4 Scout — natively multimodal, 16 experts; lighter"),
    ("mistral-small-3-1", "Mistral Small 3.1 — multimodal; fine for a simple diagram"),
    ("mistral-large-251", "Mistral Large 3 — multimodal (check whether it is deprecated)"),
    ("llama-3-2-90b-vision", "Llama 3.2 90B Vision — previous generation"),
    ("pixtral",          "Pixtral — multimodal"),
    ("granite-vision",   "Granite Vision — lightweight, focused on documents/charts"),
    ("llama-3-2-11b-vision", "Llama 3.2 11B Vision — small; gets arrow direction wrong"),
]


def catalogo() -> list[dict]:
    """Every model in the project, with whatever metadata the API returns."""
    import httpx
    r = httpx.get(f"{S.WATSONX_URL}/ml/v1/foundation_model_specs"
                  f"?version={S.API_VERSION}&limit=200",
                  headers={"Authorization": f"Bearer {S._iam_token()}"}, timeout=60)
    r.raise_for_status()
    return r.json().get("resources", [])


def _pixel_png_b64() -> str:
    """Minimal PNG (8x8 red square) used to probe image support.

    Negligible cost and an unambiguous answer: a model that accepts images
    replies; a text-only model returns 400/404.
    """
    import base64
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (220, 20, 20)).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def aceita_imagem(model_id: str) -> tuple[bool, str]:
    """EMPIRICAL probe: send an 8x8 image and see whether the model accepts it.

    WHY probe instead of filtering by name: 'llama-4-maverick' is multimodal and
    does NOT have 'vision' in its id. Filtering by name drops precisely the best
    model — that was the bug in this function. And the catalog metadata does not
    always carry the capability. The only source of truth is the endpoint itself.
    """
    import httpx
    body = {
        "model_id": model_id,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Reply only: OK"},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{_pixel_png_b64()}"}},
        ]}],
        "max_tokens": 5,
    }
    if S.WATSONX_PROJECT_ID:
        body["project_id"] = S.WATSONX_PROJECT_ID
    else:
        body["space_id"] = S.WATSONX_SPACE_ID
    try:
        r = httpx.post(f"{S.WATSONX_URL}/ml/v1/text/chat?version={S.API_VERSION}",
                       headers={"Authorization": f"Bearer {S._iam_token()}",
                                "Content-Type": "application/json"},
                       json=body, timeout=90)
    except Exception as e:
        return False, f"network error: {type(e).__name__}"
    if r.status_code == 200:
        return True, "accepted the image"
    detalhe = r.text[:120].replace("\n", " ")
    return False, f"HTTP {r.status_code}: {detalhe}"


def vision_models(probe: bool = False) -> list[str]:
    """Multimodal models. With probe=True it really tests instead of guessing."""
    ids = [m.get("model_id", "") for m in catalogo()]
    if not probe:
        # No probe: metadata + preference. It can be wrong — use --probe for certainty.
        out = []
        for m in catalogo():
            fns = {f.get("id") for f in m.get("functions", [])}
            mid = m.get("model_id", "")
            if "image_chat" in fns or any(k in mid for k, _ in PREFERENCIA):
                out.append(mid)
        return out
    return [i for i in ids if aceita_imagem(i)[0]]


def ordena_por_preferencia(ids: list[str]) -> list[str]:
    def rank(mid: str) -> int:
        for i, (chave, _) in enumerate(PREFERENCIA):
            if chave in mid:
                return i
        return len(PREFERENCIA)
    return sorted(ids, key=rank)


def check() -> int:
    print(f"\nroot     : {ROOT}")
    print(f".env     : {(Path(S.SERVER_DIR) / '.env')} "
          f"({'present' if (Path(S.SERVER_DIR) / '.env').exists() else 'MISSING'})")
    print(f"region   : {S.WATSONX_URL}")
    print(f"model    : {S.VISION_MODEL_ID}\n")

    if not S.WATSONX_APIKEY:
        print(BAD + "WATSONX_APIKEY is empty")
        print("        cp mcp/arch_vision/.env.example mcp/arch_vision/.env  and fill it in")
        return 1
    print(OK + "WATSONX_APIKEY present")

    if not (S.WATSONX_PROJECT_ID or S.WATSONX_SPACE_ID):
        print(BAD + "WATSONX_PROJECT_ID (or SPACE_ID) is empty")
        return 1
    print(OK + "WATSONX_PROJECT_ID present")

    try:
        S._iam_token()
        print(OK + "IAM accepted the apikey")
    except Exception as e:
        print(BAD + f"IAM refused it: {e}")
        print("        invalid apikey, or no access to the project")
        return 1

    try:
        ids = [m.get("model_id", "") for m in catalogo()]
    except Exception as e:
        print(BAD + f"could not list the catalog: {e}")
        print(f"        is the region {S.WATSONX_URL} the right one?")
        return 1
    print(OK + f"catalog answered: {len(ids)} model(s) in the project")

    if S.VISION_MODEL_ID not in ids:
        print(BAD + f"'{S.VISION_MODEL_ID}' is NOT in your catalog -> 404 when used")
        print("        run:   python3 mcp/arch_vision/preflight.py --probe --set")
        print("        it tests every model with an image and writes the best one to .env")
        return 1
    print(OK + f"'{S.VISION_MODEL_ID}' exists in the catalog")

    ok, motivo = aceita_imagem(S.VISION_MODEL_ID)
    if not ok:
        print(BAD + f"'{S.VISION_MODEL_ID}' does NOT accept images ({motivo})")
        print("        being in the catalog does not mean being multimodal.")
        print("        run:   python3 mcp/arch_vision/preflight.py --probe --set")
        return 1
    print(OK + f"'{S.VISION_MODEL_ID}' accepted a test image")

    print("\nVision path is ready. Run --extract to measure the quality.\n")
    return 0


def extract() -> int:
    """Real extraction on the fixture, scored against the ground truth."""
    if not FIXTURE.exists() or not GABARITO.exists():
        print(BAD + "fixture or ground truth missing")
        return 1

    gt = json.loads(GABARITO.read_text(encoding="utf-8"))
    # The label values in the ground truth are deliberately Portuguese — they are
    # drawn as pixels inside the PNG. Never translate them here or there.
    esperados = {c["suggested_id"]: c["label"] for c in gt["components"]}

    print(f"\nextracting {FIXTURE.name} with {S.VISION_MODEL_ID} (source_kind=napkin)...")
    print("(one real call; takes a few seconds)\n")

    import asyncio
    raw = asyncio.run(S.arch_vision_extract_architecture(
        image_path=str(FIXTURE), source_kind="napkin"))
    data = json.loads(raw)
    if "error" in data:
        print(BAD + data["error"])
        return 1

    comps = data.get("components", [])
    conns = data.get("connections", [])
    unks = data.get("unknowns", [])
    print(f"found: {len(comps)} components, {len(conns)} connections, {len(unks)} unknowns")
    print(f"overall confidence: {data.get('overall_confidence')}\n")

    for c in comps:
        print(f"    {c.get('name'):22s} kind={c.get('kind'):10s} conf={c.get('confidence')}")
    print()
    for c in conns:
        print(f"    {c.get('from')} -> {c.get('to')}  "
              f"[{c.get('protocol')}/{c.get('sync')}] conf={c.get('confidence')}")
    print()

    falhas = 0
    print("--- score against the ground truth ---")

    # 1. count
    if len(comps) == gt["expected_components"]:
        print(OK + f"{gt['expected_components']} components, as expected")
    else:
        print(WARN + f"expected {gt['expected_components']}, found {len(comps)}")

    # 2. hallucination — the most dangerous error, because it is plausible
    nomes_gt = {r.lower() for r in esperados.values()}
    inventados = [c["name"] for c in comps
                  if not any(p in c.get("name", "").lower()
                             for r in nomes_gt for p in r.split())]
    if inventados:
        print(BAD + f"components that are NOT in the drawing: {inventados}")
        print("        pattern hallucination: the model completed the drawing")
        falhas += 1
    else:
        print(OK + "no invented components")

    # 3. THE TRAP: the line with no arrowhead
    # 'notific' matches the label "Notificacao" drawn inside the PNG — do not
    # translate it. It is how we detect that the model recorded the arrowhead-less
    # line as an unknown instead of guessing its direction.
    achou_ambigua = any(
        c.get("sync") == "unknown" or c.get("protocol") == "unknown" for c in conns
    ) or any("notific" in json.dumps(u).lower() or "?" in json.dumps(u) for u in unks)
    if achou_ambigua:
        print(OK + "the line with no arrowhead became an unknown (it did not guess the direction)")
    else:
        print(BAD + "asserted a direction on the line with NO arrowhead")
        print("        that information does NOT exist in the drawing — the model made it up.")
        print("        usually the vision model is simply too small; try the 90B one.")
        falhas += 1

    # 4. calibration
    if (data.get("overall_confidence") or 0) > 0.95:
        print(WARN + "confidence >0.95 on a hand-drawn sketch: calibration is far too optimistic")

    print()
    if falhas:
        print(f"\033[31m{falhas} critical criterion(s) failed.\033[0m "
              f"See tests/ground-truth-example.json.\n")
        return 1
    print("\033[32mVision passed the ground truth. You can run the pipeline in Bob.\033[0m\n")
    return 0


def probe(gravar: bool) -> int:
    """Tests EVERY model in the catalog with a real image. No guessing."""
    try:
        ids = [m.get("model_id", "") for m in catalogo()]
    except Exception as e:
        print(BAD + f"could not list the catalog: {e}")
        return 1

    print(f"\nprobing {len(ids)} model(s) from YOUR project with an 8x8 image.")
    print("(being in the catalog != accepting an image; the only certainty is testing)\n")

    multimodais = []
    for mid in ids:
        ok, motivo = aceita_imagem(mid)
        if ok:
            print(f"  \033[32mIMAGE OK \033[0m  {mid}")
            multimodais.append(mid)
        else:
            print(f"  \033[90mtext only\033[0m  {mid}   ({motivo[:60]})")

    if not multimodais:
        print("\n" + BAD + "no model in your project accepts images.")
        print("        The vision path will not work here. Two ways out:")
        print("        1. Enable a multimodal model in the watsonx project")
        print("           (llama-4-maverick is the best; llama-4-scout is lighter)")
        print("        2. Use the deterministic path: export .drawio/.puml/.mmd")
        print("           instead of PNG. It is exact, free and needs no model.")
        return 1

    ordenados = ordena_por_preferencia(multimodais)
    melhor = ordenados[0]
    print(f"\n{len(ordenados)} model(s) accept images, in order of preference "
          f"for READING DIAGRAMS:\n")
    for i, mid in enumerate(ordenados):
        nota = next((d for k, d in PREFERENCIA if k in mid), "multimodal")
        marca = "->" if i == 0 else "  "
        print(f"  {marca} {mid}")
        print(f"       {nota}")

    if not gravar:
        print(f"\nTo adopt the best one:")
        print(f"  python3 mcp/arch_vision/preflight.py --probe --set")
        return 0

    env = Path(S.SERVER_DIR) / ".env"
    if not env.exists():
        print(BAD + f"{env} does not exist. cp mcp/arch_vision/.env.example {env}")
        return 1
    linhas, achou = [], False
    for ln in env.read_text(encoding="utf-8").splitlines():
        if ln.strip().startswith("WATSONX_VISION_MODEL_ID="):
            linhas.append(f"WATSONX_VISION_MODEL_ID={melhor}")
            achou = True
        else:
            linhas.append(ln)
    if not achou:
        linhas.append(f"WATSONX_VISION_MODEL_ID={melhor}")
    env.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    print(f"\n\033[32mwritten to .env:\033[0m WATSONX_VISION_MODEL_ID={melhor}")
    print("\nThe MCP server reads .env at boot: restart the server in Bob")
    print("(Settings -> MCP -> restart) so it picks up the new model.")
    print("\nNow run:  python3 mcp/arch_vision/preflight.py --extract")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", action="store_true", help="list the project catalog")
    ap.add_argument("--probe", action="store_true",
                    help="test EVERY model with an image and rank them by preference")
    ap.add_argument("--set", action="store_true",
                    help="with --probe: write the best model to .env")
    ap.add_argument("--extract", action="store_true",
                    help="real extraction + score vs the ground truth")
    a = ap.parse_args()

    if a.catalog:
        try:
            for m in catalogo():
                print(" ", m.get("model_id"))
            return 0
        except Exception as e:
            print(BAD + f"{e}")
            return 1

    if a.probe:
        return probe(gravar=a.set)

    rc = check()
    if rc or not a.extract:
        return rc
    return extract()


if __name__ == "__main__":
    sys.exit(main())
