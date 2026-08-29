#!/usr/bin/env python3
# coding=utf-8
"""把 master:/docs 的内容同步到 reports 分支（GitHub Pages 的发布源）。

为什么用 Git Data API 而不是 git clone + push
---------------------------------------------
reports 分支检出后有 169 MB（1023 份报告快照，每天还在 +2 MB）。若在 CI 里
clone 该分支再 commit/push，每次运行都要多拉一遍 169 MB，一天 48 次。

改用 API 后是**纯指针操作、零上传**：master 的 docs/ 与 reports 分支的内容
本就一一对应（只差一层 docs/ 前缀），新 tree 直接复用 master 已有的 blob sha，
因此耗时与仓库体积无关。附带好处——retention 清理掉的旧快照不在新 tree 里，
等于自动完成删除，不需要单独维护删除列表。

为什么每次都是孤儿提交（无 parent）
-----------------------------------
若做成增量提交，一天 48 个 commit，每个 commit 引用一棵 1041 条目的 tree，
光 tree 对象就能在 90 天内堆到 170 MB。reports 分支只是 Pages 的内容快照，
不需要历史，因此每次新建一个孤儿 commit 并把 ref 强指过去，旧 tree 变为
unreachable 后由 GitHub 的 gc 回收。

安全兜底
--------
强指 ref 是破坏性操作，因此加了 MIN_FILES 阈值：新 tree 的文件数低于该值
视为异常（说明上游 docs/ 内容缺失或读取失败），直接拒绝提交而不是把线上
内容替换成残缺版本。

用法
----
    GITHUB_TOKEN=xxx python3 scripts/sync_reports_branch.py [--dry-run]

环境变量：
    GITHUB_TOKEN   必填，需 contents: write 权限
    GITHUB_REPOSITORY  形如 owner/repo，CI 里自动注入；本地可缺省为下方默认值
    REPORTS_BRANCH 目标分支，默认 reports
    SOURCE_REF     源分支，默认 master
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

REPO = os.environ.get("GITHUB_REPOSITORY", "LeilaoMi/hot-news-radar")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
SOURCE_REF = os.environ.get("SOURCE_REF", "master")
REPORTS_BRANCH = os.environ.get("REPORTS_BRANCH", "reports")

# 单次 create-tree 的条目数上限，实测 1041 个一次性提交会 502，分批累积可绕开
BATCH = 100

# 低于此文件数拒绝提交，防止把线上替换成残缺内容
# （正常情况 docs/ 下有 1000+ 个文件：18 个静态壳 + 千份报告快照）
MIN_FILES = 500

DRY = "--dry-run" in sys.argv
API = f"https://api.github.com/repos/{REPO}"


def api(method, path, payload=None, expect=(200, 201, 204)):
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


def build_entries(tree):
    """把 master 的 docs/ 映射为 reports 分支的根目录（去掉 docs/ 前缀）。"""
    entries = []
    seen = set()
    for it in tree:
        if it["type"] != "blob" or not it["path"].startswith("docs/"):
            continue
        path = it["path"][len("docs/"):]
        if path in seen:
            sys.exit(f"错误：路径冲突 {path}")
        seen.add(path)
        entries.append({
            "path": path,
            "mode": it["mode"],
            "type": "blob",
            "sha": it["sha"],
        })
    return entries


def branch_exists(branch):
    try:
        api("GET", f"/git/ref/heads/{branch}")
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def main():
    if not TOKEN:
        sys.exit("错误：需要 GITHUB_TOKEN 环境变量")

    print(f"[1/6] 读取 {SOURCE_REF}:/docs 的文件清单 …")
    master_tree = get_tree(SOURCE_REF)
    entries = build_entries(master_tree)
    if len(entries) < MIN_FILES:
        sys.exit(
            f"错误：{SOURCE_REF} 的 docs/ 下只有 {len(entries)} 个文件，"
            f"低于安全阈值 {MIN_FILES}，拒绝提交（可能是读取失败）"
        )
    print(f"      {len(entries)} 个文件")

    exists = branch_exists(REPORTS_BRANCH)
    print(f"[2/6] reports 分支{'已存在' if exists else '不存在（将创建）'}")

    # 内容无变化就跳过，避免每天制造 48 个内容相同的 commit
    if exists:
        old = {i["path"]: i["sha"] for i in get_tree(REPORTS_BRANCH) if i["type"] == "blob"}
        new = {e["path"]: e["sha"] for e in entries}
        if old == new:
            print(f"      内容无变化（{len(new)} 个文件完全一致），跳过提交")
            return
        changed = [p for p in set(old) & set(new) if old[p] != new[p]]
        print(f"      差异：新增 {len(set(new) - set(old))}，"
              f"删除 {len(set(old) - set(new))}，变更 {len(changed)}")

    if DRY:
        print(f"\n[dry-run] 将提交 {len(entries)} 个文件到 {REPORTS_BRANCH}，未实际写入")
        return

    print(f"[3/6] 分批创建 tree（每批 {BATCH} 条，base_tree 累积）…")
    tree_sha = None
    for i in range(0, len(entries), BATCH):
        batch = entries[i:i + BATCH]
        payload = {"tree": batch}
        if tree_sha:
            payload["base_tree"] = tree_sha
        tree_sha = http_json("POST", "/git/trees", payload)["sha"]
        print(f"      批次 {i // BATCH + 1}: +{len(batch)} -> {tree_sha[:12]}")
    print(f"      tree: {tree_sha[:12]}")

    print("[4/6] 创建孤儿 commit（不累积历史）…")
    commit = http_json("POST", "/git/commits", {
        "message": (
            f"auto: sync Pages content from {SOURCE_REF}:/docs "
            f"({time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC)\n\n"
            "由 scripts/sync_reports_branch.py 生成。孤儿提交，无历史，"
            "旧 tree 交由 gc 回收。"
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

    print(f"[6/6] 校验 {REPORTS_BRANCH} 落库结果 …")
    got = {i["path"]: i["sha"] for i in get_tree(REPORTS_BRANCH) if i["type"] == "blob"}
    want = {e["path"]: e["sha"] for e in entries}
    if got != want:
        print(f"  ❌ 校验失败：远端 {len(got)} 个文件，期望 {len(want)} 个", file=sys.stderr)
        for p in list(set(want) - set(got))[:5]:
            print(f"       缺失 {p}", file=sys.stderr)
        sys.exit(1)

    print(f"\n✅ 已同步 {len(got)} 个文件到 {REPORTS_BRANCH} -> {commit['sha']}")


if __name__ == "__main__":
    main()
