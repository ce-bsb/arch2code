"""Exporting a run as a self-contained, auditable ZIP archive.

The literal requirement is "let the user export or download the generated code,
or the whole solution Bob created". This module is the answer to the second
half: not a folder of loose files, but an archive somebody can open in six
months and still understand.

Four rules shape everything below.

**Nothing is assembled in memory.** :func:`iter_zip` writes into a sink that is
drained after every chunk, so a 400 MB build tree costs a 256 KB buffer, not
400 MB of RSS. The archive is produced while it is being sent.

**Every path is proved before it is read.** Sources come from the run's own
state and from a build manifest that a language model wrote, which means a
component path is untrusted input. :func:`_resolve_inside` re-resolves every
candidate against the roots this run is allowed to read, and refuses anything
that leaves them. Archive names go through :func:`safe_arcname`, so no entry
can carry ``..``, an absolute path, a drive letter or a Windows-illegal
character into somebody's extraction directory.

**An absent artifact is reported, never invented.** A stage that exits 0 and
writes nothing is the exact signature of ``arch-scaffold`` running without
``--yolo`` (Bob removes ``write_to_file`` from the tool set in non-interactive
mode). The MANIFEST names every file that was expected and is missing, and says
what that absence means. A silently thin zip would be the worst possible
outcome for an audit.

**No credential ever leaves in a download.** The scaffold writes a real ``.env``
beside its ``.env.example``; ``.gitignore`` keeps that out of the repository, and
an HTTP download would walk straight past it. :func:`is_secret_file` refuses the
``.env`` family, private keys and the known credential file names at collection
time AND again over the finished plan, and every refusal is written into the
archive so the user learns the file exists instead of shipping it by accident.

**The MANIFEST is the product.** The code is reproducible; the reasoning is
not. MANIFEST.md records which drawing this came from, which model read it, at
what confidence, what the model assumed, what it could not resolve, and what a
human decided at the gate. That file is what makes the archive worth keeping.

Layout of the full archive::

    arch2code-<run_id>/
      MANIFEST.md          generated here, now — the audit narrative
      README.md            deployment steps, only when a target profile defines them
      code/                everything stage 4 generated
      audit/               air.json, verdict.md, extraction.json, capture-manifest.json,
                           validation.md, pipeline.md, run.json, events.jsonl, gate.json,
                           quality.json, verifications.jsonl, stages/<id>/*
      diagram/original/    the file the user uploaded, byte for byte
      diagram/normalized/  the PNG the vision model actually saw

The ``code`` variant is the same ``code/`` tree hoisted to the archive root,
plus README.md when a profile defines one, and nothing else.

The ``project`` variant answers the other half of the same request — "the code
artifacts AND the whole root of the directory and of the generated files" — by
carrying every file the run wrote *anywhere* under the project root, at its real
path::

    arch2code-<run_id>-project/
      MANIFEST.md          the same narrative, plus section 12: the attribution
      project/             every attributed file, at its path in the project
      audit/  diagram/     as above

There is no ``code/`` in that variant: the project tree already contains
``.arch/build/<run_id>/`` where it really lives, and a hoisted second copy would
only make a reader ask which one is authoritative. How the file list is decided
— a filesystem snapshot taken at run start, the stage-4 manifest, and the
directories named after the run — lives in :mod:`app.projectdiff` and is printed
in full in the archive's own MANIFEST, failure modes included.

Target profile contract (read-only, optional). If ``targets/<id>/target.yaml``
exists under the project root and declares a ``deploy:`` block, its steps are
rendered into README.md::

    deploy:
      summary: "One line about what deploying this produces."
      prerequisites: ["ibmcloud CLI 2.x", "..."]
      steps:
        - title: "Build the image"
          run: "docker build -t ..."
          note: "Optional prose."
      docs: ["https://..."]

``validate.checks[].{name,cmd}`` from the same file is rendered as the offline
verification section (see the briefing, section 6.5: the point of those checks
is that they run without the platform).
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal, Mapping, Sequence

from . import __version__
from . import projectdiff
from .config import Settings
from .errors import NotFound
from .models import RunState
from .store import RunStore

__all__ = [
    "ExportKind",
    "ExportEntry",
    "ExportPlan",
    "build_export",
    "iter_zip",
    "safe_arcname",
    "safe_name",
]

ExportKind = Literal["full", "code", "project"]

#: Streaming chunk. Big enough that a 100 MB file is 400 reads, small enough
#: that the peak buffer is invisible.
_CHUNK = 256 * 1024

#: Files above this are still archived but not hashed for the MANIFEST. Hashing
#: is a second full read, and beyond this size that doubles a slow export for a
#: line of provenance nobody asked for.
_HASH_MAX_BYTES = 64 * 1024 * 1024

#: Directories that are build noise, not generated code.
_SKIP_DIRS = {
    "__pycache__", ".git", ".hg", ".svn", ".venv", "venv", "node_modules",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".tox", ".DS_Store",
}
_SKIP_SUFFIXES = {".pyc", ".pyo", ".pyd", ".so", ".o", ".class"}
_SKIP_NAMES = {".DS_Store", "Thumbs.db", ".gitkeep"}

#: Files that hold credentials and must never leave the machine in a download.
#: This is not hypothetical: the scaffold writes a real ``.env`` next to its
#: ``.env.example`` (``agents/supervisor-precos/.env`` in this repo contains a
#: live WATSONX_APIKEY), and ``.gitignore`` is what keeps it out of the repo —
#: an HTTP download bypasses that entirely. Every exclusion is reported in the
#: MANIFEST, so the user learns the file exists rather than silently shipping it.
_SECRET_SUFFIXES = {
    ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".keytab", ".ppk", ".asc",
}
_SECRET_NAMES = {
    ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials", ".htpasswd",
    "credentials", "credentials.json", "service-account.json", "sa.json",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
}
#: Suffixes that mark a template rather than a filled-in secret. These are part
#: of the deliverable: without .env.example nobody can run the generated code.
_TEMPLATE_MARKERS = (".example", ".sample", ".template", ".dist", ".tpl")


def is_secret_file(name: str) -> bool:
    """True when a file name is a credential store rather than a deliverable."""
    lowered = Path(str(name)).name.lower()
    if any(lowered.endswith(marker) for marker in _TEMPLATE_MARKERS):
        return False
    if lowered == ".env" or lowered.endswith(".env") or lowered.startswith(".env."):
        return True
    if lowered in _SECRET_NAMES:
        return True
    if Path(lowered).suffix in _SECRET_SUFFIXES:
        return True
    return any(lowered.startswith(prefix) for prefix in ("id_rsa", "id_ed25519", "id_ecdsa"))

#: Windows reserved device names. An entry called ``con.py`` extracts fine on a
#: POSIX box and fails on Windows; renaming here costs nothing.
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

_UNSAFE_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]+')


# --------------------------------------------------------------------------- #
# name safety
# --------------------------------------------------------------------------- #


def safe_name(value: str, *, fallback: str = "file") -> str:
    """Reduce one path component to something safe to extract anywhere.

    Not cosmetic. ``..`` and an absolute path are the two ways a zip entry
    writes outside the directory the user extracted it into, and a control
    character or ``:`` is how the same archive becomes unextractable on
    Windows. Unicode is preserved (NFC) — a component named ``Serviço`` should
    keep its name — only the characters that are structurally dangerous go.
    """
    text = unicodedata.normalize("NFC", str(value)).strip()
    text = _UNSAFE_CHARS.sub("_", text)
    # Trailing dots and spaces break Windows extraction; a LEADING dot must
    # survive. Stripping it renamed `.env.example` to `env.example` and, worse,
    # turned a `.env` full of credentials into an innocuous-looking `env`.
    text = text.rstrip(" .").lstrip(" ")
    if not text or text in {".", ".."}:
        return fallback
    stem = text.split(".", 1)[0].lower()
    if stem in _RESERVED:
        text = f"_{text}"
    return text[:150]


def safe_arcname(*parts: str | Path) -> str:
    """Join parts into a zip entry name that cannot escape the extraction root."""
    out: list[str] = []
    for part in parts:
        if part in (None, ""):
            continue
        raw = str(part).replace("\\", "/")
        for piece in raw.split("/"):
            if piece in ("", ".", ".."):
                continue
            if re.fullmatch(r"[A-Za-z]:", piece):  # drive letter
                continue
            out.append(safe_name(piece))
    return "/".join(out) or "file"


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExportEntry:
    """One member of the archive.

    Exactly one of ``source`` (a file on disk, streamed) and ``data`` (bytes
    generated here) is set.
    """

    arcname: str
    role: str          # code | audit | diagram | generated
    note: str          # one line, rendered in the MANIFEST inventory
    source: Path | None = None
    data: bytes | None = None
    size: int = 0
    sha256: str | None = None
    mtime: datetime | None = None


@dataclass
class ExportPlan:
    """Everything the archive will contain, decided before a byte is sent.

    Deciding first means a missing run, an empty code tree or a traversal
    attempt fails with a JSON error the front end can render, instead of
    truncating a download that already returned 200.
    """

    run_id: str
    kind: ExportKind
    filename: str
    entries: list[ExportEntry] = field(default_factory=list)
    #: Human-readable lines about what was deliberately left out and why.
    omissions: list[str] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    #: For ``kind="project"``: how the set of files was decided. Rendered in
    #: MANIFEST.md and returned by the preview endpoint, because a user cannot
    #: trust a set of files without knowing the rule that produced it.
    attribution: dict[str, Any] = field(default_factory=dict)

    @property
    def total_bytes(self) -> int:
        return sum(e.size for e in self.entries)

    def by_role(self, role: str) -> list[ExportEntry]:
        return [e for e in self.entries if e.role == role]

    def summary(self) -> dict[str, Any]:
        """A JSON preview of the archive, for a UI that wants to show it first."""
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "filename": self.filename,
            "generated_at": self.generated_at.isoformat(),
            "entry_count": len(self.entries),
            "total_bytes": self.total_bytes,
            "counts": {
                role: len(self.by_role(role))
                for role in ("code", "project", "audit", "diagram", "generated")
            },
            "attribution": dict(self.attribution),
            "entries": [
                {
                    "arcname": e.arcname,
                    "role": e.role,
                    "bytes": e.size,
                    "sha256": e.sha256,
                    "note": e.note,
                }
                for e in self.entries
            ],
            "omissions": list(self.omissions),
        }


# --------------------------------------------------------------------------- #
# filesystem helpers
# --------------------------------------------------------------------------- #


def _sha256(path: Path) -> str | None:
    try:
        if path.stat().st_size > _HASH_MAX_BYTES:
            return None
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(_CHUNK), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _resolve_inside(candidate: Path | str, roots: Sequence[Path]) -> Path | None:
    """Resolve ``candidate`` and return it only if it stays inside ``roots``.

    Symlinks are followed by ``resolve()`` before the check, so a symlink to
    ``~/.ssh`` planted in a build tree resolves outside the roots and is
    dropped. Returning ``None`` rather than raising is deliberate: one stray
    path in a model-written manifest must not fail the whole export, it must be
    reported as an omission.
    """
    try:
        path = Path(candidate)
        resolved = path.resolve() if path.is_absolute() else (roots[0] / path).resolve()
    except (OSError, RuntimeError, IndexError):
        return None
    for root in roots:
        try:
            resolved.relative_to(Path(root).resolve())
        except (ValueError, OSError):
            continue
        return resolved
    return None


def _is_noise(path: Path) -> bool:
    if path.name in _SKIP_NAMES or path.suffix.lower() in _SKIP_SUFFIXES:
        return True
    return any(part in _SKIP_DIRS for part in path.parts)


def _walk_files(directory: Path) -> Iterator[Path]:
    """Every real file under ``directory``, noise excluded, in stable order."""
    if not directory.is_dir():
        return
    for entry in sorted(directory.rglob("*")):
        if entry.is_symlink() and not entry.exists():
            continue
        if not entry.is_file():
            continue
        if _is_noise(entry.relative_to(directory)):
            continue
        yield entry


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_jsonl(path: Path, *, limit: int = 5000) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    out.append(record)
                if len(out) >= limit:
                    break
    except OSError:
        return out
    return out


def _first_existing(*candidates: Path) -> Path | None:
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    return None


# --------------------------------------------------------------------------- #
# discovery: audit trail
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _AuditSpec:
    """One expected member of the audit trail."""

    arc: str
    note: str
    #: Templates relative to the Bob working directory, formatted with run_id.
    project_candidates: tuple[str, ...] = ()
    #: Templates relative to webapp/runs/<run_id>/.
    run_candidates: tuple[str, ...] = ()
    #: When missing, this is the line the MANIFEST prints.
    absence_means: str = ""
    #: Modes that actually produce this file. A Mode A run has no AIR and no
    #: verdict, and listing those as "gaps" would teach a reader to distrust a
    #: run that did exactly what it was asked to do.
    modes: tuple[str, ...] = ("vision", "pipeline")


_AUDIT_SPECS: tuple[_AuditSpec, ...] = (
    _AuditSpec(
        arc="run.json",
        note="The run state this app kept: stages, exit codes, timings, token totals.",
        run_candidates=("run.json",),
        absence_means="The run has no state file, which should be impossible; the "
                      "archive was built from a directory that is not a run.",
    ),
    _AuditSpec(
        arc="events.jsonl",
        note="Append-only event log. Every stage transition and artifact write, in order.",
        run_candidates=("events.jsonl",),
        absence_means="The run never emitted an event, so it was never started.",
    ),
    _AuditSpec(
        arc="capture-manifest.json",
        note="What the capture step did to the image: EXIF rotation, resize, sha256.",
        run_candidates=("vision/capture-manifest.json", "vision/capture.json"),
        project_candidates=(".arch/intake/{run_id}/capture-manifest.json",),
        absence_means="The image was never normalized. A structured source (.drawio) "
                      "never is, because it never goes through vision.",
    ),
    _AuditSpec(
        arc="extraction.json",
        note="What the vision model read off the drawing: components, connections, "
             "bounding boxes, per-item confidence.",
        run_candidates=("vision/extraction.json", "vision/extraction.raw.json"),
        project_candidates=(".arch/intake/{run_id}/extraction.json",),
        absence_means="No extraction was produced, so nothing downstream had an input.",
    ),
    _AuditSpec(
        arc="quality.json",
        note="Derived quality summary: broken references, connections that needed a "
             "second pass, orphan components.",
        run_candidates=("vision/quality.json",),
        absence_means="No quality summary was written (Mode A writes it; the full "
                      "pipeline does not).",
        modes=("vision",),
    ),
    _AuditSpec(
        arc="verifications.jsonl",
        note="Second-pass checks: a different prompt over the same image, one record "
             "per claim, with the verdict.",
        run_candidates=("vision/verifications.jsonl",),
        absence_means="Nobody ran a verification. Every claim in extraction.json is "
                      "therefore single-pass.",
        modes=("vision",),
    ),
    _AuditSpec(
        arc="air.json",
        note="The Architecture Intermediate Representation: the platform-neutral model "
             "every generator reads.",
        project_candidates=(".arch/air/{run_id}/air.json",),
        absence_means="The analyst stage produced no AIR, so the scaffold had nothing "
                      "formal to generate from.",
        modes=("pipeline",),
    ),
    _AuditSpec(
        arc="verdict.md",
        note="The critic's adversarial review and the gate line a human decided on.",
        project_candidates=(".arch/review/{run_id}/verdict.md",),
        absence_means="The critic stage never wrote a verdict, so the stage-3 gate had "
                      "no machine opinion to agree or disagree with.",
        modes=("pipeline",),
    ),
    _AuditSpec(
        arc="gate.json",
        note="The human decision at the stage-3 gate, recorded independently of run.json.",
        run_candidates=("gate.json",),
        absence_means="No human decision was recorded at the stage-3 gate.",
        modes=("pipeline",),
    ),
    _AuditSpec(
        arc="manifest.json",
        note="The scaffold's own manifest: which generated file implements which "
             "component, and the evidence for each.",
        project_candidates=(".arch/build/{run_id}/manifest.json",),
        absence_means="The scaffold stage wrote no manifest, so no generated file is "
                      "traceable back to a component.",
        modes=("pipeline",),
    ),
    _AuditSpec(
        arc="validation.md",
        note="The validator's report: which hypotheses were tested and what happened.",
        project_candidates=(
            ".arch/run/{run_id}/validation.md",
            ".arch/build/{run_id}/validation.md",
        ),
        absence_means="Nothing validated the generated code. Treat it as unverified.",
        modes=("pipeline",),
    ),
    _AuditSpec(
        arc="pipeline.md",
        note="The pipeline log the harness keeps alongside the run.",
        project_candidates=(
            ".arch/run/{run_id}/pipeline.md",
            ".arch/build/{run_id}/pipeline.md",
            ".arch/{run_id}/pipeline.md",
            ".arch/pipeline.md",
        ),
        absence_means="No pipeline log was written by the harness.",
        modes=("pipeline",),
    ),
)


def _collect_audit(
    state: RunState,
    run_dir: Path,
    project_root: Path,
    roots: Sequence[Path],
    missing: list[tuple[str, str]],
    *,
    prefix: str,
) -> list[ExportEntry]:
    entries: list[ExportEntry] = []
    seen: set[Path] = set()

    for spec in _AUDIT_SPECS:
        candidates: list[Path] = []
        for template in spec.run_candidates:
            candidates.append(run_dir / template)
        for template in spec.project_candidates:
            candidates.append(project_root / template.format(run_id=state.run_id))

        found = None
        for candidate in candidates:
            resolved = _resolve_inside(candidate, roots)
            if resolved is not None and resolved.is_file():
                found = resolved
                break

        if found is None:
            # Only a file this mode was supposed to produce counts as a gap.
            if state.mode in spec.modes:
                missing.append((spec.arc, spec.absence_means))
            continue
        if found in seen:
            continue
        seen.add(found)
        entries.append(
            ExportEntry(
                arcname=safe_arcname(prefix, "audit", spec.arc),
                role="audit",
                note=spec.note,
                source=found,
                size=found.stat().st_size,
                sha256=_sha256(found),
                mtime=_mtime(found),
            )
        )

    # Per-stage subprocess evidence: the exact argv, the raw NDJSON, the stderr.
    # This is what turns "Bob exited 1" into something reproducible by hand.
    stages_dir = run_dir / "stages"
    for path in _walk_files(stages_dir):
        resolved = _resolve_inside(path, roots)
        if resolved is None or resolved in seen:
            continue
        seen.add(resolved)
        rel = resolved.relative_to(stages_dir)
        entries.append(
            ExportEntry(
                arcname=safe_arcname(prefix, "audit", "stages", rel),
                role="audit",
                note=f"Stage evidence: {rel.as_posix()}",
                source=resolved,
                size=resolved.stat().st_size,
                sha256=_sha256(resolved),
                mtime=_mtime(resolved),
            )
        )
    return entries


# --------------------------------------------------------------------------- #
# discovery: generated code
# --------------------------------------------------------------------------- #

_PATH_KEYS = {"files", "file", "test", "tests", "path", "paths", "root"}


def _manifest_paths(node: Any, out: list[str]) -> None:
    """Collect every path-like string a build manifest declares.

    The manifest is written by a language model, so its exact shape drifts. Both
    observed shapes are covered — ``components.<id>.files[]`` with a sibling
    ``test``, and ``infrastructure.files[]`` — and so is any future nesting,
    because the walk is over key names rather than over a fixed structure.
    """
    if isinstance(node, Mapping):
        for key, value in node.items():
            if str(key).lower() in _PATH_KEYS:
                if isinstance(value, str):
                    out.append(value)
                elif isinstance(value, (list, tuple)):
                    out.extend(v for v in value if isinstance(v, str))
            _manifest_paths(value, out)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _manifest_paths(item, out)


def _collect_code(
    state: RunState,
    project_root: Path,
    roots: Sequence[Path],
    omissions: list[str],
    *,
    prefix: str,
) -> list[ExportEntry]:
    """Every file stage 4 generated, from both places it can put them.

    ``.arch/build/<run_id>/`` is the contracted output directory, and it is what
    the requirement names. But the scaffold also writes real project trees and
    records them in ``manifest.json`` with a ``_meta.root`` outside ``.arch/``
    (both historical runs in this repo do exactly that). Exporting only the
    contracted directory would hand the user a manifest describing code the zip
    does not contain, which is worse than no zip at all.
    """
    entries: list[ExportEntry] = []
    seen: set[Path] = set()
    used_arcnames: set[str] = set()

    def add(path: Path, arc_rel: str, note: str) -> None:
        if path in seen:
            return
        seen.add(path)
        if is_secret_file(path.name):
            omissions.append(
                f"`{arc_rel}` was NOT included: a file with that name holds "
                "credentials, and a download is not a safe way to move them. "
                "Recreate it on the target machine from its .example sibling."
            )
            return
        arcname = safe_arcname(prefix, arc_rel) if prefix else safe_arcname(arc_rel)
        if arcname in used_arcnames:
            stem = Path(arcname)
            arcname = safe_arcname(
                str(stem.parent), f"{stem.stem}.{len(used_arcnames)}{stem.suffix}"
            )
        used_arcnames.add(arcname)
        entries.append(
            ExportEntry(
                arcname=arcname,
                role="code",
                note=note,
                source=path,
                size=path.stat().st_size,
                sha256=_sha256(path),
                mtime=_mtime(path),
            )
        )

    build_dir = project_root / ".arch" / "build" / state.run_id
    resolved_build = _resolve_inside(build_dir, roots)
    if resolved_build is not None and resolved_build.is_dir():
        for path in _walk_files(resolved_build):
            add(
                path,
                str(path.relative_to(resolved_build)),
                "Generated by the scaffold stage.",
            )

    manifest = _read_json(build_dir / "manifest.json")
    if isinstance(manifest, Mapping):
        declared: list[str] = []
        _manifest_paths(manifest, declared)
        for raw in dict.fromkeys(declared):  # dedupe, keep order
            resolved = _resolve_inside(raw, roots)
            if resolved is None:
                omissions.append(
                    f"manifest.json declares {raw!r}, which resolves outside this "
                    "run's readable roots. It was NOT included."
                )
                continue
            if resolved.is_dir():
                for path in _walk_files(resolved):
                    try:
                        rel = path.relative_to(project_root)
                    except ValueError:
                        rel = Path(path.name)
                    add(path, str(rel), "Generated code, declared by manifest.json.")
            elif resolved.is_file():
                if _is_noise(Path(raw)):
                    continue
                try:
                    rel = resolved.relative_to(project_root)
                except ValueError:
                    rel = Path(resolved.name)
                add(path=resolved, arc_rel=str(rel),
                    note="Generated code, declared by manifest.json.")
            else:
                omissions.append(
                    f"manifest.json declares {raw!r}, which does not exist on disk. "
                    "The scaffold reported a file it never wrote."
                )

    return entries


# --------------------------------------------------------------------------- #
# discovery: everything the run wrote, anywhere under the project root
# --------------------------------------------------------------------------- #

#: Directories named after the run. Anything inside one of these belongs to that
#: run beyond argument, snapshot or no snapshot.
_RUN_OWNED_DIRS: tuple[str, ...] = (
    ".arch/intake/inbox/{run_id}",
    ".arch/intake/{run_id}",
    ".arch/air/{run_id}",
    ".arch/review/{run_id}",
    ".arch/build/{run_id}",
    ".arch/run/{run_id}",
)

#: A project export above this many files is almost certainly picking up
#: something it should not. It is truncated and the truncation is reported,
#: rather than producing a multi-gigabyte download nobody can use.
_MAX_PROJECT_ENTRIES = 20_000

#: Human wording for each way a file can be attributed to a run. The MANIFEST
#: prints these verbatim; they are the whole explanation of the heuristic.
_ATTRIBUTION_NOTES: Mapping[str, str] = {
    "created": "Did not exist when the run started (filesystem snapshot diff).",
    "modified": "Existed before the run and changed during it (size or mtime).",
    "manifest": "Declared by the stage-4 build manifest.",
    "run-directory": "Inside a directory named after this run.",
}


def _collect_project(
    state: RunState,
    run_dir: Path,
    project_root: Path,
    roots: Sequence[Path],
    omissions: list[str],
    *,
    prefix: str,
) -> tuple[list[ExportEntry], dict[str, Any]]:
    """Every file this run produced anywhere in the project tree.

    Three independent sources are unioned, and each file records which of them
    found it (see :data:`_ATTRIBUTION_NOTES`):

    * the **snapshot diff** — ``fs-snapshot.json``, written the moment the run
      started, compared against the tree now. Exact about what changed, blind
      about who changed it;
    * the **stage-4 manifest** — authoritative about intent, written by a model,
      so it can name a file that was never written;
    * the **directories named after the run** — unambiguous, but they are
      exactly what the scaffold escapes.

    No single source is trusted alone, and the union is labelled rather than
    laundered: the MANIFEST prints, per file, why it is in the archive.
    """
    entries: list[ExportEntry] = []
    reasons: dict[Path, list[str]] = {}
    truncated = False

    def attribute(path: Path, reason: str) -> None:
        nonlocal truncated
        if path in reasons:
            if reason not in reasons[path]:
                reasons[path].append(reason)
            return
        if len(reasons) >= _MAX_PROJECT_ENTRIES:
            truncated = True
            return
        reasons[path] = [reason]

    # -- 1. the filesystem diff ------------------------------------------- #
    snapshot = projectdiff.read_snapshot(run_dir / projectdiff.SNAPSHOT_FILENAME)
    diff = projectdiff.diff_snapshot(snapshot, project_root)

    for rel in diff.added:
        resolved = _resolve_inside(project_root / rel, roots)
        if resolved is not None and resolved.is_file():
            attribute(resolved, "created")
    for rel in diff.modified:
        resolved = _resolve_inside(project_root / rel, roots)
        if resolved is not None and resolved.is_file():
            attribute(resolved, "modified")

    # -- 2. directories named after the run -------------------------------- #
    for template in _RUN_OWNED_DIRS:
        directory = _resolve_inside(
            project_root / template.format(run_id=state.run_id), roots
        )
        if directory is None or not directory.is_dir():
            continue
        for path in _walk_files(directory):
            attribute(path, "run-directory")

    # -- 3. the stage-4 manifest ------------------------------------------- #
    manifest = _read_json(project_root / ".arch" / "build" / state.run_id / "manifest.json")
    if isinstance(manifest, Mapping):
        declared: list[str] = []
        _manifest_paths(manifest, declared)
        for raw in dict.fromkeys(declared):
            resolved = _resolve_inside(raw, roots)
            if resolved is None:
                omissions.append(
                    f"manifest.json declares {raw!r}, which resolves outside this "
                    "run's readable roots. It was NOT included."
                )
                continue
            if resolved.is_dir():
                for path in _walk_files(resolved):
                    attribute(path, "manifest")
            elif resolved.is_file():
                if not _is_noise(Path(raw)):
                    attribute(resolved, "manifest")
            else:
                omissions.append(
                    f"manifest.json declares {raw!r}, which does not exist on disk. "
                    "The scaffold reported a file it never wrote."
                )

    # -- assemble ----------------------------------------------------------- #
    # Every path here has been through _resolve_inside, so it is symlink-free;
    # the project root as configured may not be (on macOS /tmp is a symlink to
    # /private/tmp). Comparing an unresolved root against a resolved path drops
    # every file to its bare basename and flattens the tree, so both spellings
    # of the root are tried.
    root_candidates = [Path(project_root)]
    try:
        resolved_root = Path(project_root).resolve()
        if resolved_root != root_candidates[0]:
            root_candidates.append(resolved_root)
    except OSError:  # pragma: no cover - defensive
        pass

    def relative(path: Path) -> Path:
        for candidate in root_candidates:
            try:
                return path.relative_to(candidate)
            except ValueError:
                continue
        return Path(path.name)

    used_arcnames: set[str] = set()
    for path in sorted(reasons):
        why = reasons[path]
        if is_secret_file(path.name):
            omissions.append(
                f"`{path.name}` was NOT included: a file with that name holds "
                "credentials, and a download is not a safe way to move them. "
                "Recreate it on the target machine from its .example sibling."
            )
            continue
        arcname = safe_arcname(prefix, "project", relative(path))
        if arcname in used_arcnames:
            continue
        used_arcnames.add(arcname)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        entries.append(
            ExportEntry(
                arcname=arcname,
                role="project",
                note=" ".join(_ATTRIBUTION_NOTES.get(reason, reason) for reason in why),
                source=path,
                size=size,
                sha256=_sha256(path),
                mtime=_mtime(path),
            )
        )

    if truncated:
        omissions.append(
            f"More than {_MAX_PROJECT_ENTRIES} files were attributed to this run; "
            "the archive stops there. That many changed files means the snapshot "
            "baseline caught something other than the run — check "
            "`webapp/runs/<run_id>/fs-snapshot.json`."
        )
    if not diff.has_baseline:
        omissions.append(
            "This run has no filesystem baseline (`fs-snapshot.json`), so the "
            "diff could not run. Only the stage-4 manifest and the directories "
            "named after the run contributed to this archive. Runs started "
            "before this feature existed are all in this position."
        )

    report: dict[str, Any] = {
        "has_baseline": diff.has_baseline,
        "baseline_taken_at": (
            diff.snapshot.taken_at.isoformat() if diff.snapshot else None
        ),
        "baseline_files": diff.snapshot.file_count if diff.snapshot else 0,
        "baseline_excluded": list(diff.snapshot.excluded) if diff.snapshot else [],
        "created": len(diff.added),
        "modified": len(diff.modified),
        "deleted": list(diff.deleted[:200]),
        "deleted_count": len(diff.deleted),
        "unchanged": diff.unchanged,
        "truncated": truncated or diff.truncated,
        "by_reason": {
            reason: sum(1 for why in reasons.values() if reason in why)
            for reason in _ATTRIBUTION_NOTES
        },
        "project_root": str(project_root),
        "files": len(entries),
    }
    return entries, report


# --------------------------------------------------------------------------- #
# discovery: the drawing
# --------------------------------------------------------------------------- #


def _collect_diagram(
    state: RunState,
    store: RunStore,
    project_root: Path,
    roots: Sequence[Path],
    omissions: list[str],
    *,
    prefix: str,
) -> list[ExportEntry]:
    """The original upload and the PNG the model actually saw.

    Both, always, and never conflated. The normalized image is EXIF-rotated and
    resized to 1568 px, so it is the only image the bounding boxes in
    extraction.json are valid against; the original is the only thing that
    proves what the user actually handed over.
    """
    entries: list[ExportEntry] = []

    original = _first_existing(
        project_root / ".arch" / "intake" / "inbox" / state.run_id / state.upload.filename,
        Path(state.upload.stored_path),
    )
    resolved = _resolve_inside(original, roots) if original else None
    if resolved is not None:
        entries.append(
            ExportEntry(
                arcname=safe_arcname(prefix, "diagram", "original", state.upload.filename),
                role="diagram",
                note=f"The file as uploaded, byte for byte (sha256 {state.upload.sha256[:16]}…).",
                source=resolved,
                size=resolved.stat().st_size,
                sha256=_sha256(resolved),
                mtime=_mtime(resolved),
            )
        )
    else:
        omissions.append(
            "The original upload is no longer on disk, so it could not be included. "
            "Its sha256 is recorded in MANIFEST.md and in run.json."
        )

    normalized: Path | None = None
    try:
        from .vision import normalized_image_path

        normalized = normalized_image_path(store, state.run_id)
    except Exception:  # noqa: BLE001 - vision is optional to this module
        normalized = None
    if normalized is None:
        # Fallback for a run whose capture manifest is gone but whose intake
        # directory still holds the file the capture step named.
        intake = project_root / ".arch" / "intake" / state.run_id
        if intake.is_dir():
            normalized = _first_existing(*sorted(intake.glob("*.normalized.*")))

    resolved_norm = _resolve_inside(normalized, roots) if normalized else None
    if resolved_norm is not None:
        entries.append(
            ExportEntry(
                arcname=safe_arcname(prefix, "diagram", "normalized", resolved_norm.name),
                role="diagram",
                note="The image the vision model actually saw. Bounding boxes in "
                     "extraction.json are normalized against THIS image, not the original.",
                source=resolved_norm,
                size=resolved_norm.stat().st_size,
                sha256=_sha256(resolved_norm),
                mtime=_mtime(resolved_norm),
            )
        )
    else:
        omissions.append(
            "No normalized image exists for this run. A structured source (.drawio) "
            "never produces one, because it never goes through vision."
        )
    return entries


# --------------------------------------------------------------------------- #
# target profile
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TargetProfile:
    """A platform profile as read from ``targets/<id>/target.yaml``."""

    id: str
    path: Path
    document: dict[str, Any]

    @property
    def name(self) -> str:
        return str(self.document.get("name") or self.id)

    @property
    def deploy(self) -> dict[str, Any]:
        block = self.document.get("deploy")
        return dict(block) if isinstance(block, Mapping) else {}

    @property
    def checks(self) -> list[dict[str, Any]]:
        validate = self.document.get("validate")
        if not isinstance(validate, Mapping):
            return []
        checks = validate.get("checks")
        if not isinstance(checks, (list, tuple)):
            return []
        return [dict(c) for c in checks if isinstance(c, Mapping)]

    @property
    def defines_deploy_steps(self) -> bool:
        steps = self.deploy.get("steps")
        return bool(steps) or bool(self.checks)


def _target_id(state: RunState, project_root: Path, run_dir: Path) -> str | None:
    """Which platform profile this run targeted, if anything said so.

    Deliberately conservative: it reads an id that something explicitly wrote,
    and guesses nothing. A wrong profile would put deployment steps for the
    wrong platform in front of somebody holding generated code.
    """
    manifest = _read_json(project_root / ".arch" / "build" / state.run_id / "manifest.json")
    if isinstance(manifest, Mapping):
        meta = manifest.get("_meta")
        for source in (meta if isinstance(meta, Mapping) else {}, manifest):
            for key in ("target", "target_id", "profile", "platform"):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

    air = _read_json(project_root / ".arch" / "air" / state.run_id / "air.json")
    if isinstance(air, Mapping):
        for key in ("target", "target_id", "platform_profile"):
            value = air.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    pin = _read_json(run_dir / "target.json")
    if isinstance(pin, Mapping):
        value = pin.get("id") or pin.get("target")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def load_target_profile(
    state: RunState,
    settings: Settings,
    run_dir: Path,
    omissions: list[str],
) -> TargetProfile | None:
    """Read the run's platform profile, if one is declared and readable."""
    project_root = Path(state.bob_cwd or settings.bob_cwd)
    target_id = _target_id(state, project_root, run_dir)
    if not target_id:
        return None

    safe_id = safe_name(target_id)
    candidates = [
        Path(settings.project_root) / "targets" / safe_id / "target.yaml",
        project_root / "targets" / safe_id / "target.yaml",
    ]
    path = _first_existing(*candidates)
    if path is None:
        omissions.append(
            f"This run declares target profile {target_id!r}, but no "
            f"targets/{safe_id}/target.yaml exists. Deployment steps could not be "
            "rendered; see MANIFEST.md."
        )
        return None

    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        omissions.append(
            f"PyYAML is not installed, so {path} could not be read and README.md "
            "has no deployment steps. Remedy: pip install pyyaml, then export again."
        )
        return None

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a broken profile must not fail the export
        omissions.append(f"{path} could not be parsed ({exc}); README.md was skipped.")
        return None

    if not isinstance(document, Mapping):
        omissions.append(f"{path} does not contain a mapping; README.md was skipped.")
        return None
    return TargetProfile(id=target_id, path=path, document=dict(document))


