"""Tests for the storage adapter.

Two things are being defended here.

**The local backend must behave exactly as the filesystem code it mirrors.**
That is the whole basis for claiming the localhost demo is unaffected: if
``LocalObjectStore`` writes atomically, refuses to escape its root and lists in
lexicographic order, then switching a caller onto the interface changes nothing
observable.

**The key layout must be ordering-correct.** Lexicographic key order has to
equal numeric event order, because that identity is what makes ``Last-Event-ID``
a single ``StartAfter`` query instead of a scan. Test ``…keys_sort_numerically``
is the one that would catch a well-meaning simplification of the zero padding.

Nothing here talks to the network. The COS backend is exercised only through
its configuration and error paths, which is where its real bugs would be.
"""

from __future__ import annotations

import json

import pytest

from app.storage import (
    LocalObjectStore,
    StorageError,
    backend_name,
    download_prefix,
    open_object_store,
    upload_tree,
)
from app.storage import keys as K
from app.storage.publisher import EventPublisher, EventReader, rebuild_local_log


@pytest.fixture()
def store(tmp_path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path)


# --------------------------------------------------------------------------- #
# keys
# --------------------------------------------------------------------------- #


def test_event_keys_sort_numerically():
    """Lexicographic order must equal numeric order, or resume replays wrongly."""
    ids = [1, 2, 9, 10, 11, 99, 100, 1000, 99999999]
    built = [K.event_key("20260721-2130-x", i) for i in ids]
    assert built == sorted(built)
    assert [K.event_id_from_key(k) for k in built] == ids


def test_event_key_refuses_to_overflow_the_key_width():
    with pytest.raises(ValueError, match="lexicographic"):
        K.event_key("20260721-2130-x", 100_000_000)


def test_closed_marker_sorts_after_every_event():
    run = "20260721-2130-x"
    marker = K.closed_marker_key(run)
    assert marker > K.event_key(run, K.MAX_EVENT_ID)


def test_key_segments_reject_traversal():
    for bad in ("../escape", "a/b", "", ".hidden"):
        with pytest.raises(ValueError):
            K.run_prefix(bad)


# --------------------------------------------------------------------------- #
# local backend
# --------------------------------------------------------------------------- #


def test_roundtrip_and_probe(store):
    store.put_json("runs/r/run.json", {"run_id": "r"})
    assert store.get_json("runs/r/run.json") == {"run_id": "r"}
    assert store.exists("runs/r/run.json")
    assert store.probe() is not None
    store.delete("runs/r/run.json")
    assert not store.exists("runs/r/run.json")


def test_delete_of_absent_key_is_a_no_op(store):
    store.delete("runs/nope/run.json")  # must not raise


def test_missing_object_is_a_404_with_a_remedy(store):
    with pytest.raises(StorageError) as excinfo:
        store.get_bytes("runs/r/run.json")
    error = excinfo.value
    assert error.code == "object_not_found"
    assert error.status == 404
    assert error.remedy  # the project's rule: no failure without a next action


@pytest.mark.parametrize(
    "key", ["../outside", "/etc/passwd", "runs/../../etc/passwd", "a\\b"]
)
def test_traversal_is_rejected_before_touching_the_filesystem(store, key):
    with pytest.raises(StorageError) as excinfo:
        store.put_bytes(key, b"x")
    assert excinfo.value.code == "invalid_object_key"


def test_put_if_absent_is_exclusive(store):
    assert store.put_if_absent("runs/r/events/00000001.json", b"first") is True
    assert store.put_if_absent("runs/r/events/00000001.json", b"second") is False
    assert store.get_bytes("runs/r/events/00000001.json") == b"first"


def test_put_bytes_leaves_no_temp_file_behind(store, tmp_path):
    store.put_bytes("runs/r/run.json", b"{}")
    leftovers = [p.name for p in (tmp_path / "runs" / "r").iterdir() if ".tmp" in p.name]
    assert leftovers == []


def test_list_is_ordered_and_start_after_is_exclusive(store):
    run = "20260721-2130-x"
    for i in (1, 2, 3, 12):
        store.put_bytes(K.event_key(run, i), json.dumps({"id": i}).encode())

    all_keys = store.list_keys(K.events_prefix(run))
    assert all_keys == sorted(all_keys)
    assert len(all_keys) == 4

    tail = store.list_keys(K.events_prefix(run), start_after=K.event_key(run, 2))
    assert [K.event_id_from_key(k) for k in tail] == [3, 12]

    assert len(store.list_keys(K.events_prefix(run), limit=2)) == 2


