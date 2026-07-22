"""Export tests, run against the real runs on disk.

The point of this file is not coverage, it is proof: the archive is opened with
``zipfile`` after being produced, every entry is read back, and the CRC of each
is checked. A ZIP that streams but does not open is the failure mode this whole
module exists to avoid, and only an actual extraction catches it.

Run with:  /opt/anaconda3/bin/python -m pytest webapp/tests/test_export.py -v
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from app.config import load_settings
from app.errors import NotFound
from app.eventlog import EventLogRegistry
from app.export import (
    build_export,
    iter_zip,
    safe_arcname,
    safe_name,
)
from app.store import RunStore

WEBAPP = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def store() -> RunStore:
    settings = load_settings()
    return RunStore(settings, EventLogRegistry(settings.runs_root))


@pytest.fixture(scope="module")
def settings():
    return load_settings()


def _existing_run_ids() -> list[str]:
    runs = WEBAPP / "runs"
    return sorted(
        entry.name for entry in runs.iterdir()
        if entry.is_dir() and (entry / "run.json").is_file()
    )


def _zip_bytes(plan) -> bytes:
    return b"".join(iter_zip(plan))


# --------------------------------------------------------------------------- #
# name safety
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("../../etc/passwd", "etc/passwd"),
        ("/absolute/path.txt", "absolute/path.txt"),
        ("C:\\Windows\\system32\\cmd.exe", "Windows/system32/cmd.exe"),
        ("a/../../b", "a/b"),
        # "...." strips to nothing once leading/trailing dots go, so it becomes
        # the fallback rather than any kind of relative reference.
        ("....//....//x", "file/file/x"),
        ("normal/dir/file.py", "normal/dir/file.py"),
        ("image (24).png", "image (24).png"),
        ("serviço/agênte.yaml", "serviço/agênte.yaml"),
    ],
)
def test_safe_arcname_never_escapes(raw: str, expected: str) -> None:
    assert safe_arcname(raw) == expected


def test_safe_arcname_has_no_traversal_component() -> None:
    for hostile in ("../x", "..\\x", "a/../../../../etc/shadow", "//////", "..", "."):
        name = safe_arcname(hostile)
        assert ".." not in Path(name).parts
        assert not name.startswith("/")


def test_safe_name_handles_windows_reserved_and_control_chars() -> None:
    assert safe_name("con.py") == "_con.py"
    assert safe_name("a\x00b") == "a_b"
    assert safe_name("   ") == "file"
    assert safe_name("..") == "file"


def test_leading_dot_survives_sanitisation() -> None:
    """A stripped leading dot turned a credential file into an innocent one.

    ``.env`` became ``env``: still full of secrets, no longer recognisable as a
    dotfile by anything downstream. This is the regression guard for that.
    """
    assert safe_name(".env") == ".env"
    assert safe_name(".env.example") == ".env.example"
    assert safe_arcname("a/.gitignore") == "a/.gitignore"
    assert safe_name("trailing.  ") == "trailing"


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name,secret",
    [
        (".env", True),
        (".env.local", True),
        ("prod.env", True),
        ("service-account.json", True),
        ("id_rsa", True),
        ("server.pem", True),
        ("tls.key", True),
        (".netrc", True),
        (".env.example", False),
        (".env.template", False),
        ("main.py", False),
        ("docker-compose.yml", False),
        ("README.md", False),
    ],
)
def test_is_secret_file(name: str, secret: bool) -> None:
    from app.export import is_secret_file

    assert is_secret_file(name) is secret


# --------------------------------------------------------------------------- #
# the real archive
# --------------------------------------------------------------------------- #


def test_full_export_of_every_run_on_disk(settings, store) -> None:
    run_ids = _existing_run_ids()
    assert run_ids, "no runs on disk; this test needs webapp/runs/<run_id>/run.json"

    for run_id in run_ids:
        plan = build_export(settings, store, run_id, kind="full")
        blob = _zip_bytes(plan)

        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            assert archive.testzip() is None, f"{run_id}: a member failed its CRC"
            names = archive.namelist()

            # Every entry is under the single run-named root, and nothing escapes.
            root = f"arch2code-{run_id}/"
            for name in names:
                assert name.startswith(root), f"{run_id}: {name} is outside {root}"
                assert ".." not in Path(name).parts
                assert not name.startswith("/")

            assert f"{root}MANIFEST.md" in names
            manifest = archive.read(f"{root}MANIFEST.md").decode("utf-8")
            assert run_id in manifest
            assert "## 11. Everything in this archive" in manifest

            # Audit trail: run.json and events.jsonl are always there, and both
            # must read back as what they claim to be.
            assert f"{root}audit/run.json" in names
            assert f"{root}audit/events.jsonl" in names
            import json

            state = json.loads(archive.read(f"{root}audit/run.json"))
            assert state["run_id"] == run_id
            events = archive.read(f"{root}audit/events.jsonl").decode("utf-8")
            assert json.loads(events.splitlines()[0])["run_id"] == run_id

            # Every planned entry that still exists is actually in the archive.
            for entry in plan.entries:
                if entry.data is not None or (
                    entry.source is not None and entry.source.is_file()
                ):
                    assert entry.arcname in names


def test_full_export_carries_the_diagram(settings, store) -> None:
    run_id = _existing_run_ids()[0]
    plan = build_export(settings, store, run_id, kind="full")
    with zipfile.ZipFile(io.BytesIO(_zip_bytes(plan))) as archive:
        names = archive.namelist()
        originals = [n for n in names if "/diagram/original/" in n]
        assert originals, "the original drawing must be in the archive"
        # The bytes must match the upload's recorded digest.
        import hashlib
        import json

        state = json.loads(archive.read(f"arch2code-{run_id}/audit/run.json"))
        digest = hashlib.sha256(archive.read(originals[0])).hexdigest()
        assert digest == state["upload"]["sha256"]


def test_manifest_names_the_model_and_the_confidence(settings, store) -> None:
    """The MANIFEST is the product; a vision run must yield a readable one."""
    run_id = next(
        (r for r in _existing_run_ids()
         if (WEBAPP / "runs" / r / "vision" / "extraction.json").is_file()),
        None,
    )
    if run_id is None:
        pytest.skip("no run with an extraction on disk")

    plan = build_export(settings, store, run_id, kind="full")
    with zipfile.ZipFile(io.BytesIO(_zip_bytes(plan))) as archive:
        manifest = archive.read(f"arch2code-{run_id}/MANIFEST.md").decode("utf-8")

    for heading in (
        "## 2. The source diagram",
        "## 3. What read the drawing, and how sure it was",
        "## 5. Assumptions that were made",
        "## 6. Gaps left open",
        "## 10. Deployment",
    ):
        assert heading in manifest

    import json

    extraction = json.loads(
        (WEBAPP / "runs" / run_id / "vision" / "extraction.json").read_text("utf-8")
    )
    model = extraction.get("_provenance", {}).get("model")
    if model:
        assert model in manifest, "the MANIFEST must name the model that read the drawing"
    for component in extraction.get("components", []):
        assert str(component["id"]) in manifest


def test_code_export_refuses_a_run_with_no_code(settings, store) -> None:
    run_id = _existing_run_ids()[0]
    with pytest.raises(NotFound) as excinfo:
        build_export(settings, store, run_id, kind="code")
    error = excinfo.value
    assert error.code == "no_generated_code"
    assert error.remedy and "/export" in error.remedy


def test_unknown_run_is_refused_with_a_remedy(settings, store) -> None:
    with pytest.raises(NotFound) as excinfo:
        build_export(settings, store, "20990101-0000-does-not-exist", kind="full")
    assert excinfo.value.remedy


def test_preview_matches_the_archive(settings, store) -> None:
    run_id = _existing_run_ids()[0]
    plan = build_export(settings, store, run_id, kind="full")
    summary = plan.summary()
    assert summary["entry_count"] == len(plan.entries)
    with zipfile.ZipFile(io.BytesIO(_zip_bytes(plan))) as archive:
        names = set(archive.namelist())
    for entry in summary["entries"]:
        if entry["arcname"].endswith(".md") or entry["arcname"] in names:
            continue
        # Only a file that disappeared between planning and streaming may differ.
        assert not Path(entry["arcname"]).exists()


# --------------------------------------------------------------------------- #
# generated code, synthesized where the repo has none for a live run
# --------------------------------------------------------------------------- #


def test_code_export_of_a_synthetic_build_tree(tmp_path, settings, store) -> None:
    """Plant a build tree for a real run id, then export only the code.

    Uses the run ids the repo already has under .arch/build/ by pointing the
    exporter at a temporary project root, so no file in the repository is
    touched and nothing is left behind.
    """
    import dataclasses
    import json
    import shutil

    run_id = _existing_run_ids()[0]

    project = tmp_path / "project"
    build = project / ".arch" / "build" / run_id
    build.mkdir(parents=True)
    (build / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (build / "agents").mkdir()
    (build / "agents" / "agent.yaml").write_text("name: agent\n", encoding="utf-8")
    # Noise that must NOT be exported.
    (build / "__pycache__").mkdir()
    (build / "__pycache__" / "main.cpython-313.pyc").write_bytes(b"\x00\x01")
    (build / ".DS_Store").write_bytes(b"\x00")
    # A manifest that declares a file outside .arch/build, plus a hostile one.
    outside = project / "agents" / "svc"
    outside.mkdir(parents=True)
    (outside / "app.py").write_text("# generated\n", encoding="utf-8")
    (build / "manifest.json").write_text(
        json.dumps({
            "_meta": {"run_id": run_id, "root": "agents/svc"},
            "components": {
                "svc": {"files": ["agents/svc/app.py"], "evidence": "bbox [0,0,1,1]"},
                "ghost": {"files": ["../../../../etc/passwd"]},
                "absent": {"files": ["agents/svc/never_written.py"]},
            },
        }),
        encoding="utf-8",
    )

    patched = dataclasses.replace(settings, project_root=project, bob_cwd=project)

    # The run state still points at the real repo; override it the way a run
    # executed elsewhere would.
    state = store.load(run_id)
    original_bob_cwd = state.bob_cwd
    state_path = store.run_dir(run_id) / "run.json"
    backup = state_path.read_text(encoding="utf-8")
    try:
        state.bob_cwd = str(project)
        state_path.write_text(state.model_dump_json(indent=2), encoding="utf-8")

        plan = build_export(patched, store, run_id, kind="code")
        with zipfile.ZipFile(io.BytesIO(_zip_bytes(plan))) as archive:
            assert archive.testzip() is None
            names = set(archive.namelist())

        assert "main.py" in names
        assert "agents/agent.yaml" in names
        assert "agents/svc/app.py" in names
        assert archive_has_no_noise(names)
        assert not any("passwd" in n for n in names), "traversal escaped into the zip"

        joined = " ".join(plan.omissions)
        assert "etc/passwd" in joined, "the traversal attempt must be reported"
        assert "never_written.py" in joined, "a declared-but-absent file must be reported"

        # And the full export of the same run now contains that code under code/.
        full = build_export(patched, store, run_id, kind="full")
        with zipfile.ZipFile(io.BytesIO(_zip_bytes(full))) as archive:
            names = set(archive.namelist())
        root = f"arch2code-{run_id}/"
        assert f"{root}code/main.py" in names
        assert f"{root}code/agents/svc/app.py" in names
        manifest = None
        with zipfile.ZipFile(io.BytesIO(_zip_bytes(full))) as archive:
            manifest = archive.read(f"{root}MANIFEST.md").decode("utf-8")
        assert "## 9. The generated code" in manifest
        assert "agents/svc/app.py" in manifest
    finally:
        state_path.write_text(backup, encoding="utf-8")
        assert store.load(run_id).bob_cwd == original_bob_cwd
        shutil.rmtree(project, ignore_errors=True)


def test_a_real_dotenv_is_never_archived(tmp_path, settings, store) -> None:
    """The scaffold writes a live .env beside .env.example. It must not ship.

    Uses the real API key file shape, with a fake value, in a temporary project
    root. The .example sibling MUST still be exported — without it nobody can
    run the generated code.
    """
    import dataclasses
    import json
    import shutil

    run_id = _existing_run_ids()[0]
    project = tmp_path / "project"
    tree = project / "agents" / "svc"
    tree.mkdir(parents=True)
    (tree / "main.py").write_text("print('x')\n", encoding="utf-8")
    (tree / ".env").write_text("WATSONX_APIKEY=REAL-SECRET-VALUE\n", encoding="utf-8")
    (tree / ".env.example").write_text("WATSONX_APIKEY=\n", encoding="utf-8")
    (tree / "tls.key").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")

    build = project / ".arch" / "build" / run_id
    build.mkdir(parents=True)
    (build / ".env").write_text("WATSONX_APIKEY=ANOTHER-SECRET\n", encoding="utf-8")
    (build / "manifest.json").write_text(
        json.dumps({"_meta": {"run_id": run_id, "root": "agents/svc"}}), encoding="utf-8"
    )

    patched = dataclasses.replace(settings, project_root=project, bob_cwd=project)
    state_path = store.run_dir(run_id) / "run.json"
    backup = state_path.read_text(encoding="utf-8")
    try:
        state = store.load(run_id)
        state.bob_cwd = str(project)
        state_path.write_text(state.model_dump_json(indent=2), encoding="utf-8")

        for kind in ("code", "full"):
            plan = build_export(patched, store, run_id, kind=kind)
            blob = _zip_bytes(plan)
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                names = archive.namelist()
                for name in names:
                    assert Path(name).name != ".env", f"{kind}: a .env was archived"
                    assert Path(name).name != "env", f"{kind}: a .env was archived as env"
                    assert not name.endswith("tls.key"), f"{kind}: a private key was archived"
                assert any(n.endswith(".env.example") for n in names), \
                    f"{kind}: the .env.example template must still be exported"
                assert any(n.endswith("main.py") for n in names)

            # And the secret value itself is nowhere in the bytes on the wire.
            assert b"REAL-SECRET-VALUE" not in blob
            assert b"ANOTHER-SECRET" not in blob
            # The withholding is stated, never silent.
            assert any(".env" in note for note in plan.omissions)

        # The code-only archive has no MANIFEST, so it carries the notes instead.
        plan = build_export(patched, store, run_id, kind="code")
        with zipfile.ZipFile(io.BytesIO(_zip_bytes(plan))) as archive:
            assert "EXPORT-NOTES.md" in archive.namelist()
            notes = archive.read("EXPORT-NOTES.md").decode("utf-8")
        assert ".env" in notes
    finally:
        state_path.write_text(backup, encoding="utf-8")
        shutil.rmtree(project, ignore_errors=True)


def archive_has_no_noise(names: set[str]) -> bool:
    return not any(
        n.endswith(".pyc") or "__pycache__" in n or n.endswith(".DS_Store")
        for n in names
    )


def test_deploy_readme_from_a_target_profile(tmp_path, settings, store) -> None:
    """A profile that declares deploy steps produces README.md; one that does not, does not."""
    import dataclasses
    import json
    import shutil

    yaml = pytest.importorskip("yaml")

    run_id = _existing_run_ids()[0]
    project = tmp_path / "project"
    build = project / ".arch" / "build" / run_id
    build.mkdir(parents=True)
    (build / "main.py").write_text("print('x')\n", encoding="utf-8")
    (build / "manifest.json").write_text(
        json.dumps({"_meta": {"run_id": run_id, "target": "container-microservice"}}),
        encoding="utf-8",
    )
    profile_dir = project / "targets" / "container-microservice"
    profile_dir.mkdir(parents=True)
    (profile_dir / "target.yaml").write_text(
        yaml.safe_dump({
            "id": "container-microservice",
            "name": "Container microservice (compose)",
            "deploy": {
                "summary": "Runs the whole stack locally with Docker Compose.",
                "prerequisites": ["Docker 24+"],
                "steps": [{"title": "Bring it up", "run": "docker compose up -d"}],
                "docs": ["https://docs.docker.com/compose/"],
            },
            "validate": {"checks": [{"name": "compose", "cmd": "docker compose config -q"}]},
        }),
        encoding="utf-8",
    )

    patched = dataclasses.replace(settings, project_root=project, bob_cwd=project)
    state_path = store.run_dir(run_id) / "run.json"
    backup = state_path.read_text(encoding="utf-8")
    try:
        state = store.load(run_id)
        state.bob_cwd = str(project)
        state_path.write_text(state.model_dump_json(indent=2), encoding="utf-8")

        plan = build_export(patched, store, run_id, kind="full")
        with zipfile.ZipFile(io.BytesIO(_zip_bytes(plan))) as archive:
            names = set(archive.namelist())
            root = f"arch2code-{run_id}/"
            assert f"{root}README.md" in names
            readme = archive.read(f"{root}README.md").decode("utf-8")
            manifest = archive.read(f"{root}MANIFEST.md").decode("utf-8")

        assert "docker compose up -d" in readme
        assert "Container microservice (compose)" in readme
        assert "docker compose config -q" in readme          # offline checks
        assert "Container microservice (compose)" in manifest

        # code-only export carries the deploy README too.
        code_plan = build_export(patched, store, run_id, kind="code")
        with zipfile.ZipFile(io.BytesIO(_zip_bytes(code_plan))) as archive:
            assert "README.md" in archive.namelist()
    finally:
        state_path.write_text(backup, encoding="utf-8")
        shutil.rmtree(project, ignore_errors=True)


# --------------------------------------------------------------------------- #
# streaming behaviour
# --------------------------------------------------------------------------- #


def test_zip_is_produced_incrementally(settings, store) -> None:
    """The archive must arrive in several chunks, not one buffered blob."""
    run_id = _existing_run_ids()[0]
    plan = build_export(settings, store, run_id, kind="full")
    chunks = [c for c in iter_zip(plan) if c]
    assert len(chunks) > 1, "iter_zip produced a single chunk; it is not streaming"
    assert b"".join(chunks)[:2] == b"PK"


def test_http_endpoints_serve_a_valid_zip() -> None:
    """End to end through FastAPI, with the router actually mounted."""
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from app.main import create_app

    run_id = _existing_run_ids()[0]

    # The context manager is what runs the lifespan; without it app.state has no
    # store and every route answers 500.
    with fastapi_testclient.TestClient(create_app()) as client:
        response = client.get(f"/api/runs/{run_id}/export")
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "application/zip"
        assert f"arch2code-{run_id}.zip" in response.headers["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert archive.testzip() is None
            assert f"arch2code-{run_id}/MANIFEST.md" in archive.namelist()

        code = client.get(f"/api/runs/{run_id}/export/code")
        assert code.status_code == 404, "a run with no code must be refused"
        assert code.json()["code"] == "no_generated_code"
        assert code.json()["remedy"]

        preview = client.get(f"/api/runs/{run_id}/export/preview")
        assert preview.status_code == 200
        assert preview.json()["run_id"] == run_id

        missing = client.get("/api/runs/20990101-0000-nope/export")
        assert missing.status_code == 404
        assert missing.json()["remedy"]

        bad_id = client.get("/api/runs/..%2F..%2Fetc/export")
        assert bad_id.status_code in (400, 404)


# --------------------------------------------------------------------------- #
# the project export
#
# The point of these is the ATTRIBUTION, not the zip mechanics: a project
# archive is only worth anything if the reader can tell why each file is in it.
# --------------------------------------------------------------------------- #


def _plant_project(tmp_path, settings, store, run_id, *, with_baseline: bool):
    """Build a temporary project root, snapshot it, then write into it.

    Returns (patched_settings, project_root, restore) where restore() puts the
    real run state and the real repository back exactly as they were.
    """
    import dataclasses
    import json
    import shutil
    import time

    from app import projectdiff

    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "existing.py").write_text("before\n", encoding="utf-8")
    (project / "leave-me.md").write_text("untouched\n", encoding="utf-8")

    run_dir = store.run_dir(run_id)
    snapshot_path = run_dir / projectdiff.SNAPSHOT_FILENAME
    if with_baseline:
        snapshot = projectdiff.take_snapshot(
            project,
            excluded=projectdiff.excluded_for(
                project, settings.runs_root, settings.uploads_root
            ),
        )
        projectdiff.write_snapshot(snapshot_path, snapshot)

    # mtime granularity: without this a same-second rewrite of the same length
    # would look unchanged, which is the documented limitation of the method.
    time.sleep(0.01)

    # What the run "wrote": inside the build directory, outside it, and an edit.
    build = project / ".arch" / "build" / run_id
    build.mkdir(parents=True)
    (build / "manifest.json").write_text(
        json.dumps({"components": {"svc": {"files": ["agents/svc/app.py"]}}}),
        encoding="utf-8",
    )
    outside = project / "agents" / "svc"
    outside.mkdir(parents=True)
    (outside / "app.py").write_text("# generated\n", encoding="utf-8")
    (outside / ".env").write_text("WATSONX_APIKEY=live\n", encoding="utf-8")
    (outside / ".env.example").write_text("WATSONX_APIKEY=\n", encoding="utf-8")
    (project / "src" / "existing.py").write_text("after the run\n", encoding="utf-8")
    (project / "__pycache__").mkdir()
    (project / "__pycache__" / "x.cpython-313.pyc").write_bytes(b"\x00")

    patched = dataclasses.replace(settings, project_root=project, bob_cwd=project)

    state_path = run_dir / "run.json"
    backup = state_path.read_text(encoding="utf-8")
    state = store.load(run_id)
    state.bob_cwd = str(project)
    state_path.write_text(state.model_dump_json(indent=2), encoding="utf-8")

    def restore() -> None:
        state_path.write_text(backup, encoding="utf-8")
        snapshot_path.unlink(missing_ok=True)
        shutil.rmtree(project, ignore_errors=True)

    return patched, project, restore


def test_project_export_carries_what_the_run_wrote_outside_the_build_dir(
    tmp_path, settings, store
) -> None:
    run_id = _existing_run_ids()[0]
    patched, _project, restore = _plant_project(
        tmp_path, settings, store, run_id, with_baseline=True
    )
    try:
        plan = build_export(patched, store, run_id, kind="project")
        with zipfile.ZipFile(io.BytesIO(_zip_bytes(plan))) as archive:
            assert archive.testzip() is None
            names = set(archive.namelist())
            root = f"arch2code-{run_id}-project/"
            manifest = archive.read(f"{root}MANIFEST.md").decode("utf-8")

        # At their real paths, not hoisted, and not only the build directory.
        assert f"{root}project/agents/svc/app.py" in names
        assert f"{root}project/.arch/build/{run_id}/manifest.json" in names
        assert f"{root}project/src/existing.py" in names, "a modified file is a product"
        # Untouched files are not the run's work and must not bloat the archive.
        assert f"{root}project/leave-me.md" not in names
        assert archive_has_no_noise(names)
        # A live credential never leaves; its template must.
        assert not any(n.endswith("/.env") for n in names)
        assert f"{root}project/agents/svc/.env.example" in names
        # The audit trail travels with it.
        assert f"{root}audit/run.json" in names

        assert plan.attribution["has_baseline"] is True
        assert plan.attribution["created"] >= 3
        assert plan.attribution["modified"] >= 1

        assert "## 12. How this archive decided what the run produced" in manifest
        assert "Filesystem snapshot diff" in manifest
        assert "It cannot tell who changed a file" in manifest, (
            "the limitations of the heuristic are the part the reader needs"
        )
        assert "Did not exist when the run started" in manifest
    finally:
        restore()


def test_project_export_without_a_baseline_says_so_loudly(
    tmp_path, settings, store
) -> None:
    """A run started before fs-snapshot.json existed must not look complete."""
    run_id = _existing_run_ids()[0]
    patched, _project, restore = _plant_project(
        tmp_path, settings, store, run_id, with_baseline=False
    )
    try:
        plan = build_export(patched, store, run_id, kind="project")
        with zipfile.ZipFile(io.BytesIO(_zip_bytes(plan))) as archive:
            names = set(archive.namelist())
            root = f"arch2code-{run_id}-project/"
            manifest = archive.read(f"{root}MANIFEST.md").decode("utf-8")

        assert plan.attribution["has_baseline"] is False
        # The manifest and the run-named directory still work.
        assert f"{root}project/agents/svc/app.py" in names
        assert f"{root}project/.arch/build/{run_id}/manifest.json" in names
        # The diff did not, so an edit to a pre-existing file is invisible.
        assert f"{root}project/src/existing.py" not in names

        assert "There is no baseline for this run" in manifest
        assert any("no filesystem baseline" in note for note in plan.omissions)
    finally:
        restore()


def test_project_preview_and_download_agree(tmp_path, settings, store) -> None:
    run_id = _existing_run_ids()[0]
    patched, _project, restore = _plant_project(
        tmp_path, settings, store, run_id, with_baseline=True
    )
    try:
        plan = build_export(patched, store, run_id, kind="project")
        summary = plan.summary()
        with zipfile.ZipFile(io.BytesIO(_zip_bytes(plan))) as archive:
            names = set(archive.namelist())
        assert summary["entry_count"] == len(names)
        assert summary["kind"] == "project"
        assert summary["counts"]["project"] > 0
        assert summary["attribution"]["has_baseline"] is True
        assert {e["arcname"] for e in summary["entries"]} == names
    finally:
        restore()


def test_project_endpoint_serves_a_valid_zip() -> None:
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from app.main import create_app

    run_id = _existing_run_ids()[0]
    with fastapi_testclient.TestClient(create_app()) as client:
        response = client.get(f"/api/runs/{run_id}/export/project")
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "application/zip"
        assert f"arch2code-{run_id}-project.zip" in response.headers["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert archive.testzip() is None
            assert f"arch2code-{run_id}-project/MANIFEST.md" in archive.namelist()

        preview = client.get(f"/api/runs/{run_id}/export/preview?kind=project")
        assert preview.status_code == 200
        assert preview.json()["kind"] == "project"

        # An unrecognised kind is not a 400: the preview is decoration on a
        # download that would still work.
        assert client.get(
            f"/api/runs/{run_id}/export/preview?kind=nonsense"
        ).json()["kind"] == "full"
