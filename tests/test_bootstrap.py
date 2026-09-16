"""Phase 4 pieces that don't need typer/rich: venv plan, prereqs, repo setup,
daemon helpers, status rows."""
import json
import os
import sys

import pytest

from conftest import sh
from dags import daemon, panel, prereqs, repos, venv


# -- venv (Ch.5.2) ------------------------------------------------------------------

def _root(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "requirements.txt").write_text("pyyaml>=6.0\n")
    (tmp_path / "bin" / "requirements-dev.txt").write_text("-r requirements.txt\npytest>=8\n")
    return tmp_path


def _fake_venv(root, stamp):
    py = venv.venv_python(root)
    py.parent.mkdir(parents=True)
    py.write_text("")
    (venv.venv_dir(root) / venv.STAMP).write_text(json.dumps(stamp))


def test_venv_missing_means_rebuild(tmp_path):
    assert venv.plan_action(_root(tmp_path), dev=False) == "rebuild"


def test_venv_up_to_date(tmp_path):
    root = _root(tmp_path)
    _fake_venv(root, venv.wanted_stamp(root, False))
    assert venv.plan_action(root, dev=False) == "ok"


def test_venv_requirements_changed_means_install(tmp_path):
    root = _root(tmp_path)
    _fake_venv(root, venv.wanted_stamp(root, False))
    (root / "bin" / "requirements.txt").write_text("pyyaml>=6.0\nrich>=13\n")
    assert venv.plan_action(root, dev=False) == "install"


def test_dev_request_on_non_dev_venv_installs(tmp_path):
    root = _root(tmp_path)
    stamp = venv.wanted_stamp(root, True)
    stamp["dev"] = False
    _fake_venv(root, stamp)
    assert venv.plan_action(root, dev=True) == "install"


def test_dev_venv_serves_normal_runs(tmp_path):
    root = _root(tmp_path)
    _fake_venv(root, venv.wanted_stamp(root, True))
    assert venv.plan_action(root, dev=False) == "ok"
    (root / "bin" / "requirements.txt").write_text("pyyaml>=6.0\nrich>=13\n")
    assert venv.plan_action(root, dev=False) == "install"


def test_foreign_venv_is_rebuilt(tmp_path):
    """e.g. a venv created by a Linux VM that mounts this Mac folder."""
    root = _root(tmp_path)
    stamp = venv.wanted_stamp(root, False)
    stamp["platform"] = "linux" if sys.platform == "darwin" else "darwin"
    _fake_venv(root, stamp)
    assert venv.plan_action(root, dev=False) == "rebuild"


def test_old_python_is_refused():
    with pytest.raises(venv.VenvError):
        venv.check_python((3, 9, 0))
    venv.check_python((3, 10, 0))


def test_ensure_is_a_noop_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DAGS_NO_VENV", "1")
    assert venv.ensure(tmp_path, ["swarm.py"], tmp_path / "swarm.py") is None


# -- prereqs (Ch.5.3 step 1) ----------------------------------------------------------

def test_prereqs_report_missing_tools(fake_gh):
    checks = prereqs.run(which=lambda name: None, platform="linux")
    names = {c.name: c for c in checks}
    assert not names["git"].ok and not names["gh"].ok
    assert "gh auth" not in names
    assert not names["macOS"].ok and not names["macOS"].fatal
    assert {c.name for c in prereqs.failures(checks)} == {"git", "gh"}


def test_prereqs_with_gh(fake_gh):
    fake_gh.handler = lambda a, e, i: {
        ("auth", "status"): "Logged in",
        ("issue", "edit"): "--add-blocked-by --parent",
        ("issue", "create"): "--blocked-by --parent",
        ("--version",): "gh version 2.80.0 (2026-01-01)",
    }.get(tuple(a[:2]) if a[0] != "--version" else ("--version",), "")
    checks = prereqs.run(which=lambda name: f"/usr/bin/{name}", platform="darwin")
    assert all(c.ok for c in checks), [(c.name, c.detail) for c in checks if not c.ok]
    assert any("2.80.0" in c.detail for c in checks)


# -- code repos (Ch.5.3 step 2) ----------------------------------------------------------

def test_configure_is_idempotent(swarm, tmp_path):
    ctx = swarm.clone("mac-a")
    code = tmp_path / "code"
    sh(["git", "init", "-q", "-b", "main", str(code)], tmp_path)
    for _ in range(3):
        repos.configure(ctx, code)
    tmpl = sh(["git", "config", "--get", "commit.template"], code).strip()
    assert tmpl == str((ctx.root / "templates" / "commit-message.txt").resolve())
    excl = (code / ".git" / "info" / "exclude").read_text()
    assert excl.count(".swarm-task/") == 1


