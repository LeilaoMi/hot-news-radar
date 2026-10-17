#!/usr/bin/env python3
# coding=utf-8
"""把 Pages 内容（本地 docs/ 目录）同步到 reports 分支（GitHub Pages 的发布源）。

工作方式
--------
CI 在运行开头已经把 reports 分支浅克隆到 ``--repo-dir``（默认 /tmp/pages-content），
本脚本借用这个克隆的对象库：

1. 用临时 index 把 ``--from-dir``（默认 docs）整个目录写成一棵 git tree；
   未变化的文件 blob 已在对象库里，push 时只会上传真正变化的对象。
2. 以这棵 tree 创建**孤儿提交**（无 parent，reports 分支不需要历史，旧对象交给 gc）。
3. 用 ``git push --force-with-lease=refs/heads/reports:<克隆时的 OID>`` 原子地更新 ref：
   只有远端仍指向我们开始时看到的那个提交才会成功（服务端比较并交换，CAS）。

为什么必须 CAS（审计 3.1 a/b/c）
---------------------------------
旧实现走 REST API：删除列表按「同步时刻的远端」计算、最后 ``PATCH force:true``。
如果在本次运行克隆之后、同步之前有别的运行发布过，它新增的快照会被当成
「retention 清理」删掉，而且 ref 被无条件覆盖。

冲突时的处理
------------
lease 失败说明远端被别的运行更新了。脚本会抓取新的远端提交，做一次三方合并：
对方改动过、而本次没有改动的路径（相对克隆时的基线）一律采用对方版本
（新增 / 修改 / 删除都照搬），双方都改的路径以本次为准（归档页等索引下一轮会重算）。
然后以新提交为基线重试，最多 ``SYNC_MAX_ATTEMPTS`` 次（默认 3）。

安全兜底
--------
- 文件数低于 MIN_FILES（默认 500，可用 SYNC_MIN_FILES 覆盖）拒绝提交，
  防止上游内容缺失时把线上替换成残缺版本。
- 推送后用 ``git ls-remote`` 回读校验 ref 确实指向本次提交。

用法
----
    GITHUB_TOKEN=xxx python3 scripts/sync_reports_branch.py --from-dir=docs \\
        --repo-dir=/tmp/pages-content [--expect-oid=<sha>] [--remote=<url>] [--dry-run]

环境变量：
    GITHUB_TOKEN       推送用 token（contents: write）；只通过一次性的 http.extraheader 传给 git，
                       不写入任何 git 配置文件
    GITHUB_REPOSITORY  形如 owner/repo，CI 里自动注入
    REPORTS_BRANCH     目标分支，默认 reports
"""

import base64
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = os.environ.get("GITHUB_REPOSITORY", "LeilaoMi/hot-news-radar")
REPORTS_BRANCH = os.environ.get("REPORTS_BRANCH", "reports")
MIN_FILES = int(os.environ.get("SYNC_MIN_FILES", "500"))
MAX_ATTEMPTS = int(os.environ.get("SYNC_MAX_ATTEMPTS", "3"))


class LeaseRejected(Exception):
    """远端 ref 已不是预期的基线提交。"""


def parse_args(argv):
    opts = {
        "from_dir": "docs",
        "repo_dir": "/tmp/pages-content",
        "expect_oid": "",
        "remote": "",
        "dry_run": False,
    }
    for a in argv:
        if a == "--dry-run":
            opts["dry_run"] = True
        elif a.startswith("--from-dir="):
            opts["from_dir"] = a.split("=", 1)[1]
        elif a.startswith("--repo-dir="):
            opts["repo_dir"] = a.split("=", 1)[1]
        elif a.startswith("--expect-oid="):
            opts["expect_oid"] = a.split("=", 1)[1]
        elif a.startswith("--remote="):
            opts["remote"] = a.split("=", 1)[1]
        elif a.startswith("--from-ref="):
            sys.exit("错误：--from-ref 模式已移除（master 不再存放 docs/reports），请用 --from-dir=docs")
        else:
            sys.exit(f"错误：未知参数 {a}")
    return opts


