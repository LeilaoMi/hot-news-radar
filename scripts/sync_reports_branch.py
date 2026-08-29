#!/usr/bin/env python3
# coding=utf-8
"""把 Pages 内容同步到 reports 分支（GitHub Pages 的发布源）。

两种取数模式
------------
--from-ref=master（默认，迁移过渡期用）
    reports 分支检出后有 169 MB（1023 份报告快照，每天还在 +2 MB）。若在 CI 里
    clone 该分支再 commit/push，每次运行都要多拉一遍 169 MB，一天 48 次。
    该模式下新 tree 直接复用 master 已有的 blob sha，是**纯指针操作、零上传**，
    耗时与仓库体积无关。retention 清掉的旧快照不在新 tree 里，等于自动完成删除。
    代价：要求 master 上仍保留 docs/reports/。

--from-dir=docs（迁移完成后用）
    master 清掉 docs/reports/ 之后，上面那条路就读不到报告了。该模式改从本地
    工作区取数：先在本地算出每个文件的 git blob sha，与 reports 分支当前内容
    比对，**只上传真正变化的文件**（通常几个快照约 2 MB），未变化的由 base_tree
    原样保留；本地已不存在的文件用 sha=null 从 tree 中删除。

为什么每次都是孤儿提交（无 parent）
-----------------------------------
若做成增量提交，一天 48 个 commit，每个 commit 引用一棵 1000+ 条目的 tree，
光 tree 对象就能在 90 天内堆到 170 MB。reports 分支只是 Pages 的内容快照，
不需要历史，因此每次新建孤儿 commit 并把 ref 强指过去，旧 tree 变为 unreachable
后由 GitHub 的 gc 回收。

安全兜底
--------
强指 ref 是破坏性操作，因此加了 MIN_FILES 阈值：文件数低于该值视为异常
（上游内容缺失或读取失败），直接拒绝提交而不是把线上替换成残缺版本。
另外提交后会回读校验，不一致即失败退出。

用法
----
    GITHUB_TOKEN=xxx python3 scripts/sync_reports_branch.py [--dry-run]
    GITHUB_TOKEN=xxx python3 scripts/sync_reports_branch.py --from-dir=docs

环境变量：
    GITHUB_TOKEN       必填，需 contents: write 权限
    GITHUB_REPOSITORY  形如 owner/repo，CI 里自动注入
    REPORTS_BRANCH     目标分支，默认 reports
"""

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = os.environ.get("GITHUB_REPOSITORY", "LeilaoMi/hot-news-radar")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPORTS_BRANCH = os.environ.get("REPORTS_BRANCH", "reports")

# 单次 create-tree 的条目数上限，实测 1041 个一次性提交会 502，分批累积可绕开
BATCH = 100

# 低于此文件数拒绝提交，防止把线上替换成残缺内容
# （正常情况下 docs/ 下有 1000+ 个文件：18 个静态壳 + 千份报告快照）
# 可用 SYNC_MIN_FILES 覆盖，仅为便于在样本较少的目录上做演练
MIN_FILES = int(os.environ.get("SYNC_MIN_FILES", "500"))

DRY = "--dry-run" in sys.argv
API = f"https://api.github.com/repos/{REPO}"


def parse_args():
    """解析 --from-ref=X / --from-dir=X，默认 --from-ref=master。"""
    for a in sys.argv[1:]:
        if a.startswith("--from-ref="):
            return ("ref", a.split("=", 1)[1])
        if a.startswith("--from-dir="):
            return ("dir", a.split("=", 1)[1])
    return ("ref", "master")


