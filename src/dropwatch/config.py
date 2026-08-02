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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .errors import ConfigError
from .secrets import Secret, registry

DEFAULT_TIMEOUT = 20.0
DEFAULT_CACHE_MAX_AGE_HOURS = 24.0
USER_AGENT = "dropwatch/1.0 (+local personal library tool)"


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


def settings_path(environ: dict[str, str] | None = None) -> Path:
    """The one file settings are read from and written to.

    `$DROPWATCH_ENV` moves it, which is the whole of the override story: there
    was also a `--env-file` flag doing exactly this on every subcommand, and a
    `./.env` in the working directory. Both are gone. `.env` files are common
    in project directories, and one silently deciding which server gets scanned
    is not a trade worth making for the convenience.
    """
    environ = os.environ if environ is None else environ
    override = environ.get("DROPWATCH_ENV")
    if override:
        return Path(override).expanduser()
    return user_config_dir(environ) / ".env"


def find_env_file(environ: dict[str, str] | None = None) -> Path | None:
    """The settings file, or None when it does not exist yet."""
    path = settings_path(environ)
    return path if path.is_file() else None


def _check_positive_number(value: str, key: str) -> str:
    """Accept a positive number, returning it in canonical form."""
    try:
        number = float(value)
    except ValueError:
        raise ConfigError(f"{key} must be a number, got {value!r}") from None
    if number <= 0:
        raise ConfigError(f"{key} must be greater than 0, got {number:g}")
    return f"{number:g}"


