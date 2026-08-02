"""Configuration loading and validation.

Real environment variables win over ``.env`` so a shell override always takes
effect. Only non-secret values are ever echoed back to the user.
"""

from __future__ import annotations

import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .errors import ConfigError
from .secrets import Secret, registry

DEFAULT_TIMEOUT = 20.0
DEFAULT_CACHE_MAX_AGE_HOURS = 24.0
USER_AGENT = "dropwatch/2.0 (+local personal library tool)"


def invocation_name() -> str:
    """How the user actually invoked us, for hint text they can copy-paste.

    There are three ways in — the installed `dropwatch`, a direct
    `python3 dropwatch.py`, and `python3 -m dropwatch` — so any hint naming
    the command has to be derived rather than hardcoded.
    """
    argv0 = Path(sys.argv[0] or "").name
    if argv0 == "__main__.py":
        return "python3 -m dropwatch"
    if argv0.endswith(".py"):
        return f"python3 {argv0}"
    return argv0 or "dropwatch"


def default_state_dir() -> Path:
    override = os.environ.get("DROPWATCH_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "state" / "dropwatch"


def user_config_dir(environ: dict[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    override = environ.get("DROPWATCH_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "dropwatch"


def candidate_env_paths(
    explicit: Path | None = None, environ: dict[str, str] | None = None
) -> list[Path]:
    """Where a .env may live, most specific first.

    The working directory is searched before the user config directory so that
    running inside a checkout still picks up a local .env, while an installed
    command run from anywhere falls back to ~/.config/dropwatch/.env.
    """
    environ = os.environ if environ is None else environ
    if explicit is not None:
        return [Path(explicit).expanduser()]
    override = environ.get("DROPWATCH_ENV")
    if override:
        return [Path(override).expanduser()]
    return [Path.cwd() / ".env", user_config_dir(environ) / ".env"]


def find_env_file(
    explicit: Path | None = None, environ: dict[str, str] | None = None
) -> Path | None:
    for path in candidate_env_paths(explicit, environ):
        if path.is_file():
            return path
    return None


@dataclass(frozen=True)
class Setting:
    """One user-facing setting: a friendly CLI name over an env var."""

    key: str
    env_var: str
    help: str
    secret: bool = False
    required: bool = False
    default: str | None = None


SETTINGS: tuple[Setting, ...] = (
    Setting("url", "NAVIDROME_URL", "Navidrome base URL", required=True),
    Setting("username", "NAVIDROME_USERNAME", "Navidrome username", required=True),
    Setting(
        "password",
        "NAVIDROME_PASSWORD",
        "Navidrome password",
        secret=True,
        required=True,
    ),
    Setting("timeout", "REQUEST_TIMEOUT_SECONDS", "Request timeout in seconds", default="20"),
    Setting("cache-path", "CACHE_PATH", "Where local state is kept"),
    Setting("cache-max-age", "CACHE_MAX_AGE_HOURS", "Cache lifetime in hours", default="24"),
    Setting(
        "types",
        "RELEASE_TYPES",
        "Release types to report, comma separated (album, ep, single)",
    ),
)

SETTINGS_BY_KEY = {s.key: s for s in SETTINGS}
SETTINGS_BY_ENV = {s.env_var: s for s in SETTINGS}


@dataclass
class ResolvedSetting:
    """A setting's effective value and where it came from."""

    setting: Setting
    value: str | None
    source: str  # "environment", a file path, or "default"

    @property
    def display(self) -> str:
        if self.value is None:
            return "(not set)"
        return "********" if self.setting.secret else self.value


def describe_settings(
    env_file: Path | None = None, environ: dict[str, str] | None = None
) -> list[ResolvedSetting]:
    """Effective value and provenance for every setting.

    Exists so `config` can answer "why isn't my change taking effect",
    which is the obvious failure mode of a four-level precedence chain.
    """
    environ = os.environ if environ is None else environ
    env_path = find_env_file(env_file, environ)
    file_values = load_dotenv(env_path) if env_path else {}

    resolved: list[ResolvedSetting] = []
    for setting in SETTINGS:
        from_env = environ.get(setting.env_var)
        if from_env is not None and from_env.strip():
            resolved.append(ResolvedSetting(setting, from_env, "environment"))
            continue
        from_file = file_values.get(setting.env_var)
        if from_file is not None and from_file.strip():
            resolved.append(ResolvedSetting(setting, from_file, str(env_path)))
            continue
        resolved.append(ResolvedSetting(setting, setting.default, "default"))
    return resolved


CONFIG_HEADER = """\
# dropwatch configuration.
# Written by `dropwatch setup` / `dropwatch config`.
# Safe to edit by hand; keep it private (mode 600).
"""


def save_settings(path: Path, values: dict[str, str]) -> None:
    """Write the config file atomically at mode 600.

    Written to a temporary file in the same directory and renamed, so a crash
    mid-write cannot truncate a working config, and the file is never briefly
    readable by anyone else.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:  # pragma: no cover - platform dependent
        pass

    existing = load_dotenv(path)
    # Anything the user added by hand that we do not manage is preserved.
    extra = {k: v for k, v in existing.items() if k not in SETTINGS_BY_ENV}

    lines = [CONFIG_HEADER]
    for setting in SETTINGS:
        value = values.get(setting.env_var)
        if value is None or value == "":
            continue
        lines.append(f"# {setting.help}")
        lines.append(f"{setting.env_var}={value}")
        lines.append("")
    if extra:
        lines.append("# Preserved from a hand-edited file")
        for key, value in sorted(extra.items()):
            lines.append(f"{key}={value}")
        lines.append("")

    body = "\n".join(lines)
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".env.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(body)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@dataclass
class Config:
    navidrome_url: str
    navidrome_username: str
    navidrome_password: Secret
    request_timeout: float = DEFAULT_TIMEOUT
    cache_path: Path = field(default_factory=lambda: default_state_dir() / "state.sqlite3")
    cache_max_age_hours: float = DEFAULT_CACHE_MAX_AGE_HOURS
    release_types: frozenset[str] = frozenset()
    verify_tls: bool = True
    #: Set when the config was loaded without demanding Navidrome credentials.
    missing_navidrome: tuple[str, ...] = ()

    @property
    def rest_base(self) -> str:
        """Base URL for Subsonic REST calls, e.g. ``http://host:4533/rest``."""
        return self.navidrome_url.rstrip("/") + "/rest"

    @property
    def has_navidrome(self) -> bool:
        return not self.missing_navidrome


def _parse_float(raw: str | None, default: float, name: str) -> float:
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if value <= 0:
        raise ConfigError(f"{name} must be greater than 0, got {value}")
    return value


#: Accepted spellings for the `types` setting and the --type flag.
RELEASE_TYPE_ALIASES = {
    "album": "Album",
    "albums": "Album",
    "ep": "EP",
    "eps": "EP",
    "single": "Single",
    "singles": "Single",
    "unknown": "Unknown",
    "unclassified": "Unknown",
}


def parse_release_types(raw: str | None) -> frozenset[str]:
    """Parse "album,ep" into canonical type names. Empty means every type."""
    if not raw or not raw.strip():
        return frozenset()
    names = set()
    for token in re.split(r"[,\s]+", raw.strip()):
        if not token:
            continue
        canonical = RELEASE_TYPE_ALIASES.get(token.casefold())
        if canonical is None:
            valid = ", ".join(sorted({v.lower() for v in RELEASE_TYPE_ALIASES.values()}))
            raise ConfigError(
                f"Unknown release type {token!r}.", hint=f"Valid types: {valid}"
            )
        names.add(canonical)
    return frozenset(names)


def load_dotenv(path: Path) -> dict[str, str]:
    """Parse a minimal ``KEY=value`` file. Missing file yields an empty dict."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def warn_if_world_readable(path: Path, stream=sys.stderr) -> bool:
    """Warn when a secret-bearing file is readable by other local users."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        print(
            f"warning: {path} is readable by other users on this Mac. "
            f"Run: chmod 600 {path}",
            file=stream,
        )
        return True
    return False


def normalize_url(raw: str) -> str:
    """Validate and canonicalise the Navidrome base URL.

    Accepts a bare ``host:port`` and assumes http, since that is the common
    shape on a private network. Rejects anything that cannot address a server.
    """
    value = (raw or "").strip()
    if not value:
        raise ConfigError(
            "NAVIDROME_URL is not set.",
            hint="Example: NAVIDROME_URL=http://your-server:4533",
        )

    if "://" not in value:
        value = "http://" + value

    parts = urlsplit(value)

    if parts.scheme not in ("http", "https"):
        raise ConfigError(
            f"NAVIDROME_URL must use http or https, got {parts.scheme!r}.",
            hint="Example: NAVIDROME_URL=http://your-server:4533",
        )
    if not parts.hostname:
        raise ConfigError(
            f"NAVIDROME_URL has no hostname: {raw!r}",
            hint="Example: NAVIDROME_URL=http://your-server:4533",
        )
    if parts.query or parts.fragment:
        raise ConfigError(
            "NAVIDROME_URL must not contain a query string or fragment.",
            hint="Use just the base URL, for example http://your-server:4533",
        )
    if parts.username or parts.password:
        raise ConfigError(
            "Do not put credentials in NAVIDROME_URL.",
            hint="Use NAVIDROME_USERNAME and NAVIDROME_PASSWORD instead.",
        )
    try:
        port = parts.port
    except ValueError:
        raise ConfigError(f"NAVIDROME_URL has an invalid port: {raw!r}") from None
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError(f"NAVIDROME_URL port must be 1-65535, got {port}")

    path = parts.path.rstrip("/")
    # A base URL ending in /rest is a common mix-up; /rest is appended for us.
    if path.endswith("/rest"):
        path = path[: -len("/rest")]

    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def load_config(
    env_file: Path | None = None,
    environ: dict[str, str] | None = None,
    warn_stream=sys.stderr,
    require_navidrome: bool = True,
) -> Config:
    """Build a validated Config from the environment and an optional .env.

    Commands that only read the local state file — listing mappings, recording
    a block, inspecting the cache — pass ``require_navidrome=False``. They
    genuinely do not need credentials, and refusing to run without them was
    just an artefact of every command sharing one loader. The resulting Config
    records what was missing in `missing_navidrome`, so anything that later
    turns out to need a server can still fail with the usual message.
    """
    environ = dict(os.environ if environ is None else environ)

    if env_file is not None and not Path(env_file).expanduser().is_file():
        raise ConfigError(
            f"No such configuration file: {env_file}",
            hint=(
                "Check the --env-file path, or run "
                f"`{invocation_name()} setup` to configure it."
            ),
        )

    searched = candidate_env_paths(env_file, environ)
    env_path = find_env_file(env_file, environ)

    file_values = load_dotenv(env_path) if env_path else {}
    if file_values:
        warn_if_world_readable(env_path, warn_stream)

    def get(key: str) -> str | None:
        # Real environment variables take precedence over the .env file.
        value = environ.get(key)
        if value is not None and value.strip() != "":
            return value
        return file_values.get(key)

    if env_path is None and not get("NAVIDROME_URL"):
        if require_navidrome:
            locations = "\n    ".join(str(p) for p in searched)
            raise ConfigError(
                "No configuration found.",
                hint=(
                    f"Run `{invocation_name()} setup` to configure it, or set NAVIDROME_URL in the "
                    f"environment.\n  Looked in:\n    {locations}"
                ),
            )

    where = f" in {env_path}" if env_path else ""
    missing: list[str] = []

    raw_url = get("NAVIDROME_URL") or ""
    if raw_url.strip() or require_navidrome:
        # An empty URL under require_navidrome raises with the standard hint.
        url = normalize_url(raw_url)
    else:
        url = ""
        missing.append("NAVIDROME_URL")

    username = (get("NAVIDROME_USERNAME") or "").strip()
    if not username:
        if require_navidrome:
            raise ConfigError(
                "NAVIDROME_USERNAME is not set.",
                hint=f"Add NAVIDROME_USERNAME{where}.",
            )
        missing.append("NAVIDROME_USERNAME")

    password = get("NAVIDROME_PASSWORD") or ""
    if not password:
        if require_navidrome:
            raise ConfigError(
                "NAVIDROME_PASSWORD is not set.",
                hint=f"Add NAVIDROME_PASSWORD{where}.",
            )
        missing.append("NAVIDROME_PASSWORD")

    cache_raw = get("CACHE_PATH")
    cache_path = (
        Path(cache_raw).expanduser() if cache_raw else default_state_dir() / "state.sqlite3"
    )

    config = Config(
        navidrome_url=url,
        navidrome_username=username,
        navidrome_password=Secret(password),
        request_timeout=_parse_float(
            get("REQUEST_TIMEOUT_SECONDS"), DEFAULT_TIMEOUT, "REQUEST_TIMEOUT_SECONDS"
        ),
        cache_path=cache_path,
        cache_max_age_hours=_parse_float(
            get("CACHE_MAX_AGE_HOURS"),
            DEFAULT_CACHE_MAX_AGE_HOURS,
            "CACHE_MAX_AGE_HOURS",
        ),
        release_types=parse_release_types(get("RELEASE_TYPES")),
        missing_navidrome=tuple(missing),
    )

    # Register the password so the logging filter can scrub it.
    if password:
        registry.register(config.navidrome_password)
    return config