def test_mapped_repo_must_exist(swarm, tmp_path):
    ctx = swarm.clone("mac-a")
    (ctx.swarm_dir / "local.yaml").write_text(
        f"human: roman\nrepos:\n  OWNER/app: {tmp_path / 'nope'}\n")
    with pytest.raises(repos.RepoError):
        repos.ensure(ctx, "OWNER/app")


def test_mapped_repo_is_used_in_place(swarm, tmp_path):
    ctx = swarm.clone("mac-a")
    code = tmp_path / "existing-checkout"
    sh(["git", "init", "-q", "-b", "main", str(code)], tmp_path)
    (ctx.swarm_dir / "local.yaml").write_text(f"human: roman\nrepos:\n  OWNER/app: {code}\n")
    assert repos.ensure(ctx, "OWNER/app", fetch=False) == code.resolve()


def test_unmapped_repo_is_cloned_with_gh(swarm, fake_gh):
    ctx = swarm.clone("mac-a")
    target = ctx.swarm_dir / "repos" / "OWNER" / "app"

    def handler(args, env, input):
        if args[:2] == ["repo", "clone"]:
            sh(["git", "init", "-q", "-b", "main", args[3]], ctx.root)
        return ""
    fake_gh.handler = handler
    assert repos.ensure(ctx, "OWNER/app") == target
    assert fake_gh.calls[0]["args"] == ["repo", "clone", "OWNER/app", str(target)]


# -- daemon helpers (plan §2.2) ------------------------------------------------------------

def test_parse_interval():
    assert daemon.parse_interval("60s") == 60
    assert daemon.parse_interval("5m") == 300
    assert daemon.parse_interval("1h") == 3600
    assert daemon.parse_interval("45") == 45
    assert daemon.parse_interval(7) == 7


def test_pidfile(swarm):
    ctx = swarm.clone("mac-a")
    assert daemon.running_pid(ctx) is None
    daemon.pid_path(ctx).write_text(f"{os.getpid()}\n")
    assert daemon.running_pid(ctx) == os.getpid()
    daemon.pid_path(ctx).write_text("999999999\n")
    assert daemon.running_pid(ctx) is None
    with pytest.raises(RuntimeError):
        daemon.pid_path(ctx).write_text(f"{os.getpid()}\n")
        daemon.spawn(ctx, daemon.Options(), ctx.bin_dir / "swarm.py")


def test_daemon_threads_run_and_stop(swarm, tmp_path, monkeypatch):
    from backends.fake import FakeBackend
    ctx = swarm.clone("mac-a")
    backend = FakeBackend(tmp_path / "b.yaml")
    backend.add("T1", title="x", labels=["repo:OWNER/app"])
    ctx.set_backend(backend)
    seen = []
    d = daemon.Daemon(ctx, daemon.Options(quota_share=0, poll_interval=0.05, cycle_interval=0.05,
                                                 heartbeat_interval=0.05),
                      notify=lambda text, kind="info": seen.append(kind))
    d.poller.use_gh = False
    d.start_threads()
    import time
    deadline = time.time() + 20
    while time.time() < deadline and not all(lp.cycles >= 2 for lp in d.loops):
        time.sleep(0.05)
    d.stop.set()
    for lp in d.loops:
        lp.join(10)
    assert all(not lp.is_alive() for lp in d.loops)
    assert all(lp.last_error is None for lp in d.loops), [lp.last_error for lp in d.loops]
    import resolve
    assert "T1" in resolve.index(ctx.root)              # the scheduler ran plan sync
    d.write_info()
    assert daemon.info(ctx)["identity"] == "mac-a"


# -- status rows (Ch.5.4) --------------------------------------------------------------------

def test_status_rows_without_daemon(swarm):
    ctx = swarm.clone("mac-a")
    rows = {label: (value, style) for label, value, style in panel.status_rows(ctx, identity_new=True, share=2)}
    assert rows["identity"][0].startswith("mac-a (new)")
    assert "0 tasks ready" in rows["coordination"][0]
    assert rows["scheduler"][1] == "off" and "quota-share 2" in rows["scheduler"][0]
    assert rows["quota"][0].startswith("0/3")


def test_identity_override_is_remembered(tmp_path):
    """K3: `--identity` names a fresh clone for good; later commands need no flag."""
    from dags.config import Context
    (tmp_path / "backend.yaml").write_text("backend: fake\n")
    c = Context(tmp_path, identity="Mac A", persist_identity=True)
    assert c.identity == "Mac-A" and c.identity_mismatch is None
    assert Context(tmp_path).identity == "Mac-A"
    other = Context(tmp_path, identity="mac-b", persist_identity=True)
    assert other.identity == "mac-b" and other.identity_mismatch == "Mac-A"
    assert Context(tmp_path).identity == "Mac-A"          # a one-off override doesn't rename
    assert Context(tmp_path, identity="x").stored_identity() == "Mac-A"
    Context(tmp_path).set_identity("mac-c")
    assert Context(tmp_path).identity == "mac-c"