def test_local_path_is_real_and_cos_would_be_none(store):
    path = store.local_path("runs/r/run.json")
    assert path is not None and path.is_absolute()


def test_open_object_store_defaults_to_local(tmp_path):
    built = open_object_store(env={}, root=tmp_path)
    assert built.backend == "local"


def test_unknown_backend_fails_loudly_instead_of_falling_back():
    """Silently running a cloud deployment on ephemeral disk is the bug to prevent."""
    with pytest.raises(StorageError) as excinfo:
        backend_name({"ARCH2CODE_STORAGE_BACKEND": "s3"})
    assert excinfo.value.code == "unknown_storage_backend"


# --------------------------------------------------------------------------- #
# tree sync
# --------------------------------------------------------------------------- #


def test_upload_tree_never_uploads_a_credential(store, tmp_path):
    source = tmp_path / "workspace"
    (source / "air").mkdir(parents=True)
    (source / "air" / "air.json").write_text("{}")
    (source / ".env").write_text("WATSONX_APIKEY=real-secret")
    (source / "webapp.env").write_text("WATSONX_APIKEY=real-secret")

    written = upload_tree(store, source, "runs/r/arch/")

    assert written == ["runs/r/arch/air/air.json"]
    assert not any("env" in key for key in written)


def test_upload_tree_of_a_missing_directory_is_empty_not_an_error(store, tmp_path):
    assert upload_tree(store, tmp_path / "never-existed", "runs/r/arch/") == []


def test_download_prefix_names_the_missing_input(store, tmp_path):
    with pytest.raises(StorageError) as excinfo:
        download_prefix(store, "runs/r/", tmp_path / "out", required=["run.json"])
    assert excinfo.value.code == "run_input_missing"
    assert "run.json" in excinfo.value.detail


def test_upload_then_download_is_lossless(store, tmp_path):
    source = tmp_path / "src"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "verdict.md").write_text("VERDICT: APPROVED\n")
    upload_tree(store, source, "runs/r/arch/")

    out = tmp_path / "out"
    download_prefix(store, "runs/r/arch/", out, required=["nested/verdict.md"])
    assert (out / "nested" / "verdict.md").read_text() == "VERDICT: APPROVED\n"


# --------------------------------------------------------------------------- #
# event publisher / reader
# --------------------------------------------------------------------------- #


def _write_events(path, *records):
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def test_publisher_mirrors_incrementally(store, tmp_path):
    log_path = tmp_path / "events.jsonl"
    _write_events(log_path, {"id": 1, "type": "run.started"}, {"id": 2, "type": "x"})

    publisher = EventPublisher(store, "20260721-2130-x", log_path)
    assert publisher.flush() == 2
    assert publisher.flush() == 0  # nothing new: no re-upload

    _write_events(log_path, {"id": 3, "type": "run.finished"})
    assert publisher.flush() == 1
    assert publisher.published_through == 3


def test_publisher_waits_for_a_torn_tail(store, tmp_path):
    """A reader that opens the log mid-append must not publish half a line."""
    log_path = tmp_path / "events.jsonl"
    _write_events(log_path, {"id": 1, "type": "a"})
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write('{"id": 2, "ty')  # writer is still going

    publisher = EventPublisher(store, "20260721-2130-x", log_path)
    assert publisher.flush() == 1

    with log_path.open("a", encoding="utf-8") as handle:
        handle.write('pe": "b"}\n')
    assert publisher.flush() == 1
    assert EventReader(store, "20260721-2130-x").last_id() == 2


def test_reader_replays_from_last_event_id(store, tmp_path):
    log_path = tmp_path / "events.jsonl"
    _write_events(log_path, *({"id": i, "type": "e"} for i in range(1, 6)))
    EventPublisher(store, "20260721-2130-x", log_path).flush()

    reader = EventReader(store, "20260721-2130-x")
    assert [r["id"] for r in reader.read()] == [1, 2, 3, 4, 5]
    assert [r["id"] for r in reader.read(after=3)] == [4, 5]
    assert reader.last_id() == 5


def test_reader_is_not_closed_until_the_marker_is_written(store, tmp_path):
    log_path = tmp_path / "events.jsonl"
    _write_events(log_path, {"id": 1, "type": "run.started"})
    publisher = EventPublisher(store, "20260721-2130-x", log_path)
    publisher.flush()

    reader = EventReader(store, "20260721-2130-x")
    assert reader.is_closed() is False

    publisher.mark_closed("succeeded")
    assert EventReader(store, "20260721-2130-x").is_closed() is True


