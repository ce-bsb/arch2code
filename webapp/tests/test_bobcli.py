"""`--help` parsing and argv construction, against recorded fixtures.

The two fixtures are real output from Bob 1.0.6 on this machine:

* ``bob_help_root.txt``  -- run from the repository root: ten chat modes, because
  ``.bob/custom_modes.yaml`` is loaded from the working directory.
* ``bob_help_bare.txt``  -- run from a directory with no ``.bob/``: four modes.

That difference is the whole reason the working directory is part of the
contract, and these tests are what stop somebody "simplifying" the probe by
dropping ``cwd``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.bobcli import (
    APPROVAL_BY_SLUG,
    approval_for_slug,
    build_argv,
    parse_approval_modes,
    parse_chat_modes,
    parse_version,
    redact_argv,
)
from app.config import load_settings

FIXTURES = Path(__file__).parent / "fixtures"

ARCH_SLUGS = (
    "arch2code",
    "arch-intake",
    "arch-analyst",
    "arch-critic",
    "arch-scaffold",
    "arch-validator",
)


@pytest.fixture(scope="module")
def help_root() -> str:
    return (FIXTURES / "bob_help_root.txt").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def help_bare() -> str:
    return (FIXTURES / "bob_help_bare.txt").read_text(encoding="utf-8")


@pytest.fixture()
def settings(tmp_path: Path):
    return load_settings(
        {
            "ARCH2CODE_PROJECT_ROOT": str(tmp_path),
            "ARCH2CODE_BOB_BIN": "node /opt/bob/bundle/bob.js",
        }
    )


# --------------------------------------------------------------------------- #
# parse_chat_modes
# --------------------------------------------------------------------------- #


def test_repo_root_help_offers_all_ten_chat_modes(help_root):
    modes = parse_chat_modes(help_root)
    assert modes == (
        "plan",
        "code",
        "advanced",
        "ask",
        "arch2code",
        "arch-intake",
        "arch-analyst",
        "arch-critic",
        "arch-scaffold",
        "arch-validator",
    )


def test_repo_root_help_contains_every_arch2code_slug(help_root):
    modes = set(parse_chat_modes(help_root))
    assert set(ARCH_SLUGS) <= modes


def test_bare_directory_help_offers_only_the_four_built_ins(help_bare):
    assert parse_chat_modes(help_bare) == ("plan", "code", "advanced", "ask")


def test_bare_directory_help_proves_the_cwd_contract(help_bare):
    """No arch2code slug survives outside a directory with .bob/.

    If this ever passes, the modes stopped coming from the working directory
    and the probe's `cwd=settings.bob_cwd` is no longer load-bearing.
    """
    modes = set(parse_chat_modes(help_bare))
    assert not (set(ARCH_SLUGS) & modes)


def test_choices_wrapped_across_lines_are_reassembled():
    text = """\
      --chat-mode                 the mode to use for interaction, must be one
                                  of: 'plan', 'code'
              [string] [choices: "plan", "code", "arch2code",
                  "arch-intake"]
      --logout                    will remove saved credentials        [boolean]
"""
    assert parse_chat_modes(text) == ("plan", "code", "arch2code", "arch-intake")


def test_prose_form_is_accepted_when_the_choices_block_is_absent():
    text = "  --chat-mode  the mode to use, must be one of: 'plan', 'arch-critic'\n"
    assert parse_chat_modes(text) == ("plan", "arch-critic")


def test_missing_flag_returns_empty_rather_than_raising():
    assert parse_chat_modes("Usage: bob [options]") == ()
    assert parse_chat_modes("") == ()


def test_a_flag_without_choices_does_not_borrow_the_next_flags_choices():
    text = """\
      --instance-id               instance id to use for this session.  [string]
  -o, --output-format             The format of the CLI output.
                               [string] [choices: "text", "json", "stream-json"]
