# coding=utf-8
"""
路径解析工具

统一 MCP Server 各模块对“项目根目录 / 配置文件 / 数据目录”的理解，
保证 ``--project-root`` 与 ``storage.local.data_dir`` 在所有工具里一致生效：

- 数据库布局（与 ``trendradar/storage/local.py`` 一致）：``{data_dir}/{news|rss}/{YYYY-MM-DD}.db``
- ``data_dir`` 取自 ``config/config.yaml`` 的 ``storage.local.data_dir``，默认 ``output``；
  相对路径相对于项目根目录，绝对路径原样使用。
"""

from pathlib import Path
from typing import Optional, Union

import yaml

PathLike = Union[str, Path]

DEFAULT_DATA_DIR = "output"
VALID_DB_TYPES = ("news", "rss")

# 由 server 在启动时根据 --project-root 设置；None 表示使用包所在目录的上一级
_project_root_override: Optional[Path] = None


def default_project_root() -> Path:
    """包安装位置推导出的项目根目录（mcp_server 的上一级）"""
    return Path(__file__).resolve().parent.parent.parent


def set_project_root(project_root: Optional[PathLike]) -> None:
    """设置全局项目根目录（供无法拿到实例 project_root 的模块级函数使用）"""
    global _project_root_override
    _project_root_override = Path(project_root).resolve() if project_root else None


def resolve_project_root(project_root: Optional[PathLike] = None) -> Path:
    """显式参数 > 全局设置 > 默认目录"""
    if project_root:
        return Path(project_root)
    if _project_root_override is not None:
        return _project_root_override
    return default_project_root()


def get_config_path(project_root: Optional[PathLike] = None) -> Path:
    return resolve_project_root(project_root) / "config" / "config.yaml"


def _load_storage_local_config(project_root: Path) -> dict:
    config_path = project_root / "config" / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    storage = config.get("storage") or {}
    return storage.get("local") or {}


def get_data_dir(project_root: Optional[PathLike] = None, data_dir: Optional[str] = None) -> Path:
    """返回数据根目录（默认 ``{project_root}/output``）。

    Args:
        project_root: 项目根目录
        data_dir: 显式指定的 data_dir（不传则读 config.yaml）
    """
    root = resolve_project_root(project_root)
    if data_dir is None:
        data_dir = _load_storage_local_config(root).get("data_dir") or DEFAULT_DATA_DIR
    path = Path(data_dir)
    if not path.is_absolute():
        path = root / path
    return path


def get_db_path(data_dir: PathLike, db_type: str, date_str: str) -> Path:
    """``{data_dir}/{db_type}/{date_str}.db``，与爬虫写入和 MCP 读取的路径一致。"""
    if db_type not in VALID_DB_TYPES:
        raise ValueError(f"不支持的数据库类型: {db_type}")
    return Path(data_dir) / db_type / f"{date_str}.db"
