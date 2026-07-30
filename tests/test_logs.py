"""Tests for `darkroom logs import` (F4 step 4)."""
from __future__ import annotations

import argparse

import pytest

from darkroom.logs import ARCHIVE_SUBDIR, _import_run, add_subparser, plan_import

_ASIAIR_ENV_VARS = ("DARKROOM_ASIAIR_LOGS", "DARKROOM_ASIAIR")


@pytest.fixture(autouse=True)
def _isolate_asiair_env(monkeypatch):
    """conftest's guard doesn't know about the log-source vars yet."""
    for var in _ASIAIR_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


#: Contents keyed by filename. Sizes differ so a same-size skip is meaningful.
SOURCE_FILES = {
    "Autorun_Log_2026-07-28_205127.txt": "autorun one\n",
    "Autorun_Log_2026-07-29_211302.txt": "autorun two, longer\n",
    "PHD2_GuideLog_2026-07-28_205130.txt": "guide one\n",
    "PHD2_GuideLog_2026-07-29_211305.txt": "guide two, longer\n",
    "PHD2_GuideLog_2026-07-28_205130_CHN.txt": "guide one, in Chinese\n",
    "Autorun_Log_2026-07-28_205127_CHN.txt": "autorun one, in Chinese\n",
    "notes.txt": "not a log\n",
    "IMG_0001.fit": "not a log either\n",
}

#: What a correct import lands in the archive.
EXPECTED_COPIED = {
    "Autorun_Log_2026-07-28_205127.txt",
    "Autorun_Log_2026-07-29_211302.txt",
    "PHD2_GuideLog_2026-07-28_205130.txt",
    "PHD2_GuideLog_2026-07-29_211305.txt",
}


def _make_source(tmp_path):
    source = tmp_path / "ASIAIR" / "log"
    source.mkdir(parents=True)
    for name, text in SOURCE_FILES.items():
        (source / name).write_text(text)
    return source


def _snapshot(directory):
    """name -> (size, contents) for every file in a directory."""
    return {
        p.name: (p.stat().st_size, p.read_text())
        for p in directory.iterdir()
        if p.is_file()
    }


def _args(source, archive, apply=False):
    return argparse.Namespace(source=str(source), archive=str(archive), apply=apply)


def test_plan_splits_copies_duplicates_and_chn(tmp_path):
    source = _make_source(tmp_path)
    dest = tmp_path / "nas" / ARCHIVE_SUBDIR
    dest.mkdir(parents=True)
    already = "PHD2_GuideLog_2026-07-28_205130.txt"
    (dest / already).write_text(SOURCE_FILES[already])

    plan = plan_import(source, dest)

    assert {p.name for p in plan.copy} == EXPECTED_COPIED - {already}
    assert [p.name for p in plan.duplicates] == [already]
    assert {p.name for p in plan.chn} == {
        "Autorun_Log_2026-07-28_205127_CHN.txt",
        "PHD2_GuideLog_2026-07-28_205130_CHN.txt",
    }


def test_plan_recopies_when_size_differs(tmp_path):
    source = _make_source(tmp_path)
    dest = tmp_path / "nas" / ARCHIVE_SUBDIR
    dest.mkdir(parents=True)
    truncated = "PHD2_GuideLog_2026-07-28_205130.txt"
    (dest / truncated).write_text("gui")  # e.g. an interrupted copy

    plan = plan_import(source, dest)

    assert truncated in {p.name for p in plan.copy}
    assert plan.duplicates == []


def test_dry_run_writes_nothing(tmp_path, capsys):
    source = _make_source(tmp_path)
    archive = tmp_path / "nas"
    archive.mkdir()
    before = _snapshot(source)

    _import_run(_args(source, archive))

    assert not (archive / ARCHIVE_SUBDIR).exists()
    assert list(archive.iterdir()) == []
    assert _snapshot(source) == before

    out = capsys.readouterr().out
    assert "4 would be copied" in out
    assert "2 _CHN translations skipped" in out
    assert "--apply" in out


