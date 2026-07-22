"""Tolerance suite for the stream-json parser.

Bob's stream-json shapes were read out of the distributed bundle rather than
observed at runtime, so the parser's contract is not "parses correctly" but
"never raises and never loses a line". These tests encode that contract:
truncated JSON, unknown types, non-JSON noise and missing keys all have to come
out the other side as renderable events.
"""

from __future__ import annotations

import json

import pytest

from app.ndjson import (
    KNOWN_TYPES,
    LineAccumulator,
    MessageCoalescer,
    extract_stats,
    normalize_line,
    preview,
    to_event_payload,
)


# --------------------------------------------------------------------------- #
# LineAccumulator
# --------------------------------------------------------------------------- #


def test_accumulator_buffers_a_partial_tail_until_its_newline():
    acc = LineAccumulator()
    assert acc.feed(b'{"type":"mes') == []
    assert acc.feed(b'sage","text":"hi"}\n') == ['{"type":"message","text":"hi"}']


def test_accumulator_splits_multiple_lines_in_one_chunk():
    acc = LineAccumulator()
    assert acc.feed(b"a\nb\nc\n") == ["a", "b", "c"]


def test_accumulator_tolerates_crlf():
    acc = LineAccumulator()
    assert acc.feed(b'{"type":"init"}\r\n') == ['{"type":"init"}']


def test_accumulator_flush_returns_a_line_with_no_trailing_newline():
    acc = LineAccumulator()
    assert acc.feed(b'{"type":"result"}') == []
    assert acc.flush() == ['{"type":"result"}']
    assert acc.flush() == []


def test_accumulator_truncates_an_over_long_line_and_drops_its_tail():
    acc = LineAccumulator(max_line_bytes=1024)
    emitted = acc.feed(b"x" * 5000)
    assert len(emitted) == 1
    assert len(emitted[0]) == 1024
    # The remainder up to the next newline is discarded, and the line after it
    # is delivered intact: one pathological line must not poison the stream.
    assert acc.feed(b"tail-of-the-huge-line\nnext\n") == ["next"]
    assert acc.truncated_lines == 1


def test_accumulator_never_raises_on_invalid_utf8():
    acc = LineAccumulator()
    lines = acc.feed(b"\xff\xfe broken\n")
    assert len(lines) == 1
    assert "broken" in lines[0]


def test_accumulator_ignores_empty_chunks():
    acc = LineAccumulator()
    assert acc.feed(b"") == []


# --------------------------------------------------------------------------- #
# normalize_line
# --------------------------------------------------------------------------- #


def test_blank_lines_are_dropped():
    assert normalize_line("") is None
    assert normalize_line("   ") is None
    assert normalize_line("\t\n") is None


@pytest.mark.parametrize("known_type", sorted(KNOWN_TYPES))
def test_every_known_type_is_recognised(known_type):
    ev = normalize_line(json.dumps({"type": known_type}))
    assert ev is not None
    assert ev.known is True
    assert ev.type == known_type
    assert ev.parse_error is None


def test_unknown_type_is_preserved_not_rejected():
    ev = normalize_line('{"type":"heartbeat","seq":3}')
    assert ev is not None
    assert ev.known is False
    assert ev.type == "unknown"
    assert ev.observed_type == "heartbeat"
    assert ev.payload == {"type": "heartbeat", "seq": 3}
    assert ev.parse_error is None


def test_truncated_json_becomes_unknown_with_a_parse_error():
    ev = normalize_line('{"type":"message","text":"unter')
    assert ev is not None
    assert ev.type == "unknown"
    assert ev.payload is None
    assert ev.parse_error
    assert ev.raw == '{"type":"message","text":"unter'


def test_non_json_noise_is_preserved_verbatim():
    noise = "  ⠹ thinking... 42%  "
    ev = normalize_line(noise)
    assert ev is not None
    assert ev.type == "unknown"
    assert ev.raw == noise
    assert ev.parse_error


def test_a_json_array_is_not_mistaken_for_an_event():
    ev = normalize_line("[1, 2, 3]")
    assert ev is not None
    assert ev.type == "unknown"
    assert ev.payload is None
    assert "list" in (ev.parse_error or "")


def test_a_non_string_type_field_does_not_crash():
    ev = normalize_line('{"type": 7}')
    assert ev is not None
    assert ev.type == "unknown"
    assert ev.observed_type is None
    assert ev.payload == {"type": 7}


def test_a_missing_type_field_does_not_crash():
    ev = normalize_line('{"text":"no type here"}')
    assert ev is not None
    assert ev.type == "unknown"
    assert ev.observed_type is None


