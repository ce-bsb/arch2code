"""Environment-driven configuration.

Every knob is an ARCH2CODE_* environment variable. Nothing is hardcoded to a
machine-specific path except two documented defaults that are true on the
workstation this tool was built for and are overridable:

  * ARCH2CODE_PYTHON defaults to /opt/anaconda3/bin/python, because the system
    python3 is 3.9.6 and has neither mcp nor httpx nor pydantic nor Pillow.
  * ARCH2CODE_PROJECT_ROOT defaults to the parent of webapp/, i.e. the repo.

No secret is ever read into a Settings field. The watsonx credentials in
mcp/arch_vision/.env are loaded only into the environment handed to child
processes, and only their KEY NAMES are ever reported anywhere.
"""

from __future__ import annotations

import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, MutableMapping

__all__ = [
    "ConfigError",
    "Settings",
    "load_settings",
    "parse_bob_bin",
    "load_dotenv_into",
    "subprocess_env",
    "DEFAULT_PYTHON_BIN",
]

DEFAULT_PYTHON_BIN = "/opt/anaconda3/bin/python"

# Truthy spellings accepted for every boolean variable.
_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n", ""}


class ConfigError(RuntimeError):
    """Raised when the environment cannot be turned into a usable Settings.

    Note: app.errors also defines a ConfigError, an AppError subclass that
    carries an HTTP status. They are deliberately distinct — this one is a
    plain startup-time failure with no HTTP meaning — and the exception handler
    in app.errors maps this one to a 500 with a remedy as well.
    """


@dataclass(frozen=True)
class Settings:
    """Immutable, fully resolved configuration for one process."""

    project_root: Path
    webapp_root: Path
    runs_root: Path
    uploads_root: Path
    static_root: Path

    # Working directory for every Bob subprocess. Load-bearing: Bob resolves
    # --chat-mode from the .bob/custom_modes.yaml of THIS directory.
    bob_cwd: Path
    # ("bob",) or ("node", "/abs/path/bob.js"). Empty when no Bob was found,
    # which is a reported health failure rather than a startup crash.
    bob_bin: tuple[str, ...]
    bob_pty: bool
    bob_max_coins: int | None
    bob_accept_license: bool
    bob_auth_method: str

    python_bin: str
    mcp_server_path: Path
    mcp_env_file: Path
    #: webapp/.env — the application's own file. Holds BOBSHELL_API_KEY and the
    #: ARCH2CODE_* settings. Loaded into the child environment for the same
    #: reason mcp_env_file is: Bob authenticates from its environment, and a key
    #: that only the parent can see leaves every stage failing pre-flight with an
    #: empty stdout, which is the hardest failure in this system to read.
    webapp_env_file: Path

    max_upload_mb: float
    stage_timeout_s: float
    vision_timeout_s: float
    sse_heartbeat_s: float

    host: str
    port: int
    max_concurrent_pipeline_runs: int

    # ------------------------------------------------------------------
    # Convenience accessors used across the app
    # ------------------------------------------------------------------

    @property
    def bob_available(self) -> bool:
        return bool(self.bob_bin)

    @property
    def max_upload_bytes(self) -> int:
        return int(self.max_upload_mb * 1024 * 1024)

    @property
    def custom_modes_path(self) -> Path:
        return self.project_root / ".bob" / "custom_modes.yaml"

    @property
    def critic_rules_dir(self) -> Path:
        return self.project_root / ".bob" / "rules-arch-critic"

    @property
    def capture_script(self) -> Path:
        return (self.project_root / ".bob" / "skills" / "diagram-intake"
                / "scripts" / "capture_diagram.py")

    @property
    def drawio_script(self) -> Path:
        return (self.project_root / ".bob" / "skills" / "diagram-intake"
                / "scripts" / "parse_drawio.py")

    @property
    def validate_air_script(self) -> Path:
        return (self.project_root / ".bob" / "skills" / "air-normalizer"
                / "scripts" / "validate_air.py")

    @property
    def profiles_root(self) -> Path:
        """Where the platform target profiles actually live.

        One property because three components independently guessed a different
        location: the profile engine writes them here, ``app/export.py`` looked
        for ``<project_root>/targets/<id>/target.yaml`` and the front end asked
        an endpoint that did not exist. Everything now resolves through this.
        """
        return (self.project_root / ".bob" / "skills" / "scaffold-from-air"
                / "profiles")

    @property
    def target_engine_script(self) -> Path:
        """The CLI that negotiates an AIR against a profile (exit 0/1/2/3)."""
        return (self.project_root / ".bob" / "skills" / "scaffold-from-air"
                / "scripts" / "target_engine.py")

    def ensure_dirs(self) -> None:
        """Create the directories the app owns. Never touches the repo tree."""
        for path in (self.runs_root, self.uploads_root, self.static_root):
            path.mkdir(parents=True, exist_ok=True)


# ----------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------


