import os
import sys

from lazyzephyr.launcher import isolate_sys_path


def test_drops_pythonpath_entries(monkeypatch, tmp_path):
    foreign = tmp_path / "other-site-packages"
    foreign.mkdir()
    monkeypatch.setenv("PYTHONPATH", f"{foreign}{os.pathsep}")
    monkeypatch.delenv("LAZYZEPHYR_KEEP_PYTHONPATH", raising=False)
    monkeypatch.setattr(sys, "path", [str(foreign), "/keep/me", str(foreign) + "/"])
    isolate_sys_path()
    assert sys.path == ["/keep/me"]
    assert os.environ["PYTHONPATH"].startswith(str(foreign))  # children still get it


def test_opt_out(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("LAZYZEPHYR_KEEP_PYTHONPATH", "1")
    monkeypatch.setattr(sys, "path", [str(tmp_path)])
    isolate_sys_path()
    assert sys.path == [str(tmp_path)]