def test_apply_copies_the_right_set_and_leaves_source_untouched(tmp_path, capsys):
    source = _make_source(tmp_path)
    archive = tmp_path / "nas"
    before = _snapshot(source)

    _import_run(_args(source, archive, apply=True))

    dest = archive / ARCHIVE_SUBDIR
    assert set(_snapshot(dest)) == EXPECTED_COPIED
    for name in EXPECTED_COPIED:
        assert (dest / name).read_text() == SOURCE_FILES[name]

    # Nothing in the source moved, changed or disappeared.
    assert _snapshot(source) == before

    out = capsys.readouterr().out
    assert "4 copied" in out
    assert "2 _CHN translations skipped" in out


def test_second_apply_is_a_no_op(tmp_path, capsys):
    source = _make_source(tmp_path)
    archive = tmp_path / "nas"
    _import_run(_args(source, archive, apply=True))
    dest = archive / ARCHIVE_SUBDIR
    after_first = _snapshot(dest)
    mtimes = {p.name: p.stat().st_mtime_ns for p in dest.iterdir()}
    capsys.readouterr()

    _import_run(_args(source, archive, apply=True))

    assert _snapshot(dest) == after_first
    assert {p.name: p.stat().st_mtime_ns for p in dest.iterdir()} == mtimes

    out = capsys.readouterr().out
    assert "0 copied" in out
    assert "4 already archived" in out


def test_source_from_env_and_asiair_root_fallback(tmp_path, monkeypatch, capsys):
    source = _make_source(tmp_path)
    archive = tmp_path / "nas"

    monkeypatch.setenv("DARKROOM_ASIAIR_LOGS", str(source))
    _import_run(argparse.Namespace(source=None, archive=str(archive), apply=False))
    assert "4 would be copied" in capsys.readouterr().out

    # No explicit log dir: fall back to <asiair root>/log.
    monkeypatch.delenv("DARKROOM_ASIAIR_LOGS")
    monkeypatch.setenv("DARKROOM_ASIAIR", str(source.parent))
    _import_run(argparse.Namespace(source=None, archive=str(archive), apply=False))
    assert "4 would be copied" in capsys.readouterr().out


def test_missing_source_and_archive_exit_with_a_hint(tmp_path, capsys):
    archive = tmp_path / "nas"

    with pytest.raises(SystemExit) as exc:
        _import_run(argparse.Namespace(source=None, archive=str(archive), apply=False))
    assert "asiair_logs_path" in str(exc.value)

    with pytest.raises(SystemExit) as exc:
        _import_run(argparse.Namespace(source=str(tmp_path / "nope"), archive=str(archive),
                                       apply=False))
    assert "not a directory" in str(exc.value)

    source = _make_source(tmp_path)
    with pytest.raises(SystemExit) as exc:
        _import_run(argparse.Namespace(source=str(source), archive=None, apply=False))
    assert "archive_path" in str(exc.value)


def test_argparse_registration_logs_import():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    add_subparser(sub)

    args = p.parse_args(["logs", "import", "--source", "/tmp/log", "--archive", "/tmp/nas",
                         "--apply"])
    assert args.func is _import_run
    assert args.source == "/tmp/log"
    assert args.archive == "/tmp/nas"
    assert args.apply is True

    defaults = p.parse_args(["logs", "import"])
    assert defaults.source is None
    assert defaults.archive is None
    assert defaults.apply is False


def test_registered_on_the_top_level_cli():
    """`darkroom logs import` is reachable from the real parser (cli.main)."""
    from darkroom import cli, logs

    parser = argparse.ArgumentParser(prog="darkroom")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for mod in (cli.catalog_cli, cli.ingest, cli.prep, cli.finish, cli.logs):
        mod.add_subparser(sub)

    assert cli.logs is logs
    args = parser.parse_args(["logs", "import", "--archive", "/tmp/nas"])
    assert args.func is _import_run
