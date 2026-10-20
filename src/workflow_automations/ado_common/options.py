"""Options files: command-line options kept in a file, via ConfigArgParse.

A subcommand built with `add_options_file` reads `--options FILE`, a plain
``key = value`` file whose keys are the subcommand's long options without dashes
(``skip-types = Task, Bug``; lists as ``meta-field = [A=B, C]``; switches as
``true``/``false``). Options given on the command line win over the file, and the
file wins over the defaults. Environment variables are never read.

`--write-options FILE` writes every option of the subcommand with its effective
value (defaults, then the options file, then the command line) and runs nothing,
so a file can start from the defaults or record one run. Options without a value
are written commented out, so every key the subcommand takes is in the file.

`--overwrite` is never read from a file: replacing outputs stays a command-line
choice. Required options are checked by `require` after parsing, so
`--write-options` works without them.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from io import StringIO
from pathlib import Path

import configargparse

OPTIONS_DEST = "options"
WRITE_DEST = "write_options"
# Not read from an options file, and not written to one.
COMMAND_LINE_ONLY = frozenset({"overwrite", OPTIONS_DEST, WRITE_DEST, "write_config", "help"})
_FILE_SOURCE = "config_file|"


def new_parser(**kwargs) -> configargparse.ArgumentParser:
    """An argparse-compatible parser whose subcommands can read options files."""
    return configargparse.ArgumentParser(
        config_file_parser_class=configargparse.DefaultConfigFileParser,
        ignore_unknown_config_file_keys=False,
        **kwargs,
    )


def add_options_file(parser: argparse.ArgumentParser) -> None:
    """Add `--options FILE` and `--write-options FILE` to one (sub)command."""
    parser.add_argument(
        "--options",
        is_config_file=True,
        metavar="FILE",
        help=(
            "Read options from FILE (key = value, keys are the long options without "
            "dashes); options on the command line win.  --overwrite is never read "
            "from FILE."
        ),
    )
    parser.add_argument(
        "--write-options",
        metavar="FILE",
        help=(
            "Write every option of this command with its effective value to FILE "
            "and exit without running (needs --overwrite to replace FILE)."
        ),
    )


def _long_key(action: argparse.Action) -> str | None:
    longs = [s for s in action.option_strings if s.startswith("--")]
    return longs[0][2:] if longs else None


def file_settings(parser: argparse.ArgumentParser) -> dict[str, list[str]]:
    """Options file -> the keys it set in the last parse of ``parser``."""
    sources = getattr(parser, "_source_to_settings", None) or {}
    return {
        source[len(_FILE_SOURCE) :]: list(settings)
        for source, settings in sources.items()
        if source.startswith(_FILE_SOURCE)
    }


def check_options_file(parser: argparse.ArgumentParser, error: argparse.ArgumentParser) -> None:
    """Reject options-file keys that are unknown or that must be given on the command line."""
    sources = getattr(parser, "_source_to_settings", None) or {}
    known = {key: a for a in parser._actions if (key := _long_key(a))}
    for source, settings in sources.items():
        if not source.startswith(_FILE_SOURCE):
            continue
        name = source[len(_FILE_SOURCE) :]
        for key, (action, _value) in settings.items():
            action = action or known.get(key.lstrip("-"))
            if action is None:
                error.error(f"--options {name}: unknown option {key}")
            if action.dest in COMMAND_LINE_ONLY:
                error.error(f"--options {name}: {key} must be given on the command line")


def parse(
    root: argparse.ArgumentParser, argv: Sequence[str] | None
) -> tuple[argparse.Namespace, argparse.ArgumentParser]:
    """Parse ``argv`` and check the options file; return the namespace and its subcommand.

    An unknown key in an options file is reported with the file's name rather than as
    an unrecognized argument.
    """
    args, extras = root.parse_known_args(argv)
    sub = _subcommand(root, args)
    check_options_file(sub, root)
    if extras:
        root.error(f"unrecognized arguments: {' '.join(extras)}")
    return args, sub


def _subcommand(root: argparse.ArgumentParser, args: argparse.Namespace):
    for action in root._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices[getattr(args, action.dest)]
    return root


def _read_back(key: str, text: str) -> object:
    line = StringIO(f"{key} = {text}\n")
    return configargparse.DefaultConfigFileParser().parse(line).get(key)


def _format(key: str, value: object) -> str:
    """``value`` as the file's parser reads it back: plain when that is exact, else quoted.

    Lists are JSON, so commas inside an element stay in it. The parser strips one pair
    of surrounding quotes and treats `` #`` or `` ;`` as a comment, so a value that
    reads back differently is quoted; one that no quoting preserves is refused.
    """
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, list | tuple):
        expected: object = [str(v) for v in value]
        plain = json.dumps(expected, ensure_ascii=False)
    else:
        expected = plain = str(value)
    for text in (plain, f'"{plain}"', f"'{plain}'"):
        if _read_back(key, text) == expected:
            return text
    raise ValueError(f"{key} value {plain} cannot be written to an options file")


def options_text(parser: argparse.ArgumentParser, args: argparse.Namespace, title: str) -> str:
    """Every option of ``parser`` as ``key = value`` lines, unset ones commented out."""
    lines = [
        f"# {title}",
        "# Keys are the long options without dashes; the command line wins over this file.",
        "",
    ]
    for action in parser._actions:
        key = _long_key(action)
        if key is None or action.dest in COMMAND_LINE_ONLY:
            continue
        if action.help and action.help is not argparse.SUPPRESS:
            text = parser._get_formatter()._expand_help(action)  # fills %(default)s
            lines.append(f"# {' '.join(text.split())}")
        value = getattr(args, action.dest, None)
        if value is None or value == []:
            lines.append(f"# {key} =")
        else:
            lines.append(f"{key} = {_format(key, value)}")
    return "\n".join(lines) + "\n"


def write_options(
    parser: argparse.ArgumentParser,
    error: argparse.ArgumentParser,
    args: argparse.Namespace,
    title: str,
) -> Path | None:
    """Handle `--write-options`: write the file and return its path, or None when not asked."""
    target = getattr(args, WRITE_DEST, None)
    if not target:
        return None
    path = Path(target)
    if path.is_dir():
        error.error(f"--write-options {path} is a directory")
    if path.exists() and not getattr(args, "overwrite", False):
        error.error(f"{path} exists; pass --overwrite to replace it")
    try:
        text = options_text(parser, args, title)
    except ValueError as exc:
        error.error(f"--write-options {path}: {exc}")
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        error.error(f"--write-options {path}: cannot write ({exc})")
    return path


def require(
    error: argparse.ArgumentParser, args: argparse.Namespace, options: Sequence[str]
) -> None:
    """The argparse check for required options, run after `--write-options` had its turn."""
    missing = [opt for opt in options if not getattr(args, opt.lstrip("-").replace("-", "_"))]
    if missing:
        error.error(f"the following arguments are required: {', '.join(missing)}")
