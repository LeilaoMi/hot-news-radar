"""docs/subscribe.html 生成的片段必须能被 trendradar 的解析器按用户意图读取。

审计 2.4：旧片段输出 [GLOBAL_FILTER] 后直接跟话题组，解析器一直停在全局过滤区，
用户勾选的话题全部变成「命中即丢弃」。这里用 node 跑页面里真实的 generate()，
再把输出交给 load_frequency_words 解析。
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from trendradar.core.frequency import load_frequency_words

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "subscribe.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")

HARNESS = r"""
const state0 = JSON.parse(process.env.SUBSCRIBE_STATE);
const els = {};
function el(id){ return els[id] || (els[id] = {id, value:"", textContent:"", innerHTML:"",
  classList:{toggle(){}, add(){}, remove(){}}, addEventListener(){}}); }
global.document = { getElementById: el, createElement: () => el("_tmp"), body:{appendChild(){}, removeChild(){}} };
global.localStorage = { getItem: () => JSON.stringify(state0), setItem(){}, removeItem(){} };
global.navigator = {};
"""


def run_generate(state: dict) -> str:
    html = PAGE.read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert scripts, "subscribe.html 里找不到内联脚本"
    js = HARNESS + scripts[-1] + "\nprocess.stdout.write(generate());\n"
    env = dict(os.environ, SUBSCRIBE_STATE=json.dumps(state))
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30, env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout


def parse(tmp_path, text):
    p = tmp_path / "frequency_words.txt"
    p.write_text(text, encoding="utf-8")
    return load_frequency_words(str(p))


def test_topics_become_word_groups_not_global_filters(tmp_path):
    out = run_generate({"topics": {"tech": True, "phone": True}, "customName": "我的",
                        "customWords": "华为\n鸿蒙", "globalFilter": "广告, 彩票"})
    groups, _filters, global_filters = parse(tmp_path, out)
    assert global_filters == ["广告", "彩票"]
    assert len(groups) == 3
    flat = json.dumps(groups, ensure_ascii=False)
    assert "AI" in flat and "华为" in flat and "鸿蒙" in flat
    assert not any(w in global_filters for w in ("AI", "华为", "[科技·AI]"))


def test_no_global_filter_still_parses_groups(tmp_path):
    out = run_generate({"topics": {"ev": True}, "customName": "", "customWords": "", "globalFilter": ""})
    groups, _f, global_filters = parse(tmp_path, out)
    assert global_filters == []
    assert len(groups) == 1


def test_reserved_section_name_is_not_used_as_group_alias(tmp_path):
    out = run_generate({"topics": {}, "customName": "GLOBAL_FILTER", "customWords": "苹果", "globalFilter": ""})
    groups, _f, global_filters = parse(tmp_path, out)
    assert global_filters == []
    assert len(groups) == 1


def test_corrupted_draft_does_not_crash():
    out = run_generate({"topics": None})
    assert "关键词订阅片段" in out