# --------------------------------------------------------------------------- #
# MANIFEST.md
# --------------------------------------------------------------------------- #


def _fmt_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _fmt_confidence(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):.2f}"
    return "unknown"


def _md_escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _table(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    body = [list(r) for r in rows]
    if not body:
        return []
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join(["---"] * len(header)) + "|",
    ]
    for row in body:
        lines.append("| " + " | ".join(_md_escape(cell) for cell in row) + " |")
    lines.append("")
    return lines


def _stage_rows(state: RunState) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for stage in state.stages:
        stats = stage.stats
        tokens = stats.total_tokens if stats and stats.total_tokens is not None else ""
        rows.append([
            stage.index,
            stage.id,
            stage.status,
            stage.slug or "in-process",
            stage.approval_mode or "—",
            "" if stage.exit_code is None else stage.exit_code,
            f"{stage.duration_ms} ms" if stage.duration_ms else "",
            tokens,
        ])
    return rows


def _render_attribution(report: Mapping[str, Any]) -> list[str]:
    """The section that explains where the project archive's file list came from.

    Printed in full, including what the method cannot know. A user who is handed
    "the whole project" without being told how "the whole project" was decided
    has been handed a guess wearing a manifest's clothes.
    """
    lines: list[str] = []
    add = lines.append

    add("## 12. How this archive decided what the run produced")
    add("")
    add("This is a `project` export: it carries every file the run is believed to "
        "have written **anywhere under the project root**, at its real path, not "
        "only the contracted `.arch/build/` directory. Belief is not proof, so "
        "here is exactly how the list was built.")
    add("")
    add("Three independent sources were unioned. The `what it is` column of "
        "section 11 states, per file, which of them found it:")
    add("")
    add("1. **Filesystem snapshot diff.** When the run started, before any "
        "subprocess existed, every file under the project root was recorded with "
        "its size and modification time. That baseline was compared against the "
        "tree as it is now. A file that did not exist then is `created`; a file "
        "whose size or mtime changed is `modified`.")
    add("2. **The stage-4 build manifest.** `manifest.json` names the files the "
        "scaffold intended to write, including the ones it wrote outside "
        "`.arch/`. It is written by a language model, so it can name a file that "
        "was never created — those are listed as omissions above, not silently "
        "dropped.")
    add("3. **Directories named after the run.** `.arch/intake/<run_id>/`, "
        "`.arch/air/<run_id>/`, `.arch/review/<run_id>/`, `.arch/build/<run_id>/` "
        "and `.arch/run/<run_id>/` belong to this run by construction.")
    add("")

    rows = [
        ["Baseline snapshot taken",
         report.get("baseline_taken_at") or "NEVER — see the warning below"],
        ["Files in the baseline", report.get("baseline_files", 0)],
        ["Created during the run", report.get("created", 0)],
        ["Modified during the run", report.get("modified", 0)],
        ["Unchanged (not archived)", report.get("unchanged", 0)],
        ["Deleted during the run (not archivable)", report.get("deleted_count", 0)],
        ["Files in this archive's `project/`", report.get("files", 0)],
    ]
    by_reason = report.get("by_reason")
    if isinstance(by_reason, Mapping):
        for reason, count in by_reason.items():
            rows.append([f"Attributed by: {reason}", count])
    lines.extend(_table(["Measure", "Value"], rows))

    if not report.get("has_baseline"):
        add("**There is no baseline for this run.** It was started before the "
            "snapshot existed, or the snapshot could not be written. The diff "
            "therefore contributed nothing, and this archive holds only what the "
            "build manifest declared and what sits in directories named after the "
            "run. Anything the scaffold wrote elsewhere is missing from it.")
        add("")

    deleted = report.get("deleted")
    if isinstance(deleted, (list, tuple)) and deleted:
        add("**Files that existed when the run started and are gone now.** They "
            "cannot be archived — there is nothing left to read — but their "
            "disappearance is part of what the run did:")
        add("")
        for name in deleted:
            add(f"- `{name}`")
        add("")

    excluded = report.get("baseline_excluded")
    if isinstance(excluded, (list, tuple)) and excluded:
        add("Deliberately outside the comparison entirely: "
            + ", ".join(f"`{item}`" for item in excluded)
            + ". Those are this application's own bookkeeping — every run's event "
              "log lives there and changes constantly, including runs that have "
              "nothing to do with this one. This run's own trail is in `audit/`.")
        add("")

    add("### What this method cannot tell you")
    add("")
    add("- **It cannot tell who changed a file.** Anything else that wrote to the "
        "project root while the run executed — an editor saving a file, a second "
        "run, a background formatter — is indistinguishable from the scaffold's "
        "own work and is in this archive.")
    add("- **It compares size and mtime, not content.** A rewrite that preserves "
        "both would be missed. Content hashing every file in the project on every "
        "export was judged the worse trade: an extra file in an archive is "
        "harmless, a slow export during a demo is not.")
    add("- **Symlinks are neither followed nor recorded**, so a symlink into a "
        "home directory cannot become an archive entry.")
    add("- **Credential files are never included**, whatever attributed them. "
        "Every refusal is listed in section 6.")
    add("")
    return lines


