"""Interactive first-run setup.

The wizard's job is not to collect values — a text editor does that fine — but
to end with a configuration that is known to *work*. So it tests the
connection before saving and lets the user correct a bad answer in place,
rather than reporting the problem on some later command.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

from .config import (
    SETTINGS_BY_KEY,
    Config,
    describe_settings,
    invocation_name,
    load_dotenv,
    normalize_url,
    save_settings,
    settings_path,
)
from .errors import (
    ConfigError,
    NavidromeAuthError,
    NavidromeError,
    DropWatchError,
)
from .secrets import Secret

MAX_ATTEMPTS = 3


def prompt_password(label: str = "Password", current: str | None = None, indent: str = "") -> str:
    """Ask for a password twice, since it is not echoed to be checked.

    Shared with `config password` so the two ways of setting a password behave
    the same; they used to differ, one confirming and one not.
    """
    suffix = " [keep current]" if current else ""
    while True:
        value = getpass.getpass(f"{indent}{label}{suffix}: ")
        if not value:
            if current:
                return current
            print(f"{indent}  A password is required.", file=sys.stderr)
            continue
        if value == getpass.getpass(f"{indent}Confirm: "):
            return value
        print(f"{indent}  They did not match. Try again.", file=sys.stderr)


def _prompt(
    label: str,
    current: str | None = None,
    secret: bool = False,
    placeholder: str | None = None,
) -> str:
    """Ask for one value.

    A previously saved value is offered in brackets and kept on Enter. A
    placeholder only shows the expected shape and is never submitted, so the
    tool never guesses at somebody's hostname.
    """
    if secret:
        return prompt_password(label, current, indent="  ")

    if current:
        suffix = f" [{current}]"
    elif placeholder:
        suffix = f" (e.g. {placeholder})"
    else:
        suffix = ""
    value = input(f"  {label}{suffix}: ").strip()
    return value or (current or "")


def _confirm(question: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = input(f"{question} [{hint}]: ").strip().lower()
    if not answer:
        return default
    return answer.startswith("y")


def _test_connection(url: str, username: str, password: str, timeout: float) -> str:
    """Ping Navidrome with the collected values. Returns the server label."""
    # Imported here to keep module import cheap for non-setup commands.
    from .config import USER_AGENT
    from .http import HttpClient
    from .navidrome import NavidromeClient

    config = Config(
        navidrome_url=url,
        navidrome_username=username,
        navidrome_password=Secret(password),
        request_timeout=timeout,
    )
    client = NavidromeClient(config, HttpClient(timeout=timeout, user_agent=USER_AGENT))
    return client.ping()


def run_setup(environ: dict[str, str] | None = None) -> int:
    """Guided setup. Returns a process exit code."""
    from .errors import ExitCode

    if not sys.stdin.isatty():
        raise ConfigError(
            "setup needs an interactive terminal.",
            hint=(
                f"Use `{invocation_name()} config set url <value>` in scripts, "
                "or set DROPWATCH_URL, DROPWATCH_USERNAME and DROPWATCH_PASSWORD "
                "in the environment."
            ),
        )

    target = settings_path(environ)
    resolved = {r.setting.key: r for r in describe_settings(environ)}
    reconfiguring = target.is_file()

    print(f"Configuring dropwatch\n  Settings file: {target}")
    if reconfiguring:
        print("  Press Enter to keep the current value.")
    print()

    # Warn when the environment will mask whatever we write here.
    masked = [r for r in resolved.values() if r.source == "environment"]
    if masked:
        names = ", ".join(r.setting.env_var for r in masked)
        print(
            f"  Note: {names} is set in your environment and overrides this file.\n",
            file=sys.stderr,
        )

    url = resolved["url"].value
    username = resolved["username"].value
    password = resolved["password"].value
    timeout = float(resolved["timeout"].value or 20)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        url_input = _prompt("Navidrome URL", url, placeholder="http://host:4533")
        try:
            url = normalize_url(url_input)
        except ConfigError as exc:
            print(f"    {exc}\n", file=sys.stderr)
            continue

        username = _prompt("Username", username)
        password = _prompt("Password", password, secret=True)

        if not username or not password:
            print("    Username and password are both required.\n", file=sys.stderr)
            continue

        print("\n  Testing connection...")
        try:
            server = _test_connection(url, username, password, timeout)
        except NavidromeAuthError as exc:
            print(f"  ✗ {exc}", file=sys.stderr)
        except NavidromeError as exc:
            print(f"  ✗ {exc}", file=sys.stderr)
            if exc.hint:
                print(f"    {exc.hint}", file=sys.stderr)
        except DropWatchError as exc:  # pragma: no cover - defensive
            print(f"  ✗ {exc}", file=sys.stderr)
        else:
            print(f"  ✓ Connected to {server}\n")
            _save(target, url, username, password)
            print(f"Saved to {target}\n")
            program = invocation_name()
            print("You're ready:")
            print(f"  {program} scan               list missing releases")
            print(f"  {program} scan --since 2024  only recent ones")
            print(f"  {program} config             review these settings")
            return ExitCode.OK

        if attempt < MAX_ATTEMPTS:
            print()
            if not _confirm("  Try again?"):
                break
        print()

    # Every attempt failed. Offer to keep the values anyway, since the server
    # being down is a different problem from the settings being wrong.
    print(file=sys.stderr)
    if url and username and password and _confirm(
        "Could not connect. Save these settings anyway?", default=False
    ):
        _save(target, url, username, password)
        print(f"\nSaved to {target}", file=sys.stderr)
        print(
            f"Run `{invocation_name()} check` once the server is reachable.",
            file=sys.stderr,
        )
        return ExitCode.OK

    print("Nothing was saved.", file=sys.stderr)
    return ExitCode.CONFIG


def _save(target: Path, url: str, username: str, password: str) -> None:
    """Write the three values the wizard collects, keeping everything else.

    Starting from the file rather than from resolved settings matters: a
    setting currently shadowed by an environment variable reads as coming from
    the environment, so rebuilding the file from resolved values silently
    dropped whatever the file said. Re-running setup should never lose a
    setting you are not being asked about.
    """
    values = load_dotenv(target)
    values.update(
        {
            SETTINGS_BY_KEY["url"].env_var: url,
            SETTINGS_BY_KEY["username"].env_var: username,
            SETTINGS_BY_KEY["password"].env_var: password,
        }
    )
    save_settings(target, values)