def api(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        API + path if path.startswith("/") else path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + TOKEN,
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "sync-reports-branch",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")
        print(f"  HTTP {e.code} {method} {path}\n  {body[:400]}", file=sys.stderr)
        raise


def http_json(method, path, payload=None, retries=5):
    """带退避重试：网关偶发 5xx，重试即可恢复。"""
    for attempt in range(1, retries + 1):
        try:
            return api(method, path, payload)
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == retries:
                raise
            print(f"    {e.code}，第 {attempt}/{retries} 次重试 …")
            time.sleep(2 * attempt)
    raise RuntimeError("unreachable")


def get_tree(ref):
    d = http_json("GET", f"/git/trees/{ref}?recursive=1")
    if d.get("truncated"):
        sys.exit(f"错误：{ref} 的文件树被截断，无法安全同步（需改成分批遍历）")
    return d["tree"]


def branch_exists(branch):
    try:
        api("GET", f"/git/ref/heads/{branch}")
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def git_blob_sha(data: bytes) -> str:
    """计算 git 的 blob 对象 sha1：sha1("blob <len>\\0" + 内容)。

    有了它就能在本地判断文件是否变化，未变化的直接沿用旧 sha，不必重新上传。
    """
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def create_blob(data: bytes) -> str:
    import base64
    return http_json("POST", "/git/blobs", {
        "content": base64.b64encode(data).decode(),
        "encoding": "base64",
    })["sha"]


# --------------------------------------------------------------------------
# 取数：两种模式
# --------------------------------------------------------------------------

def collect_from_ref(ref):
    """从远端 ref 的 docs/ 收集（零上传，复用既有 blob）。

    返回 (entries, total_files, use_base_tree)。全量重建，因此不用 base_tree。
    """
    entries, seen = [], set()
    for it in get_tree(ref):
        if it["type"] != "blob" or not it["path"].startswith("docs/"):
            continue
        path = it["path"][len("docs/"):]
        if path in seen:
            sys.exit(f"错误：路径冲突 {path}")
        seen.add(path)
        entries.append({"path": path, "mode": it["mode"],
                        "type": "blob", "sha": it["sha"]})
    return entries, len(entries), False


def collect_from_dir(root, branch):
    """从本地目录收集，只上传变化的文件。

    返回 (entries, total_files, use_base_tree=True)。
    """
    base = Path(root)
    if not base.is_dir():
        sys.exit(f"错误：目录不存在 {root}")

    local = {}
    for p in base.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(base).as_posix()
        data = p.read_bytes()
        mode = "100755" if os.access(p, os.X_OK) else "100644"
        local[rel] = (mode, git_blob_sha(data), data)

    if not local:
        sys.exit(f"错误：{root} 下没有任何文件")

    # 远端当前内容（分支不存在时为空）
    remote = {}
    if branch_exists(branch):
        remote = {i["path"]: i["sha"] for i in get_tree(branch) if i["type"] == "blob"}

    entries, cache = [], {}
    uploaded = 0
    for rel, (mode, sha, data) in sorted(local.items()):
        if remote.get(rel) == sha:
            continue  # 未变化，由 base_tree 原样保留
        if sha not in cache:
            if DRY:
                cache[sha] = sha  # dry-run 不真的上传
            else:
                cache[sha] = create_blob(data)
                uploaded += 1
        entries.append({"path": rel, "mode": mode, "type": "blob", "sha": cache[sha]})

    # 本地已不存在的（retention 清理掉的旧快照）从 tree 中删除
    for rel in sorted(set(remote) - set(local)):
        entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": None})

    print(f"      本地 {len(local)} 个文件，需上传 {uploaded} 个，"
          f"删除 {len(set(remote) - set(local))} 个，"
          f"未变化 {len(local) - uploaded} 个（沿用旧 blob）")
    return entries, len(local), True


def main():
    if not TOKEN:
        sys.exit("错误：需要 GITHUB_TOKEN 环境变量")

    mode, src = parse_args()
    exists = branch_exists(REPORTS_BRANCH)

    print(f"[1/6] 模式：{'复用远端 blob（零上传）' if mode == 'ref' else '从本地目录上传'}"
          f"  源={src}")
    if mode == "ref":
        entries, total, use_base = collect_from_ref(src)
    else:
        entries, total, use_base = collect_from_dir(src, REPORTS_BRANCH)

    if total < MIN_FILES:
        sys.exit(
            f"错误：只收集到 {total} 个文件，低于安全阈值 {MIN_FILES}，"
            f"拒绝提交（可能是读取失败或源内容缺失）"
        )
    print(f"      合计 {total} 个文件")

    print(f"[2/6] reports 分支{'已存在' if exists else '不存在（将创建）'}")

    if not entries:
        print("      无增删变更，跳过提交")
        return

    if DRY:
        print(f"\n[dry-run] 将提交 {len(entries)} 个变更条目"
              f"（最终 {total} 个文件），未实际写入")
        return

    base_sha = None
    if use_base and exists:
        base_sha = api("GET", f"/git/ref/heads/{REPORTS_BRANCH}")["object"]["sha"]
        base_sha = api("GET", f"/git/commits/{base_sha}")["tree"]["sha"]

    print(f"[3/6] 分批创建 tree（每批 {BATCH} 条"
          f"{'，base_tree 增量' if base_sha else '，全量重建'}）…")
    tree_sha = base_sha
    for i in range(0, len(entries), BATCH):
        batch = entries[i:i + BATCH]
        payload = {"tree": batch}
        if tree_sha:
            payload["base_tree"] = tree_sha
        tree_sha = http_json("POST", "/git/trees", payload)["sha"]
        print(f"      批次 {i // BATCH + 1}: {len(batch)} 条 -> {tree_sha[:12]}")
    print(f"      tree: {tree_sha[:12]}")

    print("[4/6] 创建孤儿 commit（不累积历史）…")
    commit = http_json("POST", "/git/commits", {
        "message": (
            f"auto: sync Pages content ({time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC)\n\n"
            f"由 scripts/sync_reports_branch.py 生成，取数模式 --from-{mode}。\n"
            "孤儿提交，无历史，旧 tree 交由 gc 回收。"
        ),
        "tree": tree_sha,
        "parents": [],
    })
    print(f"      commit: {commit['sha'][:12]}")

    print(f"[5/6] 更新 refs/heads/{REPORTS_BRANCH} …")
    if exists:
        http_json("PATCH", f"/git/refs/heads/{REPORTS_BRANCH}",
                  {"sha": commit["sha"], "force": True})
    else:
        http_json("POST", "/git/refs",
                  {"ref": f"refs/heads/{REPORTS_BRANCH}", "sha": commit["sha"]})

    print(f"[6/6] 回读校验 …")
    got = {i["path"]: i["sha"] for i in get_tree(REPORTS_BRANCH) if i["type"] == "blob"}
    if len(got) != total:
        print(f"  ❌ 校验失败：远端 {len(got)} 个文件，期望 {total} 个", file=sys.stderr)
        sys.exit(1)

    print(f"\n✅ 已同步 {len(got)} 个文件到 {REPORTS_BRANCH} -> {commit['sha']}")


if __name__ == "__main__":
    main()