# --------------------------------------------------------------------------- #
# to_event_payload
# --------------------------------------------------------------------------- #


def _payload(line: str) -> tuple[str, dict]:
    ev = normalize_line(line)
    assert ev is not None
    return to_event_payload(ev)


def test_message_maps_to_bob_message_with_role_and_text():
    name, data = _payload('{"type":"message","role":"assistant","text":"working"}')
    assert name == "bob.message"
    assert data["role"] == "assistant"
    assert data["text"] == "working"
    assert data["raw"]


def test_message_text_is_read_from_a_content_block_list():
    name, data = _payload(
        '{"type":"message","message":{"role":"assistant",'
        '"content":[{"type":"text","text":"alpha"},{"type":"text","text":"beta"}]}}'
    )
    assert name == "bob.message"
    assert data["role"] == "assistant"
    assert data["text"] == "alpha\nbeta"


def test_message_with_no_text_at_all_yields_none_and_keeps_raw():
    name, data = _payload('{"type":"message"}')
    assert name == "bob.message"
    assert data["role"] is None
    assert data["text"] is None
    assert data["raw"] == '{"type":"message"}'


def test_tool_use_truncates_its_input_preview():
    blob = "y" * 5000
    name, data = _payload(json.dumps({"type": "tool_use", "name": "write_to_file",
                                      "id": "tu_1", "input": {"content": blob}}))
    assert name == "bob.tool_use"
    assert data["tool"] == "write_to_file"
    assert data["tool_use_id"] == "tu_1"
    assert len(data["input_preview"]) <= 400
    assert data["input_preview"].endswith("…")


def test_tool_result_is_error_is_informational_and_may_be_absent():
    name, data = _payload('{"type":"tool_result","tool_use_id":"tu_1","output":"ok"}')
    assert name == "bob.tool_result"
    assert data["is_error"] is None
    assert data["output_preview"] == "ok"


def test_error_line_maps_to_bob_error():
    name, data = _payload('{"type":"error","message":"model unavailable"}')
    assert name == "bob.error"
    assert data["message"] == "model unavailable"


def test_unknown_line_maps_to_bob_unknown_with_the_raw_line():
    name, data = _payload('{"type":"telemetry","n":1}')
    assert name == "bob.unknown"
    assert data["observed_type"] == "telemetry"
    assert data["raw"] == '{"type":"telemetry","n":1}'
    assert data["parse_error"] is None


def test_unparsable_line_maps_to_bob_unknown_with_parse_error():
    name, data = _payload("not json at all")
    assert name == "bob.unknown"
    assert data["payload"] is None
    assert data["parse_error"]


def test_every_known_type_maps_without_raising_even_when_empty():
    for known in sorted(KNOWN_TYPES):
        name, data = _payload(json.dumps({"type": known}))
        assert name.startswith("bob.")
        assert "raw" in data


# --------------------------------------------------------------------------- #
# extract_stats
# --------------------------------------------------------------------------- #


def test_stats_are_read_from_a_nested_stats_object():
    stats = extract_stats(
        {
            "type": "result",
            "stats": {
                "total_tokens": 30,
                "input_tokens": 10,
                "output_tokens": 20,
                "duration_ms": 1234,
                "session_costs": {"coins": 2.5},
            },
        }
    )
    assert stats.total_tokens == 30
    assert stats.input_tokens == 10
    assert stats.output_tokens == 20
    assert stats.duration_ms == 1234
    assert stats.session_costs == {"coins": 2.5}


def test_stats_accept_the_prompt_completion_spelling():
    stats = extract_stats({"usage": {"prompt_tokens": 7, "completion_tokens": 9}})
    assert stats.input_tokens == 7
    assert stats.output_tokens == 9


def test_all_none_stats_is_a_valid_result():
    stats = extract_stats({"type": "result"})
    assert stats.total_tokens is None
    assert stats.session_costs is None


def test_stats_of_garbage_inputs_do_not_raise():
    assert extract_stats(None).total_tokens is None
    assert extract_stats({"stats": "not-a-mapping"}).total_tokens is None
    assert extract_stats({"stats": {"total_tokens": "nonsense"}}).total_tokens is None


def test_stats_ignore_booleans_masquerading_as_numbers():
    assert extract_stats({"stats": {"total_tokens": True}}).total_tokens is None


# --------------------------------------------------------------------------- #
# preview
# --------------------------------------------------------------------------- #


def test_preview_passes_short_strings_through():
    assert preview("short") == "short"