def _check_path(value: str, key: str) -> str:
    """Accept a writable-looking path, stored absolute."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if path.is_dir():
        raise ConfigError(
            f"{key} must name a file, not a directory: {path}",
            hint="For example: ~/.local/state/dropwatch/state.sqlite3",
        )
    return str(path)


@dataclass(frozen=True)
class Setting:
    """One user-facing setting.

    `check` validates and canonicalises a value at the moment it is set, so a
    bad one is rejected by the command that caused it rather than surfacing on
    some unrelated command later.
    """

    key: str
    help: str
    secret: bool = False
    required: bool = False
    default: str | None = None
    #: Effective value when unset and there is no literal default, for display.
    implicit: Callable[[], str] | None = None
    check: Callable[[str, str], str] | None = None

    @property
    def env_var(self) -> str:
        """The environment variable, derived rather than declared.

        One rule with no exceptions: DROPWATCH_ plus the key. The prefix keeps
        a stray CACHE_PATH or NAVIDROME_PASSWORD in someone's shell from
        quietly winning over their settings file, and deriving it means the
        name you type and the name an error prints are always the same word.
        """
        return "DROPWATCH_" + self.key.upper().replace("-", "_")

    def validate(self, value: str) -> str:
        return self.check(value, self.key) if self.check else value

    @property
    def effective_default(self) -> str | None:
        """What applies when nothing is set. None for required settings.

        Some defaults are literals ("20"), others are computed at run time (the
        state file path). Callers should not have to care which.
        """
        if self.default is not None:
            return self.default
        return self.implicit() if self.implicit is not None else None


SETTINGS: tuple[Setting, ...] = (
    Setting("url", "Navidrome base URL", required=True,
            check=lambda v, _k: normalize_url(v)),
    Setting("username", "Navidrome username", required=True),
    Setting("password", "Navidrome password", secret=True, required=True),
    Setting("timeout", "Request timeout in seconds", default="20",
            check=_check_positive_number),
    Setting("cache-path", "Where local state is kept",
            implicit=lambda: str(default_state_dir() / "state.sqlite3"),
            check=_check_path),
    Setting("cache-max-age", "Cache lifetime in hours", default="24",
            check=_check_positive_number),
    Setting("types", "Release types to report, comma separated (album, ep, single)",
            implicit=lambda: "all",
            check=lambda v, _k: ",".join(sorted(parse_release_types(v))).lower()),
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
        """What `config` prints. Never "(not set)" for something that has an
        effective value — a state file path exists whether or not you chose it,
        and showing nothing there sent people looking for a setting to fix."""
        if self.setting.secret and self.value:
            return "********"
        if self.value is not None:
            return self.value
        return self.setting.effective_default or "(not set)"


def describe_settings(environ: dict[str, str] | None = None) -> list[ResolvedSetting]:
    """Effective value and provenance for every setting.

    Exists so `config` can answer "why isn't my change taking effect", which a
    shell variable shadowing the file still makes possible.
    """
    environ = os.environ if environ is None else environ
    env_path = find_env_file(environ)
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


#: A bare value survives the round trip unless it has edge whitespace or
#: characters the reader would otherwise consume.
_NEEDS_QUOTING = re.compile(r'^\s|\s$|[\n\r"\']')


def encode_value(value: str) -> str:
    """Quote a value when writing it bare would not read back identically.

    Passwords are the reason. `hunter2 ` with a trailing space, or one that
    genuinely contains quotes, used to be written bare and read back altered —
    silently, and after `setup` had already tested the unaltered version
    against the server and reported success.
    """
    if value and not _NEEDS_QUOTING.search(value):
        return value
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


_ESCAPES = {"n": "\n", "r": "\r", '"': '"', "\\": "\\"}


def decode_value(raw: str) -> str:
    """Inverse of `encode_value`, tolerant of hand-written files."""
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]  # single quotes are literal, as in POSIX shells
    if not (len(raw) >= 2 and raw[0] == raw[-1] == '"'):
        return raw

    inner, out, i = raw[1:-1], [], 0
    while i < len(inner):
        if inner[i] == "\\" and i + 1 < len(inner):
            out.append(_ESCAPES.get(inner[i + 1], "\\" + inner[i + 1]))
            i += 2
        else:
            out.append(inner[i])
            i += 1
    return "".join(out)


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
        lines.append(f"{setting.env_var}={encode_value(value)}")
        lines.append("")
    if extra:
        lines.append("# Preserved from a hand-edited file")
        for key, value in sorted(extra.items()):
            lines.append(f"{key}={encode_value(value)}")
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


def _number(raw: str | None, default: float, key: str) -> float:
    """Read a numeric setting, validating it the same way `config set` does.

    A value can still reach here unvalidated — hand-edited file, exported shell
    variable — so the check is not redundant, but it is the same check.
    """
    if raw is None or raw.strip() == "":
        return default
    return float(_check_positive_number(raw, key))


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
        value = decode_value(value)
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
            "url is not set.",
            hint="Example: dropwatch config set url http://your-server:4533",
        )

    if "://" not in value:
        value = "http://" + value

    parts = urlsplit(value)

    if parts.scheme not in ("http", "https"):
        raise ConfigError(
            f"url must use http or https, got {parts.scheme!r}.",
            hint="Example: dropwatch config set url http://your-server:4533",
        )
    if not parts.hostname:
        raise ConfigError(
            f"url has no hostname: {raw!r}",
            hint="Example: dropwatch config set url http://your-server:4533",
        )
    if parts.query or parts.fragment:
        raise ConfigError(
            "url must not contain a query string or fragment.",
            hint="Use just the base URL, for example http://your-server:4533",
        )
    if parts.username or parts.password:
        raise ConfigError(
            "Do not put credentials in the url.",
            hint="Use the username and password settings instead.",
        )
    try:
        port = parts.port
    except ValueError:
        raise ConfigError(f"url has an invalid port: {raw!r}") from None
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError(f"url port must be 1-65535, got {port}")

    path = parts.path.rstrip("/")
    # A base URL ending in /rest is a common mix-up; /rest is appended for us.
    if path.endswith("/rest"):
        path = path[: -len("/rest")]

    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def load_config(
    environ: dict[str, str] | None = None,
    warn_stream=sys.stderr,
    require_navidrome: bool = True,
) -> Config:
    """Build a validated Config from the environment and the settings file.

    Commands that only read the local state file — listing mappings, recording
    a block, inspecting the cache — pass ``require_navidrome=False``. They
    genuinely do not need credentials, and refusing to run without them was
    just an artefact of every command sharing one loader. The resulting Config
    records what was missing in `missing_navidrome`, so anything that later
    turns out to need a server can still fail with the usual message.
    """
    environ = dict(os.environ if environ is None else environ)

    expected = settings_path(environ)
    env_path = find_env_file(environ)

    # A DROPWATCH_ENV pointing nowhere is a typo, not an absent config, and
    # saying so beats listing the path as somewhere we merely looked.
    if env_path is None and environ.get("DROPWATCH_ENV"):
        raise ConfigError(
            f"No such configuration file: {expected}",
            hint=(
                "$DROPWATCH_ENV points there. Unset it to use "
                f"{user_config_dir(environ) / '.env'}, or run "
                f"`{invocation_name()} setup`."
            ),
        )

    file_values = load_dotenv(env_path) if env_path else {}
    if file_values:
        warn_if_world_readable(env_path, warn_stream)

    def get(key: str) -> str | None:
        """Raw value for a setting. A real environment variable wins."""
        env_var = SETTINGS_BY_KEY[key].env_var
        value = environ.get(env_var)
        if value is not None and value.strip() != "":
            return value
        return file_values.get(env_var)

    if env_path is None and not get("url"):
        if require_navidrome:
            raise ConfigError(
                "No configuration found.",
                hint=(
                    f"Run `{invocation_name()} setup` to configure it.\n"
                    f"  Expected a settings file at: {expected}"
                ),
            )

    missing: list[str] = []

    def required(key: str) -> str:
        """A required credential, named the way you would set it.

        Errors quote the config key, not the environment variable behind it:
        you typed `url`, so being told the environment variable was unset made
        you translate before you could act.
        """
        setting = SETTINGS_BY_KEY[key]
        raw = get(key) or ""
        # Surrounding whitespace is formatting in a URL or username and
        # content in a password, so only the former gets tidied.
        if not setting.secret:
            raw = raw.strip()
        if raw.strip():
            return setting.validate(raw)
        if require_navidrome:
            fix = "config password" if setting.secret else f"config set {key} <value>"
            raise ConfigError(
                f"{key} is not set.", hint=f"Run `{invocation_name()} {fix}`."
            )
        missing.append(key)
        return ""

    url = required("url")
    username = required("username")
    password = required("password")

    cache_raw = get("cache-path")
    cache_path = (
        Path(cache_raw).expanduser() if cache_raw else default_state_dir() / "state.sqlite3"
    )

    config = Config(
        navidrome_url=url,
        navidrome_username=username,
        navidrome_password=Secret(password),
        request_timeout=_number(get("timeout"), DEFAULT_TIMEOUT, "timeout"),
        cache_path=cache_path,
        cache_max_age_hours=_number(
            get("cache-max-age"), DEFAULT_CACHE_MAX_AGE_HOURS, "cache-max-age"
        ),
        release_types=parse_release_types(get("types")),
        missing_navidrome=tuple(missing),
    )

    # Register the password so the logging filter can scrub it.
    if password:
        registry.register(config.navidrome_password)
    return config
