# coding=utf-8
"""AI 结果缓存（SQLite 持久化）。

动机：同一新闻标题跨运行、跨批次反复翻译，重复消耗 token 与时长；
Actions 单次运行也有两轮执行（主流程 + daily 补跑），文件级缓存在
同一 Runner VM 内天然共享；Docker 常驻部署则获得完整跨运行收益。

设计：
- key = sha256(kind + '\\x00' + content)。kind 携带用途与目标语言
  （如 ``translate:English``），不同用途/语言互不污染。
- 单文件 SQLite（默认 ``output/ai_cache.db``），schema 不匹配或文件
  损坏时自动降级为不可用并尝试重建——缓存可随时丢弃，不影响正确性。
- 所有缓存操作失败一律静默降级为未命中：缓存绝不能拖垮业务主流程。
- 环境变量 ``AI_CACHE_ENABLED=false`` 整体关闭（默认开启）；
  ``AI_CACHE_DB`` 可覆盖存储路径。
"""
import hashlib
import os
import sqlite3
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_cache (
    cache_key  TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    content    TEXT NOT NULL,
    result     TEXT NOT NULL,
    created_at REAL NOT NULL
)
"""


def _env_enabled() -> bool:
    return os.environ.get("AI_CACHE_ENABLED", "").strip().lower() not in ("0", "false", "no")


class AICache:
    """基于 SQLite 的 AI 调用结果缓存。"""

    def __init__(self, db_path=None, enabled=None):
        """
        Args:
            db_path: 缓存数据库路径；默认取 env AI_CACHE_DB，再退到 output/ai_cache.db
            enabled: 显式开关；默认取 env AI_CACHE_ENABLED（缺省开启）
        """
        self.enabled = _env_enabled() if enabled is None else bool(enabled)
        self.db_path = None
        self._conn = None
        if not self.enabled:
            return

        if db_path is None:
            db_path = os.environ.get("AI_CACHE_DB") or "output/ai_cache.db"
        self.db_path = Path(db_path)
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False：通知发送可能来自工作线程；
            # Python 3.11+ 的 sqlite3 模块本身已做序列化保护
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.execute(_SCHEMA)
            self._conn.commit()
        except Exception:
            # 打不开缓存 ≠ 不能翻译：整体降级为禁用
            self.enabled = False
            self._conn = None

    @staticmethod
    def make_key(kind: str, content: str) -> str:
        """缓存键：sha256(kind + NUL + content)。"""
        return hashlib.sha256(f"{kind}\x00{content}".encode("utf-8")).hexdigest()

    def get(self, kind: str, content: str):
        """查询缓存；未命中或任何异常返回 None。"""
        if not self.enabled or self._conn is None:
            return None
        try:
            row = self._conn.execute(
                "SELECT result FROM ai_cache WHERE cache_key = ?",
                (self.make_key(kind, content),),
            ).fetchone()
            return row[0] if row else None
        except Exception:
            return None

    def put(self, kind: str, content: str, result: str) -> None:
        """写入缓存；任何异常静默忽略。空结果不缓存（避免污染）。"""
        if not self.enabled or self._conn is None:
            return
        if not result or not str(result).strip():
            return
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO ai_cache "
                "(cache_key, kind, content, result, created_at) VALUES (?, ?, ?, ?, ?)",
                (self.make_key(kind, content), kind, content, str(result), time.time()),
            )
            self._conn.commit()
        except Exception:
            pass

    def clear(self) -> int:
        """清空缓存，返回清除的条数（管理用途/测试）。"""
        if not self.enabled or self._conn is None:
            return 0
        try:
            cur = self._conn.execute("DELETE FROM ai_cache")
            self._conn.commit()
            return cur.rowcount or 0
        except Exception:
            return 0

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