"""
    assert parse_chat_modes(text) == ()
    assert parse_approval_modes(text) == ()


# --------------------------------------------------------------------------- #
# parse_approval_modes
# --------------------------------------------------------------------------- #


def test_approval_modes_are_parsed_from_the_real_help(help_root):
    assert parse_approval_modes(help_root) == ("default", "auto_edit", "yolo")


def test_approval_policy_covers_every_bob_backed_stage():
    # Writes extraction.json and runs no script, so the weakest editing mode is
    # the right one.
    assert APPROVAL_BY_SLUG["arch-intake"] == "auto_edit"

    # These three are told to run a script and auto_edit excludes
    # execute_command, so under it the tool is absent from their tool list.
    # They cannot satisfy the instruction and cannot say why: they retry until
    # the stage times out, which reads as a slow model rather than a missing
    # capability. The analyst's exit criterion is literally "validate_air.py
    # exits 0 on it".
    assert APPROVAL_BY_SLUG["arch-analyst"] == "yolo"
    assert APPROVAL_BY_SLUG["arch-critic"] == "yolo"
    assert APPROVAL_BY_SLUG["arch-validator"] == "yolo"

    # Not a preference: default and auto_edit both exclude write_to_file, so
    # arch-scaffold under either exits 0 and produces no file.
    assert APPROVAL_BY_SLUG["arch-scaffold"] == "yolo"


def test_every_stage_that_runs_a_script_can_actually_run_one():
    """The policy must grant execute_command wherever a prompt demands a script.

    This is the invariant the previous table broke. Written as a rule rather
    than four literals so that adding a stage which shells out cannot silently
    inherit a mode that forbids it.
    """
    runs_a_script = {"arch-analyst", "arch-critic", "arch-validator", "arch-scaffold"}
    for slug in runs_a_script:
        assert APPROVAL_BY_SLUG[slug] == "yolo", (
            f"{slug} runs a script; only yolo keeps execute_command available"
        )


def test_every_declared_approval_mode_is_one_bob_accepts(help_root):
    accepted = set(parse_approval_modes(help_root))
    assert set(APPROVAL_BY_SLUG.values()) <= accepted


def test_unknown_slug_falls_back_to_the_least_privileged_mode():
    assert approval_for_slug("something-else") == "auto_edit"
    assert approval_for_slug(None) == "auto_edit"


# --------------------------------------------------------------------------- #
# parse_version
# --------------------------------------------------------------------------- #


def test_version_is_read_from_the_bare_version_output():
    assert parse_version("1.0.6\n") == "1.0.6"


def test_node_deprecation_noise_on_stderr_is_skipped():
    stderr = (
        "(node:11581) [DEP0040] DeprecationWarning: The `punycode` module is "
        "deprecated.\n"
    )
    assert parse_version("1.0.6\n", stderr) == "1.0.6"


def test_labelled_version_is_recognised():
    assert parse_version("bob version 2.1.0-beta.1") == "2.1.0-beta.1"


def test_help_output_carries_no_version_and_that_is_not_an_error(help_root):
    # Bob 1.0.6 prints the version only for --version; probe_bob makes a second
    # call precisely because of this.
    assert parse_version(help_root) is None


# --------------------------------------------------------------------------- #
# build_argv
# --------------------------------------------------------------------------- #


def test_prompt_is_the_last_positional_and_dash_p_is_never_emitted(settings):
    argv = build_argv(
        settings, chat_mode="arch-intake", prompt="do the thing", approval_mode="auto_edit"
    )
    assert argv[-1] == "do the thing"
    assert "-p" not in argv
    assert "--prompt" not in argv


def test_binary_prefix_is_preserved_verbatim(settings):
    argv = build_argv(
        settings, chat_mode="arch-critic", prompt="review", approval_mode="auto_edit"
    )
    assert argv[:2] == ["node", "/opt/bob/bundle/bob.js"]


def test_stream_json_and_chat_mode_are_always_present(settings):
    argv = build_argv(
        settings, chat_mode="arch-analyst", prompt="p", approval_mode="auto_edit"
    )
    assert argv[argv.index("--chat-mode") + 1] == "arch-analyst"
    assert argv[argv.index("--output-format") + 1] == "stream-json"


def test_auto_edit_is_emitted_as_approval_mode(settings):
    argv = build_argv(
        settings, chat_mode="arch-intake", prompt="p", approval_mode="auto_edit"
    )
    assert argv[argv.index("--approval-mode") + 1] == "auto_edit"
    assert "--yolo" not in argv


def test_yolo_is_emitted_as_the_bare_flag(settings):
    argv = build_argv(
        settings, chat_mode="arch-scaffold", prompt="p", approval_mode="yolo"
    )
    assert "--yolo" in argv
    assert "--approval-mode" not in argv


def test_optional_flags_are_omitted_when_unset(settings):
    argv = build_argv(
        settings, chat_mode="arch-intake", prompt="p", approval_mode="auto_edit"
    )
    assert "--max-coins" not in argv
    assert "-r" not in argv
    assert "--include-directories" not in argv


def test_optional_flags_are_emitted_when_set(settings, tmp_path):
    extra = tmp_path / "input"
    argv = build_argv(
        settings,
        chat_mode="arch-intake",
        prompt="p",
        approval_mode="auto_edit",
        include_directories=[extra],
        max_coins=12,
        resume="latest",
    )
    assert argv[argv.index("--include-directories") + 1] == str(extra)
    assert argv[argv.index("--max-coins") + 1] == "12"
    assert argv[argv.index("-r") + 1] == "latest"


def test_licence_and_auth_flags_come_from_settings(settings):
    argv = build_argv(
        settings, chat_mode="arch-intake", prompt="p", approval_mode="auto_edit"
    )
    assert "--accept-license" in argv
    assert argv[argv.index("--auth-method") + 1] == "api-key"


def test_every_argv_element_is_a_string(settings, tmp_path):
    argv = build_argv(
        settings,
        chat_mode="arch-intake",
        prompt="p",
        approval_mode="auto_edit",
        include_directories=[tmp_path],
        max_coins=3,
    )
    assert all(isinstance(a, str) for a in argv)


# --------------------------------------------------------------------------- #
# redact_argv
# --------------------------------------------------------------------------- #


def test_redact_truncates_only_the_prompt(settings):
    prompt = "z" * 900
    argv = build_argv(
        settings, chat_mode="arch-intake", prompt=prompt, approval_mode="auto_edit"
    )
    redacted = redact_argv(argv)
    assert redacted[:-1] == argv[:-1]
    assert len(redacted[-1]) < len(prompt)
    assert "chars)" in redacted[-1]


def test_redact_leaves_a_short_prompt_alone(settings):
    argv = build_argv(
        settings, chat_mode="arch-intake", prompt="short", approval_mode="auto_edit"
    )
    assert redact_argv(argv) == argv


def test_no_stage_carries_its_own_approval_mode():
    """The policy must have exactly one home.

    PIPELINE_STAGES used to hard-code an approval_mode per stage while
    bobcli.APPROVAL_BY_SLUG declared another. `spec.approval_mode or
    approval_for_slug(...)` let the copy win, so correcting the policy changed
    nothing at runtime and the analyst kept running without execute_command.
    A deliberate one-off override is still possible — it just has to be noticed.
    """
    from app.pipeline import PIPELINE_STAGES, VISION_STAGES

    for spec in (*PIPELINE_STAGES, *VISION_STAGES):
        assert spec.approval_mode is None, (
            f"{spec.id} pins approval_mode={spec.approval_mode!r}, which silently "
            f"overrides APPROVAL_BY_SLUG[{spec.slug!r}]"
        )


def test_the_effective_mode_of_every_script_running_stage_is_yolo():
    """End to end over the resolution the runner actually performs."""
    from app.pipeline import PIPELINE_STAGES
    from app.bobcli import approval_for_slug

    effective = {
        s.id: (s.approval_mode or approval_for_slug(s.slug)) for s in PIPELINE_STAGES
    }
    assert effective["intake"] == "auto_edit"
    for stage in ("analyst", "critic", "scaffold", "validator"):
        assert effective[stage] == "yolo", f"{stage} resolves to {effective[stage]}"
