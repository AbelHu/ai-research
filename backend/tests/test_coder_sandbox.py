"""Tests for the Coder sandbox: isolated import + lint + test validation (P1).

Execution tests use the always-available ``RlimitSandbox`` (no ``bwrap``
required); the bubblewrap argv construction is tested as a pure function, and
backend selection is tested with a patched ``shutil.which``.
"""

from __future__ import annotations

from app.coder import sandbox as sandbox_mod
from app.coder.sandbox import (
    BubblewrapSandbox,
    RlimitSandbox,
    detect_sandbox,
    run_checks,
)

# A clean, importable, ruff-clean (E9,F) generated skill module.
_VALID = '''from pydantic import BaseModel
from app.skills.registry import skill


class P(BaseModel):
    x: int


class R(BaseModel):
    y: int


@skill(
    name="generated.sbx_double",
    description="double x",
    params=P,
    returns=R,
    permissions=[],
    effect="read",
)
def run(params, ctx):
    return R(y=params.x * 2)
'''


def _bundle(tmp_path, **files: str):
    for name, body in files.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    return tmp_path


def test_valid_bundle_passes(tmp_path) -> None:
    _bundle(tmp_path, **{"double.py": _VALID})
    report = run_checks(tmp_path, sandbox=RlimitSandbox(), timeout=30)
    assert report.ok, report.summary
    assert report.by_name("import").ok
    assert report.by_name("lint").ok
    assert report.by_name("tests").skipped  # no test files in the bundle


def test_import_error_fails(tmp_path) -> None:
    _bundle(tmp_path, **{"bad.py": "import definitely_not_a_real_module_zzz\n\nVALUE = 1\n"})
    report = run_checks(tmp_path, sandbox=RlimitSandbox(), timeout=30)
    assert not report.ok
    imp = report.by_name("import")
    assert imp is not None and not imp.ok
    assert "FAIL" in imp.output or "ModuleNotFoundError" in imp.output


def test_lint_error_but_imports(tmp_path) -> None:
    # `import os` is unused (F401): imports fine, but ruff flags it.
    _bundle(tmp_path, **{"lint_me.py": "import os\n\nVALUE = 1\n"})
    report = run_checks(tmp_path, sandbox=RlimitSandbox(), timeout=30)
    assert report.by_name("import").ok  # it imports cleanly
    assert not report.by_name("lint").ok  # but lint fails
    assert not report.ok


def test_failing_test_fails(tmp_path) -> None:
    _bundle(
        tmp_path,
        **{
            "m.py": "def add(a, b):\n    return a + b\n",
            "test_m.py": "from m import add\n\n\ndef test_add():\n    assert add(1, 1) == 3\n",
        },
    )
    report = run_checks(tmp_path, sandbox=RlimitSandbox(), timeout=60)
    tests = report.by_name("tests")
    assert tests is not None and not tests.skipped and not tests.ok
    assert not report.ok


def test_passing_test_passes(tmp_path) -> None:
    _bundle(
        tmp_path,
        **{
            "m.py": "def add(a, b):\n    return a + b\n",
            "test_m.py": "from m import add\n\n\ndef test_add():\n    assert add(1, 1) == 2\n",
        },
    )
    report = run_checks(tmp_path, sandbox=RlimitSandbox(), timeout=60)
    assert report.ok, report.summary
    assert report.by_name("tests").ok


def test_timeout_is_reported(tmp_path) -> None:
    _bundle(tmp_path, **{"slow.py": "import time\n\ntime.sleep(5)\n"})
    report = run_checks(tmp_path, sandbox=RlimitSandbox(), timeout=1, run_tests=False)
    imp = report.by_name("import")
    assert imp is not None and imp.timed_out and not imp.ok
    assert not report.ok


# --- backend construction / selection (no bwrap required) -------------------


def test_bubblewrap_wrap_builds_no_network_argv(tmp_path) -> None:
    sb = BubblewrapSandbox(bwrap_path="bwrap", backend_root=tmp_path)
    argv = sb.wrap(["python", "-c", "print(1)"], cwd=tmp_path, env={"PATH": "/usr/bin"})

    assert argv[0] == "bwrap"
    assert "--unshare-all" in argv  # no network + new namespaces
    assert "--die-with-parent" in argv
    assert "--clearenv" in argv
    assert "--setenv" in argv  # env forwarded explicitly
    assert "--chdir" in argv and str(tmp_path) in argv
    # The real command follows the `--` separator unchanged.
    sep = argv.index("--")
    assert argv[sep + 1 :] == ["python", "-c", "print(1)"]


def test_detect_sandbox_falls_back_to_rlimit(monkeypatch) -> None:
    monkeypatch.setattr(sandbox_mod.shutil, "which", lambda _name: None)
    assert isinstance(detect_sandbox(), RlimitSandbox)


def test_detect_sandbox_uses_bwrap_when_present(monkeypatch) -> None:
    monkeypatch.setattr(sandbox_mod.shutil, "which", lambda _name: "/usr/bin/bwrap")
    sb = detect_sandbox()
    assert isinstance(sb, BubblewrapSandbox)
    assert sb.bwrap_path == "/usr/bin/bwrap"