def _flag(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(
        f"{key}={raw!r} is not a boolean. Use one of 1/0, true/false, yes/no, on/off."
    )


def _number(env: Mapping[str, str], key: str, default: float) -> float:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover - trivial
        raise ConfigError(f"{key}={raw!r} is not a number.") from exc


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - trivial
        raise ConfigError(f"{key}={raw!r} is not an integer.") from exc


def _optional_int(env: Mapping[str, str], key: str) -> int | None:
    raw = (env.get(key) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - trivial
        raise ConfigError(f"{key}={raw!r} is not an integer.") from exc


def parse_bob_bin(raw: str | None) -> tuple[str, ...]:
    """Turn ARCH2CODE_BOB_BIN into an argv prefix.

        "bob"                 -> ("bob",)
        "node /abs/bob.js"    -> ("node", "/abs/bob.js")
        "/abs/bob.js"         -> ("node", "/abs/bob.js")
        None / ""             -> ("bob",) if one is on PATH

    Split with shlex so a quoted path with spaces survives; never through a
    shell, so nothing in the value can be interpreted as a command.

    Raises ConfigError when there is nothing to run. load_settings catches that
    and records an empty bob_bin, because the app has to start in order to
    show the user what is broken.
    """
    value = (raw or "").strip()

    if not value:
        found = shutil.which("bob")
        if found:
            return (found,)
        raise ConfigError(
            "No Bob binary found. Set ARCH2CODE_BOB_BIN to either an executable "
            "on PATH (e.g. \"bob\") or the bundle invocation "
            "(e.g. \"node /path/to/bundle/bob.js\")."
        )

    try:
        parts = shlex.split(value)
    except ValueError as exc:
        raise ConfigError(
            f"ARCH2CODE_BOB_BIN={raw!r} could not be split into arguments: {exc}. "
            "Quote the path if it contains spaces."
        ) from exc

    if not parts:
        raise ConfigError(f"ARCH2CODE_BOB_BIN={raw!r} is empty after parsing.")

    # A bare .js path is a bundle; it needs node in front of it.
    if len(parts) == 1 and parts[0].lower().endswith(".js"):
        return ("node", parts[0])
    if parts[0].lower().endswith(".js"):
        return ("node", *parts)
    return tuple(parts)


def load_dotenv_into(path: Path, target: MutableMapping[str, str]) -> list[str]:
    """Load a dotenv file into `target` and return the KEY NAMES loaded.

    Values are never returned, never logged and never surfaced by the API. The
    real environment always wins: a key already present in `target` is left
    untouched, so an operator can override a file value from the shell.

    A missing or unreadable file is not an error — it returns an empty list and
    the health probe reports the consequence.
    """
    names: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return names

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].strip()
        key, sep, value = stripped.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        names.append(key)
        if key not in target or not target.get(key):
            target[key] = value
    return names


def subprocess_env(
    settings: Settings, extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Base environment for every child process (Bob, the MCP server, scripts).

    Both .env files are merged in so a child can authenticate — the watsonx keys
    from mcp/arch_vision/.env and BOBSHELL_API_KEY from webapp/.env — but they
    are never echoed back to the client: StageDetail returns env_keys (names)
    only.

    The real environment wins over both files (``load_dotenv_into`` only fills
    what is absent), which is what makes the container work: Code Engine injects
    the secret directly and there is no .env on disk at all.
    """
    env: dict[str, str] = dict(os.environ)
    load_dotenv_into(settings.mcp_env_file, env)
    load_dotenv_into(settings.webapp_env_file, env)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env["ARCH2CODE_PROJECT_ROOT"] = str(settings.project_root)
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from the environment. Defaults to os.environ.

    Never raises for a missing Bob: a machine with no Bob install must still be
    able to run Mode A, and the health report is what tells the user why Mode B
    is unavailable.
    """
    env = os.environ if env is None else env

    webapp_root = Path(__file__).resolve().parent.parent
    raw_root = (env.get("ARCH2CODE_PROJECT_ROOT") or "").strip()
    project_root = Path(raw_root).expanduser().resolve() if raw_root else webapp_root.parent

    raw_cwd = (env.get("ARCH2CODE_BOB_CWD") or "").strip()
    bob_cwd = Path(raw_cwd).expanduser().resolve() if raw_cwd else project_root

    try:
        bob_bin = parse_bob_bin(env.get("ARCH2CODE_BOB_BIN"))
    except ConfigError:
        # Reported by the bob_binary health probe, which blocks Mode B only.
        bob_bin = ()

    python_bin = (env.get("ARCH2CODE_PYTHON") or "").strip() or DEFAULT_PYTHON_BIN

    return Settings(
        project_root=project_root,
        webapp_root=webapp_root,
        runs_root=webapp_root / "runs",
        uploads_root=webapp_root / "uploads",
        static_root=webapp_root / "static",
        bob_cwd=bob_cwd,
        bob_bin=bob_bin,
        bob_pty=_flag(env, "ARCH2CODE_BOB_PTY", False),
        bob_max_coins=_optional_int(env, "ARCH2CODE_BOB_MAX_COINS"),
        bob_accept_license=_flag(env, "ARCH2CODE_BOB_ACCEPT_LICENSE", True),
        bob_auth_method=(env.get("ARCH2CODE_BOB_AUTH_METHOD") or "api-key").strip(),
        python_bin=python_bin,
        mcp_server_path=project_root / "mcp" / "arch_vision" / "server.py",
        mcp_env_file=project_root / "mcp" / "arch_vision" / ".env",
        webapp_env_file=webapp_root / ".env",
        max_upload_mb=_number(env, "ARCH2CODE_MAX_UPLOAD_MB", 25.0),
        stage_timeout_s=_number(env, "ARCH2CODE_STAGE_TIMEOUT_S", 1800.0),
        vision_timeout_s=_number(env, "ARCH2CODE_VISION_TIMEOUT_S", 180.0),
        sse_heartbeat_s=_number(env, "ARCH2CODE_SSE_HEARTBEAT_S", 15.0),
        host=(env.get("ARCH2CODE_HOST") or "127.0.0.1").strip(),
        port=_int(env, "ARCH2CODE_PORT", 8765),
        max_concurrent_pipeline_runs=max(
            1, _int(env, "ARCH2CODE_MAX_CONCURRENT_PIPELINE_RUNS", 1)
        ),
    )
