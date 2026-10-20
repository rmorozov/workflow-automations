"""Options files shared by the ADO command lines."""

import pytest

from workflow_automations.ado_common import options


def _parser():
    root = options.new_parser(prog="tool")
    root.add_argument("--name", default="a", help="name (default: %(default)s)")
    root.add_argument("--tag", action="append", default=[], help="repeatable")
    root.add_argument("--overwrite", action="store_true")
    options.add_options_file(root)
    return root


def test_command_line_replaces_a_list_from_the_file(tmp_path):
    path = tmp_path / "o.ini"
    path.write_text("tag = [x, y]\nname = b\n", encoding="utf-8")
    args, _ = options.parse(_parser(), ["--options", str(path)])
    assert (args.name, args.tag) == ("b", ["x", "y"])
    args, _ = options.parse(_parser(), ["--options", str(path), "--tag", "z"])
    assert (args.name, args.tag) == ("b", ["z"])


def test_options_text_comments_unset_options_and_fills_defaults():
    root = _parser()
    args, _ = options.parse(root, [])
    text = options.options_text(root, args, "tool options")
    assert text.startswith("# tool options\n")
    assert "# name (default: a)\nname = a\n" in text
    assert "# tag =\n" in text
    assert "overwrite" not in text and "write-options" not in text


def test_unknown_file_key_names_the_file(tmp_path, capsys):
    path = tmp_path / "o.ini"
    path.write_text("nam = b\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        options.parse(_parser(), ["--options", str(path)])
    assert f"--options {path}: unknown option nam" in capsys.readouterr().err
