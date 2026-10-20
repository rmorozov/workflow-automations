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


@pytest.mark.parametrize(
    ("name", "tags"),
    [
        ("/tmp/reports #1.xlsx", ["Team, owner=Custom.Team"]),
        ("a ; b", ['quote " inside', "it's", "  padded  "]),
        ('"quoted"', ["[x]", "#start", "x # y"]),
        ("  lead and trail  ", ["a,b", "c"]),
        ("C:\\work\\out.xlsx", ["back\\slash"]),
    ],
)
def test_written_values_read_back_unchanged(tmp_path, name, tags):
    root = _parser()
    argv = ["--name", name] + [arg for tag in tags for arg in ("--tag", tag)]
    args, _ = options.parse(root, argv)
    path = tmp_path / "o.ini"
    path.write_text(options.options_text(root, args, "tool options"), encoding="utf-8")
    again, _ = options.parse(_parser(), ["--options", str(path)])
    assert (again.name, again.tag) == (name, tags)


def test_value_no_quoting_preserves_is_refused(tmp_path, capsys):
    path = tmp_path / "o.ini"
    with pytest.raises(SystemExit):
        options.write_options(*_parse_and_parser(["--name", "[a]", "--write-options", str(path)]))
    assert "name value [a] cannot be written" in capsys.readouterr().err
    assert not path.exists()


def _parse_and_parser(argv):
    root = _parser()
    args, _ = options.parse(root, argv)
    return root, root, args, "tool options"