def test_closed_marker_is_not_mistaken_for_an_event(store, tmp_path):
    log_path = tmp_path / "events.jsonl"
    _write_events(log_path, {"id": 1, "type": "run.started"})
    publisher = EventPublisher(store, "20260721-2130-x", log_path)
    publisher.flush()
    publisher.mark_closed("succeeded")

    reader = EventReader(store, "20260721-2130-x")
    assert [r["id"] for r in reader.read()] == [1]
    assert reader.last_id() == 1


def test_second_leg_continues_the_event_log_instead_of_overwriting_it(
    store, tmp_path
):
    """The gate splits a run across two containers. Ids must not restart at 1.

    This is the failure this rebuild exists to prevent: leg B starts with an
    empty disk, its EventLog mints id 1 again, and the publisher overwrites
    leg A's 00000001.json — stages 1 to 3 disappear from the timeline and
    Last-Event-ID replays the wrong events to every connected browser.
    """
    run_id = "20260721-2130-x"

    # Leg A: stages 1-3, ending parked at the gate.
    leg_a_log = tmp_path / "leg-a" / "events.jsonl"
    leg_a_log.parent.mkdir(parents=True)
    _write_events(
        leg_a_log,
        {"id": 1, "type": "run.started"},
        {"id": 2, "type": "run.stage.finished", "stage": "intake"},
        {"id": 3, "type": "run.awaiting_input", "stage": "critic"},
    )
    EventPublisher(store, run_id, leg_a_log).flush()

    # Leg B: a new container, an empty disk.
    leg_b_log = tmp_path / "leg-b" / "events.jsonl"
    already = rebuild_local_log(store, run_id, leg_b_log)
    assert already == 3
    assert leg_b_log.read_text().count("\n") == 3  # the history is back on disk

    # The real EventLog resumes numbering from the rebuilt file.
    from app.eventlog import EventLog

    resumed = EventLog(leg_b_log, run_id)
    event = resumed.append("run.resumed", {"decision": "approve"})
    assert event.id == 4

    publisher = EventPublisher(store, run_id, leg_b_log, published_through=already)
    assert publisher.flush() == 1

    reader = EventReader(store, run_id)
    assert [r["id"] for r in reader.read()] == [1, 2, 3, 4]
    assert [r["type"] for r in reader.read()][0] == "run.started"


def test_rebuild_is_a_no_op_on_the_first_leg(store, tmp_path):
    assert rebuild_local_log(store, "20260721-2130-x", tmp_path / "events.jsonl") == 0
    assert not (tmp_path / "events.jsonl").exists()


# --------------------------------------------------------------------------- #
# COS configuration (no network)
# --------------------------------------------------------------------------- #


def test_cos_config_requires_bucket_and_endpoint():
    from app.storage.cos import CosConfig

    with pytest.raises(StorageError) as excinfo:
        CosConfig.from_env({"ARCH2CODE_STORAGE_BACKEND": "cos"})
    assert excinfo.value.code == "cos_not_configured"
    assert "ARCH2CODE_COS_BUCKET" in excinfo.value.detail


def test_cos_config_never_exposes_a_credential():
    from app.storage.cos import CosConfig

    config = CosConfig.from_env(
        {
            "ARCH2CODE_COS_BUCKET": "arch2code-runs",
            "ARCH2CODE_COS_ENDPOINT": "s3.us-south.cloud-object-storage.appdomain.cloud",
            "ARCH2CODE_COS_APIKEY": "super-secret-value",
        }
    )
    assert config.auth_mode == "iam-apikey"
    assert config.endpoint.startswith("https://")  # bare hostnames are upgraded
    described = json.dumps(config.describe())
    assert "super-secret-value" not in described


def test_cos_without_any_credential_says_which_three_were_tried():
    from app.storage.cos import CosConfig, CosObjectStore

    config = CosConfig.from_env(
        {
            "ARCH2CODE_COS_BUCKET": "b",
            "ARCH2CODE_COS_ENDPOINT": "https://s3.example.com",
            "ARCH2CODE_COS_TRUSTED_PROFILE_TOKEN_FILE": "/nonexistent",
        }
    )
    with pytest.raises(StorageError) as excinfo:
        CosObjectStore(config)
    assert excinfo.value.code == "cos_no_credentials"
    assert "trusted-profiles-enabled" in excinfo.value.remedy