class Git:
    def __init__(self, repo_dir, remote, token=""):
        self.git_dir = str(Path(repo_dir) / ".git")
        if not Path(self.git_dir).is_dir():
            sys.exit(f"错误：{repo_dir} 不是 git 克隆（缺少 .git）")
        self.remote = remote
        self.auth = []
        if token and remote.startswith("https://"):
            cred = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            # 只作用于本次命令，不落盘
            self.auth = ["-c", f"http.extraheader=AUTHORIZATION: basic {cred}"]

    def run(self, *args, env=None, check=True, net=False, input_bytes=None, cwd=None):
        cmd = ["git"] + (self.auth if net else []) + ["--git-dir", self.git_dir] + list(args)
        full_env = dict(os.environ)
        full_env.setdefault("GIT_AUTHOR_NAME", "github-actions[bot]")
        full_env.setdefault("GIT_AUTHOR_EMAIL", "41898282+github-actions[bot]@users.noreply.github.com")
        full_env.setdefault("GIT_COMMITTER_NAME", full_env["GIT_AUTHOR_NAME"])
        full_env.setdefault("GIT_COMMITTER_EMAIL", full_env["GIT_AUTHOR_EMAIL"])
        if env:
            full_env.update(env)
        r = subprocess.run(cmd, env=full_env, capture_output=True, input=input_bytes, cwd=cwd)
        if check and r.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args[:3])} 失败（{r.returncode}）：{r.stderr.decode(errors='ignore').strip()[:800]}"
            )
        return r

    def out(self, *args, **kw):
        return self.run(*args, **kw).stdout.decode().strip()

    # ── 远端 ───────────────────────────────────────────────────────────
    def remote_oid(self):
        line = self.out("ls-remote", self.remote, f"refs/heads/{REPORTS_BRANCH}", net=True)
        return line.split()[0] if line else ""

    def fetch_tip(self):
        self.run("fetch", "--quiet", "--depth", "1", self.remote,
                 f"refs/heads/{REPORTS_BRANCH}", net=True)
        return self.out("rev-parse", "FETCH_HEAD")

    def push_with_lease(self, commit, expect):
        lease = f"--force-with-lease=refs/heads/{REPORTS_BRANCH}:{expect}"
        r = self.run("push", "--porcelain", lease, self.remote,
                     f"{commit}:refs/heads/{REPORTS_BRANCH}", check=False, net=True)
        if r.returncode == 0:
            return
        msg = (r.stdout + r.stderr).decode(errors="ignore")
        # lease 失败（stale info / rejected）与网络等其它错误要区分开：
        # 回读远端，确实已不是 expect 才算冲突，可以合并后重试。
        current = self.remote_oid()
        if current and current != expect:
            raise LeaseRejected(current)
        raise RuntimeError(f"git push 失败：{msg.strip()[:800]}")

    # ── 本地对象 ───────────────────────────────────────────────────────
    def tree_of(self, commit):
        return self.out("rev-parse", f"{commit}^{{tree}}")

    def ls_tree(self, treeish):
        """返回 {path: (mode, sha)}，只含 blob。"""
        raw = self.run("ls-tree", "-r", "-z", treeish).stdout.decode("utf-8", errors="surrogateescape")
        res = {}
        for rec in raw.split("\0"):
            if not rec:
                continue
            meta, path = rec.split("\t", 1)
            mode, typ, sha = meta.split()
            if typ == "blob":
                res[path] = (mode, sha)
        return res

    def write_tree_from_dir(self, work_tree):
        with tempfile.TemporaryDirectory() as td:
            env = {"GIT_INDEX_FILE": str(Path(td) / "index"), "GIT_WORK_TREE": str(work_tree)}
            # -f：忽略 .gitignore；-A：包括删除。--git-dir 已显式指定，work-tree 走环境变量
            self.run("add", "-A", "-f", ".", env=env, cwd=str(work_tree))
            return self.out("write-tree", env=env)

    def commit_orphan(self, tree, message):
        return self.out("commit-tree", tree, "-m", message)

    def blob_bytes(self, sha):
        return self.run("cat-file", "blob", sha).stdout


def count_files(root: Path) -> int:
    return sum(1 for p in root.rglob("*") if p.is_file())


