"""The object key layout. One module, so the app and the job cannot disagree.

The application process and the pipeline job run in different containers and
never share memory. The only thing that binds them is this naming scheme, so it
lives in exactly one file and every key is built by a function here.

    runs/<run_id>/run.json                  the RunState document
    runs/<run_id>/gate.json                 the human gate decision (audit trail)
    runs/<run_id>/events/00000001.json      one object per event, id = filename
    runs/<run_id>/events/_closed.json       terminal marker
    runs/<run_id>/stages/<stage>/…          stdout.ndjson, stderr.txt, argv.json…
    runs/<run_id>/vision/…                  Mode A output
    runs/<run_id>/arch/…                    the .arch/ tree Bob wrote, synced up
    uploads/<upload_id>/upload.json         the UploadRef document
    uploads/<upload_id>/<filename>          the uploaded bytes

Two decisions worth stating.

**Events are one object per event, not one appended file.** IBM's own guidance
on mounting a bucket says *"multiple instances writing the same file can cause
corruption"* and that s3fs has eventual consistency and no atomic rename. An
append-only file is the single worst shape for object storage. One immutable
object per event is the best: writes never collide, and a reader resumes with
``StartAfter`` instead of seeking into a file that may be mid-write.

**Event ids are zero-padded to 8 digits.** Lexicographic key order then equals
numeric event order, which is what makes ``Last-Event-ID`` a pure prefix query.
Eight digits is 100 million events per run; at the observed rate of ~15 events
for a full vision run, that ceiling is not reachable by accident. A run that
somehow exceeds it would break ordering silently, so :func:`event_key` refuses
instead.
"""

from __future__ import annotations

import re

__all__ = [
    "EVENT_DIGITS",
    "MAX_EVENT_ID",
    "run_prefix",
    "run_state_key",
    "gate_key",
    "events_prefix",
    "event_key",
    "event_id_from_key",
    "closed_marker_key",
    "stage_prefix",
    "vision_prefix",
    "arch_prefix",
    "upload_prefix",
    "upload_meta_key",
    "upload_file_key",
]

#: Width of the numeric part of an event key. See the module docstring.
EVENT_DIGITS = 8
MAX_EVENT_ID = 10**EVENT_DIGITS - 1

_EVENT_KEY_RE = re.compile(r"/events/(\d{%d})\.json$" % EVENT_DIGITS)


def _segment(value: str, what: str) -> str:
    """Reject anything that would change the shape of the key space."""
    text = str(value or "").strip()
    if not text or "/" in text or text.startswith(".") or "\\" in text:
        raise ValueError(
            f"{what}={value!r} cannot be used as a key segment: it must be a "
            "non-empty string with no slash and no leading dot."
        )
    return text


def run_prefix(run_id: str) -> str:
    return f"runs/{_segment(run_id, 'run_id')}/"


def run_state_key(run_id: str) -> str:
    return f"{run_prefix(run_id)}run.json"


def gate_key(run_id: str) -> str:
    return f"{run_prefix(run_id)}gate.json"


def events_prefix(run_id: str) -> str:
    return f"{run_prefix(run_id)}events/"


def event_key(run_id: str, event_id: int) -> str:
    """Key of one event. Ordering is the whole contract — see the docstring."""
    if not isinstance(event_id, int) or isinstance(event_id, bool) or event_id < 1:
        raise ValueError(f"event id must be a positive integer, got {event_id!r}")
    if event_id > MAX_EVENT_ID:
        raise ValueError(
            f"event id {event_id} exceeds the {EVENT_DIGITS}-digit key width. Beyond "
            "this point lexicographic order stops matching numeric order and the "
            "Last-Event-ID resume would silently replay the wrong events."
        )
    return f"{events_prefix(run_id)}{event_id:0{EVENT_DIGITS}d}.json"


def event_id_from_key(key: str) -> int | None:
    """Parse the id back out of a key. ``None`` for anything that is not an event."""
    match = _EVENT_KEY_RE.search(key or "")
    return int(match.group(1)) if match else None


def closed_marker_key(run_id: str) -> str:
    """Terminal marker.

    Under ``events/`` but named with a leading underscore, which sorts AFTER
    every digit in ASCII. A listing for the tail therefore never returns the
    marker in the middle of the event sequence.
    """
    return f"{events_prefix(run_id)}_closed.json"


def stage_prefix(run_id: str, stage: str) -> str:
    return f"{run_prefix(run_id)}stages/{_segment(stage, 'stage')}/"


def vision_prefix(run_id: str) -> str:
    return f"{run_prefix(run_id)}vision/"


def arch_prefix(run_id: str) -> str:
    """Where the ``.arch/`` tree Bob wrote in the job's CWD is preserved.

    Bob writes to its working directory by contract and that is not negotiable.
    The job mirrors the tree here after each stage, so the artifacts outlive the
    container. Retention note: a completed job run is deleted by Code Engine
    after one week — the bucket is the record, never the job run.
    """
    return f"{run_prefix(run_id)}arch/"


def upload_prefix(upload_id: str) -> str:
    return f"uploads/{_segment(upload_id, 'upload_id')}/"


def upload_meta_key(upload_id: str) -> str:
    return f"{upload_prefix(upload_id)}upload.json"


def upload_file_key(upload_id: str, filename: str) -> str:
    return f"{upload_prefix(upload_id)}{_segment(filename, 'filename')}"