def test_preview_serializes_objects_and_never_raises():
    assert preview({"a": 1}) == '{"a": 1}'

    class Unserializable:
        def __repr__(self) -> str:
            return "<obj>"

    assert "obj" in preview(Unserializable())


def test_preview_respects_a_zero_limit():
    assert preview("anything", limit=0) == ""


# --------------------------------------------------------------------------- #
# MessageCoalescer
#
# The contract these encode is lossless-ness. Merging the deltas is only
# acceptable because the concatenation is byte-identical to what arrived and
# every original line survives inside the merged event; a "readable" trail that
# quietly drops output would be worse than the flood it replaced.
# --------------------------------------------------------------------------- #


def _delta(text: str, role: str = "assistant") -> dict:
    line = json.dumps(
        {"type": "message", "role": role, "content": text, "delta": True}
    )
    event = normalize_line(line)
    _, data = to_event_payload(event)
    return data


def test_delta_flag_is_extracted_from_the_line():
    assert _delta("hi")["delta"] is True
    _, whole = to_event_payload(
        normalize_line('{"type":"message","role":"assistant","content":"hi"}')
    )
    assert whole["delta"] is False


def test_consecutive_deltas_become_one_event():
    coalescer = MessageCoalescer()
    emitted = []
    for word in ("The ", "quick ", "brown ", "fox"):
        emitted.extend(coalescer.feed("bob.message", _delta(word)))
    assert emitted == [], "nothing should be emitted while the turn is open"

    emitted.extend(coalescer.flush())
    assert len(emitted) == 1
    name, data = emitted[0]
    assert name == "bob.message"
    assert data["text"] == "The quick brown fox"
    assert data["aggregated"] == 4
    assert data["role"] == "assistant"
    assert data["final"] is True


def test_a_tool_call_closes_the_block_and_keeps_the_order():
    """The reasoning that led to a call must be emitted before the call."""
    coalescer = MessageCoalescer()
    coalescer.feed("bob.message", _delta("I will read the file."))
    emitted = coalescer.feed("bob.tool_use", {"tool": "read_file"})
    assert [name for name, _ in emitted] == ["bob.message", "bob.tool_use"]
    assert emitted[0][1]["text"] == "I will read the file."


def test_a_long_turn_is_cut_into_blocks_that_continue_each_other():
    coalescer = MessageCoalescer(max_chars=10, max_age_s=1e9)
    emitted = []
    for _ in range(3):
        emitted.extend(coalescer.feed("bob.message", _delta("0123456789")))
    assert len(emitted) == 3
    assert [data["final"] for _, data in emitted] == [False, False, False]
    assert [data["chunk"] for _, data in emitted] == [0, 1, 2]
    assert len({data["turn"] for _, data in emitted}) == 1, "still one turn"


def test_a_stalled_block_is_released_by_age():
    # Frozen at zero, then five seconds later for good. Nothing about the block
    # is big enough to close it; only the age is.
    ticks = [0.0, 0.0]
    coalescer = MessageCoalescer(
        max_chars=10_000, clock=lambda: ticks.pop(0) if ticks else 5.0
    )
    assert coalescer.feed("bob.message", _delta("a")) == []
    emitted = coalescer.feed("bob.message", _delta("b"))
    assert len(emitted) == 1 and emitted[0][1]["text"] == "ab"


def test_raw_lines_are_preserved_for_the_everything_view():
    coalescer = MessageCoalescer()
    coalescer.feed("bob.message", _delta("one"))
    coalescer.feed("bob.message", _delta("two"))
    _, data = coalescer.flush()[0]
    assert len(data["raw"].splitlines()) == 2
    assert all(json.loads(line)["delta"] for line in data["raw"].splitlines())


def test_a_user_message_is_never_merged_into_the_reasoning():
    """The prompt is not the model thinking, and one turn is not two."""
    coalescer = MessageCoalescer()
    emitted = coalescer.feed("bob.message", _delta("the prompt", role="user"))
    assert len(emitted) == 1
    assert emitted[0][1]["role"] == "user"
    assert "aggregated" not in emitted[0][1]


def test_a_whole_message_passes_through_untouched():
    coalescer = MessageCoalescer()
    _, whole = to_event_payload(
        normalize_line('{"type":"message","role":"assistant","content":"done"}')
    )
    emitted = coalescer.feed("bob.message", whole)
    assert emitted == [("bob.message", whole)]


def test_flushing_an_empty_coalescer_emits_nothing():
    assert MessageCoalescer().flush() == []