def render_manifest(
    plan: ExportPlan,
    state: RunState,
    *,
    extraction: Mapping[str, Any] | None,
    air: Mapping[str, Any] | None,
    capture: Mapping[str, Any] | None,
    verifications: Sequence[Mapping[str, Any]],
    build_manifest: Mapping[str, Any] | None,
    profile: TargetProfile | None,
    missing: Sequence[tuple[str, str]],
) -> str:
    """The narrative that makes the archive auditable months later.

    Written from files, never from live state: everything below is read back out
    of the same documents that are in the zip, so a reader can check every claim
    in this file against a sibling of it.
    """
    lines: list[str] = []
    add = lines.append

    add(f"# arch2code export — {state.run_id}")
    add("")
    add(f"Generated {plan.generated_at.strftime('%Y-%m-%d %H:%M:%S UTC')} "
        f"by arch2code webapp {__version__}.")
    add("")
    add("This archive is the whole audit trail of one run: the drawing that went in, "
        "what a model read from it, what a human decided about that reading, the code "
        "that was generated, and the evidence for every step in between. Nothing here "
        "was reconstructed after the fact — every file is a copy of what was on disk "
        "when the run executed.")
    add("")

    # ---------------------------------------------------------------- summary
    add("## 1. The run at a glance")
    add("")
    provenance = {}
    if isinstance(extraction, Mapping) and isinstance(extraction.get("_provenance"), Mapping):
        provenance = dict(extraction["_provenance"])
    rows = [
        ["Run id", state.run_id],
        ["Mode", f"`{state.mode}`" + (" (vision preview: reads the drawing, generates no code)"
                                      if state.mode == "vision" else " (full pipeline)")],
        ["Final status", state.status],
        ["Created", state.created_at.isoformat()],
        ["Last updated", state.updated_at.isoformat()],
        ["Stages succeeded",
         f"{sum(1 for s in state.stages if s.status == 'succeeded')} of {len(state.stages)}"],
        ["Bob working directory", f"`{state.bob_cwd}`"],
        ["Total tokens",
         f"{state.totals.tokens_in} in / {state.totals.tokens_out} out"],
    ]
    lines.extend(_table(["Field", "Value"], rows))

    # ---------------------------------------------------------------- source
    add("## 2. The source diagram")
    add("")
    upload = state.upload
    rows = [
        ["File name", upload.filename],
        ["Content type", upload.content_type],
        ["Size", _fmt_bytes(upload.bytes)],
        ["sha256", f"`{upload.sha256}`"],
        ["Declared source kind", state.source_kind],
        ["Routing decision", upload.routing.extraction_path],
        ["Tool the router recommended", upload.routing.recommended_tool],
        ["Operator hint", state.hint or "(none given)"],
    ]
    lines.extend(_table(["Field", "Value"], rows))
    if upload.structured_siblings:
        add("A structured source for the same drawing existed alongside it "
            f"({', '.join(Path(s).name for s in upload.structured_siblings)}). "
            "Parsing that file is exact and free; vision is neither.")
        add("")
    if upload.warnings:
        add("Warnings recorded at upload time:")
        add("")
        for warning in upload.warnings:
            add(f"- {warning}")
        add("")

    if isinstance(capture, Mapping):
        normalization = capture.get("normalization")
        if isinstance(normalization, Mapping):
            normalized = normalization.get("normalized")
            original = normalization.get("original")
            rows = []
            if isinstance(original, Mapping):
                rows.append(["Original pixels",
                             f"{original.get('width')}×{original.get('height')}"])
            if isinstance(normalized, Mapping):
                rows.append(["Normalized pixels",
                             f"{normalized.get('width')}×{normalized.get('height')}"])
            for key in ("scale", "exif_rotation_applied", "resized", "format"):
                if key in normalization:
                    rows.append([key.replace("_", " ").capitalize(), normalization[key]])
            if rows:
                add("The image was normalized before the model saw it. Bounding boxes "
                    "are expressed against the normalized image:")
                add("")
                lines.extend(_table(["Field", "Value"], rows))
        warnings = capture.get("warnings")
        if isinstance(warnings, (list, tuple)) and warnings:
            add("Capture warnings:")
            add("")
            for warning in warnings:
                add(f"- {warning}")
            add("")

    # ---------------------------------------------------------------- reader
    add("## 3. What read the drawing, and how sure it was")
    add("")
    rows = [
        ["Vision model", provenance.get("model") or "(not recorded)"],
        ["Prompt version", provenance.get("prompt_version") or "(not recorded)"],
        ["Extraction path", provenance.get("extraction_path") or upload.routing.extraction_path],
    ]
    if isinstance(extraction, Mapping):
        rows.append(["Overall confidence", _fmt_confidence(extraction.get("overall_confidence"))])
        if extraction.get("legibility_notes"):
            rows.append(["Legibility notes", extraction["legibility_notes"]])
        components = extraction.get("components")
        connections = extraction.get("connections")
        rows.append(["Components read", len(components) if isinstance(components, list) else 0])
        rows.append(["Connections read", len(connections) if isinstance(connections, list) else 0])
    lines.extend(_table(["Field", "Value"], rows))

    add("Confidence is the model's own, per item, on a 0–1 scale. Anything below "
        "0.85 is flagged below; low confidence is not a failure, it is a reading "
        "that a human should look at before it becomes code.")
    add("")

    convention = extraction.get("_bbox_convention") if isinstance(extraction, Mapping) else None
    if isinstance(convention, Mapping):
        add(f"`evidence.value` in `audit/extraction.json` is the model's raw output. "
            f"It was measured — not assumed — to be **{convention.get('convention')}** "
            f"({convention.get('reason') or convention.get('note') or 'no note recorded'}). "
            "Reading those four numbers the other way puts every box in the wrong "
            "place, so anything that draws them must apply the same reading.")
        add("")

    if isinstance(extraction, Mapping) and isinstance(extraction.get("components"), list):
        rows = []
        for component in extraction["components"]:
            if not isinstance(component, Mapping):
                continue
            confidence = component.get("confidence")
            flag = "review" if isinstance(confidence, (int, float)) and confidence < 0.85 else ""
            rows.append([
                component.get("id"),
                component.get("name"),
                component.get("kind"),
                component.get("tech") or "—",
                _fmt_confidence(confidence),
                flag,
            ])
        if rows:
            add("### Components, as read")
            add("")
            lines.extend(_table(
                ["id", "name", "kind", "tech", "confidence", "flag"], rows
            ))

    if isinstance(extraction, Mapping) and isinstance(extraction.get("connections"), list):
        rows = []
        for connection in extraction["connections"]:
            if not isinstance(connection, Mapping):
                continue
            rows.append([
                connection.get("id"),
                f"{connection.get('from')} → {connection.get('to')}",
                connection.get("protocol"),
                connection.get("sync"),
                _fmt_confidence(connection.get("confidence")),
            ])
        if rows:
            add("### Connections, as read")
            add("")
            lines.extend(_table(
                ["id", "edge", "protocol", "sync", "confidence"], rows
            ))

    # ---------------------------------------------------------------- verification
    if verifications:
        add("## 4. Second-pass verification")
        add("")
        add("Each row is a separate call over the same image with a different prompt. "
            "A verdict of `false` or `uncertain` is a finding, not a failure of the "
            "tool: it means the first pass and the second pass disagree.")
        add("")
        rows = []
        for record in verifications:
            rows.append([
                record.get("target_id") or record.get("target_kind"),
                (record.get("claim") or "")[:110],
                record.get("verdict"),
                _fmt_confidence(record.get("confidence")),
                (record.get("contradiction") or record.get("observed") or "")[:110],
            ])
        lines.extend(_table(
            ["target", "claim", "verdict", "confidence", "what the model saw"], rows
        ))
    else:
        add("## 4. Second-pass verification")
        add("")
        add("No verification was run. Every claim in `audit/extraction.json` is "
            "single-pass and unchecked.")
        add("")

    # ---------------------------------------------------------------- assumptions
    add("## 5. Assumptions that were made")
    add("")
    assumptions = []
    for document in (air, extraction):
        if isinstance(document, Mapping) and isinstance(document.get("assumptions"), list):
            assumptions = [a for a in document["assumptions"] if isinstance(a, Mapping)]
            if assumptions:
                break
    if assumptions:
        add("An assumption is something no one drew and the model filled in anyway. "
            "`confirmed = false` means nobody has agreed with it yet.")
        add("")
        rows = []
        for item in assumptions:
            rows.append([
                item.get("id"),
                item.get("text"),
                item.get("made_by") or "model",
                "yes" if item.get("confirmed") else "NO",
                (item.get("impact") or "")[:160],
            ])
        lines.extend(_table(
            ["id", "assumption", "made by", "confirmed", "impact if wrong"], rows
        ))
    else:
        add("No assumptions were recorded. For a Mode A run that is expected: the "
            "vision stage reads the drawing and does not reason beyond it.")
        add("")

    # ---------------------------------------------------------------- gaps
    add("## 6. Gaps left open")
    add("")
    unknowns = []
    for document in (air, extraction):
        if isinstance(document, Mapping) and isinstance(document.get("unknowns"), list):
            unknowns = [u for u in document["unknowns"] if isinstance(u, Mapping)]
            if unknowns:
                break
    if unknowns:
        rows = []
        for item in unknowns:
            rows.append([
                item.get("id"),
                item.get("about"),
                item.get("question"),
                "BLOCKING" if item.get("blocking") else "non-blocking",
                item.get("answer") or "(unanswered)",
            ])
        lines.extend(_table(
            ["id", "about", "question", "severity", "answer"], rows
        ))
    else:
        add("The extraction recorded no open questions.")
        add("")

    quality: Mapping[str, Any] = {}
    if isinstance(extraction, Mapping) and isinstance(extraction.get("_quality"), Mapping):
        quality = extraction["_quality"]
    broken = quality.get("broken_refs") or []
    needs = quality.get("connections_needing_verification") or []
    if broken:
        add(f"**Broken references** — connections that point at a component id which "
            f"does not exist: {', '.join(str(b) for b in broken)}")
        add("")
    if needs:
        add(f"**Connections the model flagged for verification**: "
            f"{', '.join(str(n) for n in needs)}")
        add("")
    if quality.get("action_required"):
        add(f"**Action the extractor asked for**: {quality['action_required']}")
        add("")

    if state.mode == "vision":
        add("This is a Mode A run: it captures the drawing, reads it, and stops. "
            "`air.json`, `verdict.md`, `manifest.json` and `validation.md` belong to "
            "the full pipeline and are absent by design, not by failure.")
        add("")
    if missing:
        add("**Files that this run was supposed to produce and did not.** Each line is "
            "a real gap in the trail, not a packaging error:")
        add("")
        for name, meaning in missing:
            add(f"- `{name}` — {meaning}")
        add("")
    if plan.omissions:
        add("**Other omissions:**")
        add("")
        for note in plan.omissions:
            add(f"- {note}")
        add("")

    # ---------------------------------------------------------------- gate
    add("## 7. The human decision")
    add("")
    gate = state.gate
    if gate is None:
        add("This run has no stage-3 gate. Mode A stops after extraction, and the "
            "gate belongs to the full pipeline.")
        add("")
    else:
        rows = [
            ["Critic's verdict", gate.verdict],
            ["Gate line found in verdict.md", gate.gate_line or "(none)"],
            ["Human decision", gate.decision or "(not decided)"],
            ["Overrode the critic", "YES" if gate.override else "no"],
            ["Reason given", gate.reason or "—"],
            ["Decided at", gate.decided_at.isoformat() if gate.decided_at else "—"],
        ]
        lines.extend(_table(["Field", "Value"], rows))
        if gate.verdict == "absent":
            add("`absent` means verdict.md never contained a gate string at all. "
                "Anything that proceeded past this point proceeded without a machine "
                "opinion.")
            add("")
        if gate.override:
            add("This run was released over the critic's objection. The reason above "
                "is the whole justification on record.")
            add("")

    # ---------------------------------------------------------------- stages
    add("## 8. What each stage did")
    add("")
    lines.extend(_table(
        ["#", "stage", "status", "chat mode", "approval", "exit", "duration", "tokens"],
        _stage_rows(state),
    ))
    add("`approval` is the Bob approval mode the stage ran under. `arch-scaffold` "
        "must run as `yolo`: under the default and under `auto_edit`, Bob removes "
        "`write_to_file` from the tool set, and the stage exits 0 having written "
        "nothing at all.")
    add("")
    if state.error is not None:
        add(f"**The run failed:** {state.error.title} — {state.error.detail}")
        if state.error.remedy:
            add("")
            add(f"Remedy on record: {state.error.remedy}")
        add("")

    # ---------------------------------------------------------------- code
    add("## 9. The generated code")
    add("")
    is_project = plan.kind == "project"
    code_entries = plan.by_role("project" if is_project else "code")
    folder = "project/" if is_project else "code/"
    if code_entries:
        add(f"{len(code_entries)} file(s), {_fmt_bytes(sum(e.size for e in code_entries))} "
            f"in total, under `{folder}`."
            + (" Each one keeps the path it has in the project, and section 12 "
               "explains how each was attributed to this run." if is_project else ""))
        add("")
        if isinstance(build_manifest, Mapping) and isinstance(
            build_manifest.get("components"), Mapping
        ):
            add("Traceability, straight from the scaffold's own manifest — which "
                "generated file implements which component of the drawing:")
            add("")
            rows = []
            for component_id, entry in build_manifest["components"].items():
                if not isinstance(entry, Mapping):
                    continue
                files = entry.get("files")
                files = files if isinstance(files, list) else [files]
                rows.append([
                    component_id,
                    ", ".join(str(f) for f in files if f),
                    entry.get("test") or "—",
                    entry.get("evidence") or "—",
                ])
            lines.extend(_table(
                ["component", "files", "test", "evidence in the drawing"], rows
            ))
    elif is_project:
        add("**No file anywhere under the project root could be attributed to this "
            "run.** Nothing was created, nothing was modified, the build manifest "
            "declared nothing and the directories named after the run are empty. "
            "Section 12 states which of the three attribution sources were even "
            "available; if there was no baseline snapshot, that is the first thing "
            "to fix.")
        add("")
    else:
        add("**This archive contains no generated code.**")
        add("")
        if state.mode == "vision":
            add("That is correct for this run: Mode A is a vision preview. It reads "
                "the drawing and stops, on purpose, before anything is generated. "
                "Run the full pipeline on the same upload to get code.")
        else:
            add("The scaffold stage produced nothing. The usual cause is "
                "`arch-scaffold` running without the `yolo` approval mode; check "
                "`audit/stages/scaffold/` for the exact argv and stderr.")
        add("")

    # ---------------------------------------------------------------- deploy
    add("## 10. Deployment")
    add("")
    if profile is not None and profile.defines_deploy_steps:
        add(f"This run targeted the **{profile.name}** platform profile "
            f"(`{profile.id}`). Its deployment steps are in `README.md` at the root "
            "of this archive, rendered from `" + str(profile.path.name) + "`.")
    elif profile is not None:
        add(f"This run targeted the **{profile.name}** platform profile "
            f"(`{profile.id}`), but that profile declares no `deploy:` block, so no "
            "deployment steps could be rendered. Deployment is whatever the "
            "generated code's own README says.")
    else:
        add("No platform profile is declared for this run, so there are no "
            "profile-defined deployment steps to render. Deployment instructions, "
            "if any, are whatever the generated code carries in `code/README.md`.")
    add("")

    # ---------------------------------------------------------------- inventory
    add("## 11. Everything in this archive")
    add("")
    add("Paths are relative to the archive root. The digest is the first 16 hex "
        "characters of the file's sha256 as it exists here; an empty cell means the "
        "file was too large to hash during export. Verify any entry after "
        "extracting with `shasum -a 256 <path>`.")
    add("")
    if plan.kind == "project":
        add("For everything under `project/`, the last column states **why that "
            "file is here** — which of the three attribution sources found it. "
            "Section 12 explains what each answer means and what it cannot prove.")
        add("")
    root_prefix = f"arch2code-{safe_name(state.run_id)}" + (
        "-project/" if plan.kind == "project" else "/"
    )
    lines.extend(_table(
        ["path", "size", "sha256 (first 16)", "what it is"],
        [
            [
                entry.arcname[len(root_prefix):]
                if entry.arcname.startswith(root_prefix) else entry.arcname,
                _fmt_bytes(entry.size),
                (entry.sha256 or "")[:16],
                entry.note,
            ]
            for entry in sorted(plan.entries, key=lambda e: e.arcname)
        ],
    ))
    add("`MANIFEST.md` — this file — is deliberately absent from the table above: it "
        "is generated at download time and cannot contain its own digest.")
    add("")

    if plan.kind == "project":
        lines.extend(_render_attribution(plan.attribution))

    add("---")
    add("")
    add("Produced by arch2code, which drives the IBM Bob CLI and a watsonx.ai vision "
        "model. Every number above came from a file in this archive; none of it was "
        "typed by hand.")
    add("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# README.md (deployment)
# --------------------------------------------------------------------------- #


def render_deploy_readme(
    profile: TargetProfile, state: RunState, plan: ExportPlan
) -> str:
    """Deployment steps for the generated artifact, from the target profile."""
    deploy = profile.deploy
    lines: list[str] = []
    add = lines.append

    add(f"# Deploying `{state.run_id}` — {profile.name}")
    add("")
    if deploy.get("summary"):
        add(str(deploy["summary"]))
        add("")
    add(f"These steps come from the platform profile `{profile.id}` "
        f"(`targets/{safe_name(profile.id)}/target.yaml`), not from this exporter. "
        "If they are wrong, the profile is wrong, and fixing it there fixes every "
        "future export.")
    add("")

    prerequisites = deploy.get("prerequisites")
    if isinstance(prerequisites, (list, tuple)) and prerequisites:
        add("## Before you start")
        add("")
        for item in prerequisites:
            add(f"- {item}")
        add("")

    steps = deploy.get("steps")
    if isinstance(steps, (list, tuple)) and steps:
        add("## Steps")
        add("")
        for index, step in enumerate(steps, start=1):
            if isinstance(step, Mapping):
                title = step.get("title") or step.get("name") or f"Step {index}"
                add(f"### {index}. {title}")
                add("")
                if step.get("note"):
                    add(str(step["note"]))
                    add("")
                command = step.get("run") or step.get("cmd")
                if command:
                    add("```bash")
                    add(str(command).strip())
                    add("```")
                    add("")
            else:
                add(f"{index}. {step}")
        add("")

    checks = profile.checks
    if checks:
        add("## Verify before deploying")
        add("")
        add("These run offline, against the generated code alone. No cluster, no "
            "tenant, no credentials — which is the point: a design can be checked "
            "against the target before anyone provisions anything.")
        add("")
        for check in checks:
            name = check.get("name") or "check"
            command = check.get("cmd") or check.get("run")
            if not command:
                continue
            add(f"**{name}**")
            add("")
            add("```bash")
            add(str(command).strip())
            add("```")
            add("")

    docs = deploy.get("docs")
    if isinstance(docs, (list, tuple)) and docs:
        add("## Reference")
        add("")
        for doc in docs:
            add(f"- {doc}")
        add("")

    code_entries = plan.by_role("code")
    add("## What you are deploying")
    add("")
    add(f"{len(code_entries)} generated file(s) from run `{state.run_id}`, "
        f"read off `{state.upload.filename}`.")
    add("")
    add("The generated code has not been reviewed by a human unless somebody says it "
        "has. `MANIFEST.md` in the full export lists every assumption the model made "
        "and every question it could not answer. Read that before this runs anywhere "
        "that matters.")
    add("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #


def build_export(
    settings: Settings,
    store: RunStore,
    run_id: str,
    *,
    kind: ExportKind = "full",
) -> ExportPlan:
    """Decide the whole archive before a single byte is streamed.

    Raises:
        NotFound: the run does not exist, or ``kind="code"`` was asked of a run
            that generated none.
    """
    state = store.load(run_id)  # raises NotFound with a remedy
    run_dir = store.run_dir(run_id)
    project_root = Path(state.bob_cwd or settings.bob_cwd)

    # The only directories this export may read from. Everything is re-resolved
    # against these, including paths a model wrote into manifest.json.
    roots = [
        project_root,
        Path(settings.project_root),
        Path(settings.runs_root),
        Path(settings.uploads_root),
    ]

    suffix = {"code": "-code.zip", "project": "-project.zip"}.get(kind, ".zip")
    plan = ExportPlan(
        run_id=run_id,
        kind=kind,
        filename=f"arch2code-{safe_name(run_id)}{suffix}",
    )
    missing: list[tuple[str, str]] = []

    root_prefix = (
        f"arch2code-{safe_name(run_id)}"
        + ("-project" if kind == "project" else "")
        if kind in ("full", "project")
        else ""
    )
    code_prefix = safe_arcname(root_prefix, "code") if kind == "full" else ""

    # The project archive carries the whole tree at its real paths, so a second
    # copy of the same files hoisted into code/ would only make the reader ask
    # which one is authoritative.
    code_entries = (
        []
        if kind == "project"
        else _collect_code(state, project_root, roots, plan.omissions, prefix=code_prefix)
    )

    if kind == "code" and not code_entries:
        raise NotFound(
            code="no_generated_code",
            title="This run generated no code to download",
            detail=(
                f"{run_id} has no files under .arch/build/{run_id}/ and its build "
                "manifest declares none."
                + (
                    " This run is a Mode A vision preview: it reads the drawing and "
                    "stops before generation, by design."
                    if state.mode == "vision"
                    else " The scaffold stage exited without writing anything, which "
                    "is what arch-scaffold does when it does not run under the yolo "
                    "approval mode."
                )
            ),
            remedy=(
                "Run the full pipeline on this upload to produce code, or download "
                f"the full export at /api/runs/{run_id}/export, which contains the "
                "audit trail regardless."
            ),
        )

    plan.entries.extend(code_entries)

    if kind == "project":
        project_entries, plan.attribution = _collect_project(
            state, run_dir, project_root, roots, plan.omissions, prefix=root_prefix
        )
        plan.entries.extend(project_entries)

    if kind in ("full", "project"):
        plan.entries.extend(
            _collect_audit(
                state, run_dir, project_root, roots, missing, prefix=root_prefix
            )
        )
        plan.entries.extend(
            _collect_diagram(
                state, store, project_root, roots, plan.omissions, prefix=root_prefix
            )
        )

    # Last line of defence. Every collector already refuses credential files;
    # this makes it structurally impossible for a future collector to forget.
    survivors: list[ExportEntry] = []
    for entry in plan.entries:
        if entry.source is not None and is_secret_file(Path(entry.arcname).name):
            note = f"`{entry.arcname}` was withheld: it is a credential file."
            if note not in plan.omissions:
                plan.omissions.append(note)
            continue
        survivors.append(entry)
    plan.entries = survivors

    profile = load_target_profile(state, settings, run_dir, plan.omissions)

    # Documents generated here, added last so they can describe everything above.
    #
    # The MANIFEST reads the ENRICHED extraction where one is available, because
    # that is the only place the measured bounding-box convention is recorded.
    # The RAW file is what goes into the archive: the audit copy has to be the
    # bytes the model produced, and the MANIFEST explains how to read them.
    extraction: Any = None
    try:
        from .vision import load_extraction

        extraction = load_extraction(store, run_id)
    except Exception:  # noqa: BLE001 - vision is optional to this module
        extraction = None
    if not isinstance(extraction, Mapping):
        extraction = _read_json(run_dir / "vision" / "extraction.json")
    if not isinstance(extraction, Mapping):
        extraction = _read_json(project_root / ".arch" / "intake" / run_id / "extraction.json")
    air = _read_json(project_root / ".arch" / "air" / run_id / "air.json")
    capture = _read_json(run_dir / "vision" / "capture-manifest.json")
    if not isinstance(capture, Mapping):
        capture = _read_json(
            project_root / ".arch" / "intake" / run_id / "capture-manifest.json"
        )
    build_manifest = _read_json(
        project_root / ".arch" / "build" / run_id / "manifest.json"
    )
    verifications = _read_jsonl(run_dir / "vision" / "verifications.jsonl")

    if profile is not None and profile.defines_deploy_steps:
        readme = render_deploy_readme(profile, state, plan).encode("utf-8")
        plan.entries.append(
            ExportEntry(
                arcname=safe_arcname(root_prefix, "README.md"),
                role="generated",
                note="Deployment steps for the generated artifact, from the target profile.",
                data=readme,
                size=len(readme),
                sha256=hashlib.sha256(readme).hexdigest(),
            )
        )

    if kind == "code" and plan.omissions:
        # The code-only archive has no MANIFEST, and "your .env was withheld" is
        # not something a user may discover by noticing an absence.
        notes = (
            f"# Export notes — {run_id}\n\n"
            "This archive holds only the generated code. The following files were "
            "deliberately left out:\n\n"
            + "\n".join(f"- {line}" for line in plan.omissions)
            + f"\n\nThe full export at /api/runs/{run_id}/export contains the audit "
              "trail, the source drawing and a MANIFEST that explains all of it.\n"
        ).encode("utf-8")
        plan.entries.append(
            ExportEntry(
                arcname=safe_arcname(root_prefix, "EXPORT-NOTES.md"),
                role="generated",
                note="What was left out of this archive, and why.",
                data=notes,
                size=len(notes),
                sha256=hashlib.sha256(notes).hexdigest(),
            )
        )

    if kind in ("full", "project"):
        manifest_md = render_manifest(
            plan,
            state,
            extraction=extraction if isinstance(extraction, Mapping) else None,
            air=air if isinstance(air, Mapping) else None,
            capture=capture if isinstance(capture, Mapping) else None,
            verifications=verifications,
            build_manifest=build_manifest if isinstance(build_manifest, Mapping) else None,
            profile=profile,
            missing=missing,
        ).encode("utf-8")
        plan.entries.insert(
            0,
            ExportEntry(
                arcname=safe_arcname(root_prefix, "MANIFEST.md"),
                role="generated",
                note="This file: what everything in the archive is, and where it came from.",
                data=manifest_md,
                size=len(manifest_md),
                sha256=hashlib.sha256(manifest_md).hexdigest(),
            ),
        )

    return plan


# --------------------------------------------------------------------------- #
# streaming zip
# --------------------------------------------------------------------------- #


class _StreamSink(io.RawIOBase):
    """A write-only, non-seekable sink whose buffer is drained after every write.

    ``zipfile`` probes for ``seek`` at construction time; because this raises,
    the archive is written with data descriptors and never needs to go back and
    patch a header. That is the whole trick that makes a ZIP streamable.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._position = 0

    def writable(self) -> bool:  # noqa: D102
        return True

    def seekable(self) -> bool:  # noqa: D102
        return False

    def write(self, data) -> int:  # type: ignore[override]
        chunk = bytes(data)
        self._buffer += chunk
        self._position += len(chunk)
        return len(chunk)

    def tell(self) -> int:  # noqa: D102
        return self._position

    def drain(self) -> bytes:
        chunk = bytes(self._buffer)
        self._buffer.clear()
        return chunk


def _zipinfo(entry: ExportEntry) -> zipfile.ZipInfo:
    moment = entry.mtime or datetime.now(timezone.utc)
    # The ZIP epoch is 1980; anything earlier is not representable.
    year = max(moment.year, 1980)
    info = zipfile.ZipInfo(
        entry.arcname,
        date_time=(year, moment.month, moment.day,
                   moment.hour, moment.minute, moment.second),
    )
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    return info


def iter_zip(plan: ExportPlan) -> Iterator[bytes]:
    """Yield the archive in chunks, holding one buffer rather than one archive.

    A file that vanishes between planning and streaming is skipped rather than
    fatal: the response has already returned 200 by then, and a truncated
    download is a worse outcome than an archive with one fewer file — which the
    MANIFEST already lists, so the gap is visible.
    """
    sink = _StreamSink()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for entry in plan.entries:
            info = _zipinfo(entry)
            if entry.data is not None:
                archive.writestr(info, entry.data)
                yield sink.drain()
                continue
            if entry.source is None or not entry.source.is_file():
                continue
            try:
                with archive.open(info, "w") as destination, entry.source.open("rb") as source:
                    while True:
                        chunk = source.read(_CHUNK)
                        if not chunk:
                            break
                        destination.write(chunk)
                        pending = sink.drain()
                        if pending:
                            yield pending
            except OSError:
                continue
            pending = sink.drain()
            if pending:
                yield pending
    yield sink.drain()