def merge_theirs_into_dir(git, work_tree: Path, base_commit, theirs_commit, ours_tree):
    """把「对方改了、我们没改」的路径写回本地目录。返回 (采用对方的条目数, 双方都改的条目数)。"""
    base = git.ls_tree(base_commit)
    theirs = git.ls_tree(theirs_commit)
    ours = git.ls_tree(ours_tree)
    taken, both = 0, 0
    for path in sorted(set(base) | set(theirs)):
        b, t, o = base.get(path), theirs.get(path), ours.get(path)
        if t == b:
            continue            # 对方没动
        if o != b:
            if o != t:
                both += 1       # 双方都改：以本次为准
            continue
        dest = work_tree / path
        if t is None:
            if dest.exists():
                dest.unlink()
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(git.blob_bytes(t[1]))
            if t[0] == "100755":
                dest.chmod(0o755)
        taken += 1
    return taken, both


def summarize(git, base_tree, new_tree):
    a = git.ls_tree(base_tree) if base_tree else {}
    b = git.ls_tree(new_tree)
    added = len(set(b) - set(a))
    deleted = len(set(a) - set(b))
    modified = sum(1 for p in set(a) & set(b) if a[p] != b[p])
    return added, modified, deleted


def main(argv=None):
    opts = parse_args(sys.argv[1:] if argv is None else argv)
    token = os.environ.get("GITHUB_TOKEN", "")
    remote = opts["remote"] or f"https://github.com/{REPO}.git"
    work_tree = Path(opts["from_dir"]).resolve()
    if not work_tree.is_dir():
        sys.exit(f"错误：目录不存在 {opts['from_dir']}")
    if remote.startswith("https://") and not token and not opts["dry_run"]:
        sys.exit("错误：需要 GITHUB_TOKEN 环境变量")

    git = Git(opts["repo_dir"], remote, token)

    total = count_files(work_tree)
    print(f"[1/5] 源目录 {opts['from_dir']}：{total} 个文件")
    if total < MIN_FILES:
        sys.exit(f"错误：只收集到 {total} 个文件，低于安全阈值 {MIN_FILES}，拒绝提交（可能是读取失败或源内容缺失）")

    base = opts["expect_oid"] or git.out("rev-parse", "HEAD")
    print(f"[2/5] 基线（克隆时的 {REPORTS_BRANCH}）：{base[:12]}")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        tree = git.write_tree_from_dir(work_tree)
        base_tree = git.tree_of(base)
        added, modified, deleted = summarize(git, base_tree, tree)
        print(f"[3/5] 第 {attempt} 次：tree {tree[:12]}（新增 {added} / 修改 {modified} / 删除 {deleted}）")
        if tree == base_tree:
            print("      与远端内容一致，无需提交")
            return 0
        if opts["dry_run"]:
            print("[dry-run] 未提交、未推送")
            return 0

        commit = git.commit_orphan(tree, (
            f"auto: sync Pages content ({time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC)\n\n"
            "由 scripts/sync_reports_branch.py 生成（--force-with-lease 原子更新）。\n"
            "孤儿提交，无历史，旧 tree 交由 gc 回收。"
        ))
        print(f"[4/5] 孤儿提交 {commit[:12]}，推送（lease = {base[:12]}）…")
        try:
            git.push_with_lease(commit, base)
        except LeaseRejected as e:
            current = str(e)
            print(f"      ⚠️ 远端已被其它运行更新为 {current[:12]}，合并对方的改动后重试")
            if attempt == MAX_ATTEMPTS:
                sys.exit(f"错误：连续 {MAX_ATTEMPTS} 次与并发运行冲突，放弃本次同步")
            theirs = git.fetch_tip()
            taken, both = merge_theirs_into_dir(git, work_tree, base, theirs, tree)
            print(f"      采用对方的 {taken} 个条目；双方都改的 {both} 个以本次为准")
            base = theirs
            continue

        print("[5/5] 回读校验 …")
        got = git.remote_oid()
        if got != commit:
            print(f"  ❌ 校验失败：远端 {got[:12]}，期望 {commit[:12]}", file=sys.stderr)
            return 1
        print(f"\n✅ 已同步 {total} 个文件到 {REPORTS_BRANCH} -> {commit}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
