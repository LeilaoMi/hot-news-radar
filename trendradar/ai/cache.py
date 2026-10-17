# coding=utf-8
"""AI 结果缓存（SQLite 持久化）。

动机：同一新闻标题跨运行、跨批次反复翻译，重复消耗 token 与时长；
Actions 单次运行也有两轮执行（主流程 + daily 补跑），文件级缓存在
同一 Runner VM 内天然共享；Docker 常驻部署则获得完整跨运行收益。

设计：
- key = sha256(kind + '\\x00' + content)。kind 携带用途与目标语言
  （如 ``translate:English``），不同用途/语言互不污染。
- 单文件 SQLite，路径优先级：env ``AI_CACHE_DB`` > ai.CACHE_DB_PATH >
  yaml ``ai.cache_db_path`` > ``output/ai_cache.db``（相对工作目录，即仓库根）。
  schema 不匹配或文件损坏时自动降级为不可用——缓存可随时丢弃，不影响正确性。
- 所有缓存操作失败一律静默降级为未命中：缓存绝不能拖垮业务主流程。
- 环境变量 ``AI_CACHE_ENABLED`` 是**总闸**：设为 false/0/no/off 时，无论各功能的
  ``cache_enabled`` 如何都关闭；未设置时默认开启。
- TTL：env ``AI_CACHE_TTL_DAYS`` > yaml ``ai.cache_ttl_days`` > 7 天；过期条目读取时视为
  未命中，打开时顺带清理（<=0 表示永不过期）。

注意：GitHub Actions 每次都是全新 checkout，若不用 actions/cache 持久化该文件，
缓存只在同一 job 内（主流程 + daily 补跑）有效，不能跨运行复用。
"""
import hashlib
import sqlite3
import time
from pathlib import Path

from trendradar.ai import settings

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
    """env AI_CACHE_ENABLED 总闸：未设置或无法识别 → True；识别为 false → False。"""
    parsed = settings.env_bool("AI_CACHE_ENABLED")
    return True if parsed is None else parsed


class AICache:
    """基于 SQLite 的 AI 调用结果缓存。"""

    def __init__(self, db_path=None, enabled=None, ttl_seconds=None):
        """
        Args:
            db_path: 缓存数据库路径；默认见 settings.cache_db_path()（env AI_CACHE_DB >
                yaml ai.cache_db_path > output/ai_cache.db）
            enabled: 功能级开关（None 视为开启）；env AI_CACHE_ENABLED=false 时总闸关闭
            ttl_seconds: 过期时间；默认见 settings.cache_ttl_seconds()（7 天，<=0 不过期）
        """
        feature_on = True if enabled is None else settings.parse_bool(enabled) is not False
        self.enabled = feature_on and _env_enabled()
        self.db_path = None
        self._conn = None
        self.ttl_seconds = settings.cache_ttl_seconds() if ttl_seconds is None else float(ttl_seconds)
        if not self.enabled:
            return

        if not db_path:
            db_path = settings.cache_db_path()
        self.db_path = Path(db_path)
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False：通知发送可能来自工作线程；
            # Python 3.11+ 的 sqlite3 模块本身已做序列化保护
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.execute(_SCHEMA)
            if self.ttl_seconds > 0:
                self._conn.execute(
                    "DELETE FROM ai_cache WHERE created_at < ?",
                    (time.time() - self.ttl_seconds,),
                )
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
                "SELECT result, created_at FROM ai_cache WHERE cache_key = ?",
                (self.make_key(kind, content),),
            ).fetchone()
            if not row:
                return None
            if self.ttl_seconds > 0 and (time.time() - float(row[1] or 0)) > self.ttl_seconds:
                return None
            return row[0]
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
