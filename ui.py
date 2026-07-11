"""Tiny colored-terminal helpers (colorama), with graceful no-color fallback."""
from __future__ import annotations

from getpass import getpass

try:
    import colorama
    from colorama import Fore, Style

    colorama.init(autoreset=True)
    _ON = True
except Exception:  # colorama missing -> plain text
    _ON = False

    class _Blank:
        def __getattr__(self, _):
            return ""

    Fore = Style = _Blank()  # type: ignore


def _w(code: str, s: str) -> str:
    return f"{code}{s}{Style.RESET_ALL}" if _ON else s


def bold(s: str) -> str:
    return _w(Style.BRIGHT, s)


def dim(s: str) -> str:
    return _w(Style.DIM, s)


def cyan(s: str) -> str:
    return _w(Fore.CYAN, s)


def green(s: str) -> str:
    return _w(Fore.GREEN, s)


def yellow(s: str) -> str:
    return _w(Fore.YELLOW, s)


def red(s: str) -> str:
    return _w(Fore.RED, s)


def magenta(s: str) -> str:
    return _w(Fore.MAGENTA, s)


def banner(text: str) -> None:
    line = "=" * 60
    print(cyan(line))
    print(bold(cyan("  " + text)))
    print(cyan(line))


def rule(text: str) -> None:
    print(dim("-" * 60))
    print(bold(text))
    print(dim("-" * 60))


def info(text: str) -> None:
    print(cyan("> ") + text)


def success(text: str) -> None:
    print(green("[ok] ") + text)


def warn(text: str) -> None:
    print(yellow("[!] ") + text)


def error(text: str) -> None:
    print(red("[x] ") + text)


def ask(prompt: str, default: str | None = None, secret: bool = False) -> str:
    label = cyan("> ") + prompt
    if default:
        label += dim(f" [{default}]")
    label += ": "
    while True:
        value = (getpass(label) if secret else input(label)).strip()
        if not value and default is not None:
            return default
        if value:
            return value
        warn("required")
