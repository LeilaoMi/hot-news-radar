"""scripts/sync_reports_branch.py：原子更新（--force-with-lease）与并发合并。

用本地 bare 仓库模拟 GitHub：不联网、不需要 token。
"""
import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


def _load():
    spec = importlib.util.spec_from_file_location("sync_reports_branch", ROOT / "scripts" / "sync_reports_branch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(*args, cwd=None):
    env = dict(os.environ, **GIT_ENV)
    r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def write(root: Path, files: dict):
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "MIN_FILES", 1)
    bare = tmp_path / "remote.git"
    git("init", "--bare", "-q", str(bare))
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "-q", "-b", "reports", cwd=seed)
    write(seed, {"index.html": "shell", "reports/2026-10-17/08-00.html": "a"})
    git("add", "-A", cwd=seed)
    git("commit", "-q", "-m", "seed", cwd=seed)
    git("push", "-q", str(bare), "reports:refs/heads/reports", cwd=seed)
    return mod, bare, tmp_path


def clone(bare, dest):
    git("clone", "-q", "--depth", "1", "--branch", "reports", f"file://{bare}", str(dest))
    return git("rev-parse", "HEAD", cwd=dest)


def remote_files(bare, tmp_path, name):
    dest = tmp_path / name
    git("clone", "-q", "--branch", "reports", f"file://{bare}", str(dest))
    return sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file() and ".git" not in p.parts)


def make_docs(clone_dir: Path, docs: Path):
    """模拟 workflow：docs/ = master 静态壳 + 克隆下来的 reports/。"""
    shutil.copytree(clone_dir / "reports", docs / "reports")
    (docs / "index.html").write_text("shell", encoding="utf-8")


def test_simple_sync_is_orphan_and_applies_changes(env):
    mod, bare, tmp = env
    c = tmp / "c1"
    base = clone(bare, c)
    docs = tmp / "docs1"
    make_docs(c, docs)
    write(docs, {"reports/2026-10-17/12-00.html": "b"})
    rc = mod.main([f"--from-dir={docs}", f"--repo-dir={c}", f"--expect-oid={base}", f"--remote=file://{bare}"])
    assert rc == 0
    files = remote_files(bare, tmp, "check1")
    assert "reports/2026-10-17/12-00.html" in files
    tip = git("rev-parse", "refs/heads/reports", cwd=bare)
    assert git("rev-list", "--count", tip, cwd=bare) == "1"  # 孤儿提交，无历史


def test_concurrent_run_snapshot_is_not_deleted(env):
    """运行 A 克隆后，运行 B 先发布了新快照；A 同步时不能把 B 的快照删掉。"""
    mod, bare, tmp = env
    ca, cb = tmp / "ca", tmp / "cb"
    base_a = clone(bare, ca)
    base_b = clone(bare, cb)
    assert base_a == base_b

    docs_b = tmp / "docs_b"
    make_docs(cb, docs_b)
    write(docs_b, {"reports/2026-10-17/09-00.html": "from-b"})
    assert mod.main([f"--from-dir={docs_b}", f"--repo-dir={cb}", f"--expect-oid={base_b}",
                     f"--remote=file://{bare}"]) == 0

    docs_a = tmp / "docs_a"
    make_docs(ca, docs_a)
    write(docs_a, {"reports/2026-10-17/09-30.html": "from-a", "reports/archive.html": "a-index"})
    assert mod.main([f"--from-dir={docs_a}", f"--repo-dir={ca}", f"--expect-oid={base_a}",
                     f"--remote=file://{bare}"]) == 0

    files = remote_files(bare, tmp, "check2")
    assert "reports/2026-10-17/09-00.html" in files   # B 的快照保住了
    assert "reports/2026-10-17/09-30.html" in files   # A 的也在
    assert "reports/2026-10-17/08-00.html" in files


def test_stale_lease_without_merge_would_be_rejected(env):
    """直接验证 lease：拿一个过期的 expect 推送必须失败，而不是覆盖。"""
    mod, bare, tmp = env
    c = tmp / "c3"
    base = clone(bare, c)
    docs = tmp / "docs3"
    make_docs(c, docs)
    write(docs, {"reports/x.html": "x"})
    g = mod.Git(str(c), f"file://{bare}")
    tree = g.write_tree_from_dir(docs)
    commit = g.commit_orphan(tree, "t")
    # 远端先被别人更新
    other = tmp / "other"
    clone(bare, other)
    write(other, {"reports/y.html": "y"})
    git("add", "-A", cwd=other)
    git("commit", "-q", "-m", "other", cwd=other)
    git("push", "-q", "origin", "HEAD:refs/heads/reports", cwd=other)
    with pytest.raises(mod.LeaseRejected):
        g.push_with_lease(commit, base)


def test_refuses_when_too_few_files(env, monkeypatch):
    mod, bare, tmp = env
    monkeypatch.setattr(mod, "MIN_FILES", 1000)
    c = tmp / "c4"
    base = clone(bare, c)
    docs = tmp / "docs4"
    make_docs(c, docs)
    with pytest.raises(SystemExit):
        mod.main([f"--from-dir={docs}", f"--repo-dir={c}", f"--expect-oid={base}", f"--remote=file://{bare}"])


def test_no_change_is_noop(env):
    mod, bare, tmp = env
    c = tmp / "c5"
    base = clone(bare, c)
    docs = tmp / "docs5"
    make_docs(c, docs)
    before = git("rev-parse", "refs/heads/reports", cwd=bare)
    assert mod.main([f"--from-dir={docs}", f"--repo-dir={c}", f"--expect-oid={base}",
                     f"--remote=file://{bare}"]) == 0
    assert git("rev-parse", "refs/heads/reports", cwd=bare) == before
