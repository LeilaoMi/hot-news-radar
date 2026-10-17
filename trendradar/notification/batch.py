# coding=utf-8
"""
批次处理模块

提供消息分批发送的辅助函数：
- 按渠道计量消息长度（字节 / UTF-16 码元）
- 超长内容按行、再按字符边界拆分（不丢内容；Telegram 拆分时保持 HTML 标签闭合）
- 批次头部添加
"""

import re
from typing import Callable, List, Optional

# ---------------------------------------------------------------------------
# 计量
# ---------------------------------------------------------------------------

# 以字符（UTF-16 码元）计长度的渠道；其余渠道按 UTF-8 字节计
#   Telegram sendMessage: text 最多 4096 字符（按 UTF-16 码元计算实体偏移）
CHAR_MEASURED_FORMATS = {"telegram"}

# 各渠道硬上限（单位见 CHAR_MEASURED_FORMATS），用于最终兜底检查
CHANNEL_HARD_LIMITS = {
    "telegram": 4096,      # 字符
    "ntfy": 4096,          # 字节（超过会被当作附件）
    "bark": 4096,          # 字节（APNs 整个 payload 上限）
    "wework": 4096,        # 字节（markdown content）
    "wework_text": 2048,   # 字节（text content）
    "dingtalk": 20000,     # 字节
    "feishu": 30000,       # 字节
    "slack": 40000,        # 字符（text 字段，按字节计更保守）
}


def utf16_len(text: str) -> int:
    """UTF-16 码元长度（Telegram 计数方式；emoji 等增补平面字符计 2）"""
    return len(text.encode("utf-16-le")) // 2


def text_size(text: str, format_type: Optional[str] = None) -> int:
    """按渠道计量文本长度：Telegram 用 UTF-16 码元，其余用 UTF-8 字节"""
    if format_type in CHAR_MEASURED_FORMATS:
        return utf16_len(text)
    return len(text.encode("utf-8"))


def get_measure(format_type: Optional[str]) -> Callable[[str], int]:
    """返回该渠道的计量函数"""
    if format_type in CHAR_MEASURED_FORMATS:
        return utf16_len
    return lambda s: len(s.encode("utf-8"))


# ---------------------------------------------------------------------------
# 批次头部
# ---------------------------------------------------------------------------


def get_batch_header(format_type: str, batch_num: int, total_batches: int) -> str:
    """根据 format_type 生成对应格式的批次头部

    Args:
        format_type: 推送类型（telegram, slack, wework_text, bark, feishu, dingtalk, ntfy, wework）
        batch_num: 当前批次编号
        total_batches: 总批次数

    Returns:
        格式化的批次头部字符串
    """
    if format_type == "telegram":
        return f"<b>[第 {batch_num}/{total_batches} 批次]</b>\n\n"
    elif format_type == "slack":
        return f"*[第 {batch_num}/{total_batches} 批次]*\n\n"
    elif format_type in ("wework_text", "bark"):
        # 企业微信文本模式和 Bark 使用纯文本格式
        return f"[第 {batch_num}/{total_batches} 批次]\n\n"
    else:
        # 飞书、钉钉、ntfy、企业微信 markdown 模式
        return f"**[第 {batch_num}/{total_batches} 批次]**\n\n"


def get_max_batch_header_size(format_type: str) -> int:
    """估算批次头部的最大长度（假设最多 999 批次），单位与 text_size 一致

    用于在分批时预留空间，避免事后截断破坏内容完整性。
    """
    max_header = get_batch_header(format_type, 999, 999)
    return text_size(max_header, format_type)


# ---------------------------------------------------------------------------
# 截断（保留给外部调用方；推送主流程已不再截断内容）
# ---------------------------------------------------------------------------


def truncate_to_bytes(text: str, max_bytes: int) -> str:
    """安全截断字符串到指定字节数，避免截断多字节字符"""
    text_bytes = text.encode("utf-8")
    if len(text_bytes) <= max_bytes:
        return text
    return text_bytes[:max(0, max_bytes)].decode("utf-8", errors="ignore")


def truncate_at_line_boundary(text: str, max_bytes: int) -> str:
    """在行边界处截断（先按字节截断，再回退到最近的换行符位置）"""
    if len(text.encode("utf-8")) <= max_bytes:
        return text

    rough_cut = truncate_to_bytes(text, max_bytes)
    last_newline = rough_cut.rfind("\n")
    if last_newline > 0:
        return rough_cut[:last_newline]
    return rough_cut


def truncate_preserving_footer(content: str, max_bytes: int) -> str:
    """截断内容，优先保留尾部 footer（兼容旧接口）"""
    if len(content.encode("utf-8")) <= max_bytes:
        return content
    body, footer = split_footer(content)
    footer_size = len(footer.encode("utf-8"))
    if not footer or footer_size >= max_bytes:
        return truncate_at_line_boundary(content, max_bytes)
    return truncate_at_line_boundary(body, max_bytes - footer_size) + footer


# ---------------------------------------------------------------------------
# 拆分（不丢内容）
# ---------------------------------------------------------------------------

# Telegram HTML 词法：标签 / 实体 / 单个字符
_TG_TOKEN_RE = re.compile(r"<[^<>]*>|&[#A-Za-z0-9]+;|.", re.DOTALL)
_TG_TAG_NAME_RE = re.compile(r"^<\s*(/?)\s*([A-Za-z][A-Za-z0-9-]*)")
_TG_VOID_TAGS: set = set()
# 优先断开位置：空白、中英文标点之后
_BREAK_CHARS = set(" \t,，。；;、！!？?)）]】」》>|")


def _split_plain(
    text: str, limit: int, measure: Callable[[str], int], first_limit: Optional[int] = None
) -> List[str]:
    """按字符边界把一段文本拆成每段 ≤ limit 的块（第一块 ≤ first_limit），尽量在空白/标点处断开"""
    if limit <= 0:
        raise ValueError("limit must be positive")
    chunks: List[str] = []
    current = ""
    current_size = 0
    last_break = -1  # current 中最近可断位置（该字符之后）
    for ch in text:
        ch_size = measure(ch)
        cur_limit = first_limit if (first_limit and not chunks) else limit
        if current and current_size + ch_size > cur_limit:
            # 断在最近的可断点（不早于块长度的一半，避免切出太碎的块）
            if last_break >= len(current) // 2:
                head, tail = current[: last_break + 1], current[last_break + 1:]
            else:
                head, tail = current, ""
            chunks.append(head)
            current = tail
            current_size = measure(current) if current else 0
            last_break = -1
            for idx, c in enumerate(current):
                if c in _BREAK_CHARS:
                    last_break = idx
        current += ch
        current_size += ch_size
        if ch in _BREAK_CHARS:
            last_break = len(current) - 1
    if current:
        chunks.append(current)
    return chunks


def _split_telegram_html(
    text: str, limit: int, measure: Callable[[str], int], first_limit: Optional[int] = None
) -> List[str]:
    """Telegram HTML 感知拆分：不在标签/实体中间断开，跨块的标签在块尾闭合、下一块开头重开"""
    tokens = _TG_TOKEN_RE.findall(text)
    chunks: List[str] = []
    stack: List[tuple] = []  # (tag_name, opening_tag_text)
    current = ""
    current_size = 0
    has_text = False

    def closing_for(stk: List[tuple]) -> str:
        return "".join(f"</{name}>" for name, _ in reversed(stk))

    def reopen_for(stk: List[tuple]) -> str:
        return "".join(open_tag for _, open_tag in stk)

    for tok in tokens:
        tok_size = measure(tok)
        # 预计加入该 token 后块尾需要的闭合标签长度
        new_stack = list(stack)
        m = _TG_TAG_NAME_RE.match(tok) if tok.startswith("<") else None
        if m:
            is_close, name = m.group(1) == "/", m.group(2).lower()
            if is_close:
                for k in range(len(new_stack) - 1, -1, -1):
                    if new_stack[k][0] == name:
                        del new_stack[k:]
                        break
            elif not tok.endswith("/>") and name not in _TG_VOID_TAGS:
                new_stack.append((name, tok))
        projected = current_size + tok_size + measure(closing_for(new_stack))
        cur_limit = first_limit if (first_limit and not chunks) else limit
        if has_text and projected > cur_limit:
            chunks.append(current + closing_for(stack))
            current = reopen_for(stack)
            current_size = measure(current)
            has_text = False
        if not m and tok.strip():
            has_text = True
        current += tok
        current_size += tok_size
        stack = new_stack
    if current:
        chunks.append(current + closing_for(stack))
    # 极端情况：重开标签本身就超过 limit（几乎不可能）→ 退化为纯文本拆分
    out: List[str] = []
    for idx, c in enumerate(chunks):
        cap = first_limit if (first_limit and idx == 0) else limit
        if measure(c) > cap:
            out.extend(_split_plain(c, limit, measure, first_limit=cap if idx == 0 else None))
        else:
            out.append(c)
    return out


def split_long_line(
    line: str, limit: int, format_type: Optional[str] = None, first_limit: Optional[int] = None
) -> List[str]:
    """把单行拆成每段 ≤ limit（按渠道计量）的若干段，不丢任何内容

    first_limit: 第一段的上限（用于把第一段填进当前批次的剩余空间），默认与 limit 相同
    """
    measure = get_measure(format_type)
    if measure(line) <= (first_limit or limit):
        return [line]
    if format_type == "telegram":
        return _split_telegram_html(line, limit, measure, first_limit)
    return _split_plain(line, limit, measure, first_limit)


def split_text_to_limit(text: str, limit: int, format_type: Optional[str] = None) -> List[str]:
    """按行把文本装箱为每段 ≤ limit 的块；单行超限时按字符边界继续拆"""
    measure = get_measure(format_type)
    if measure(text) <= limit:
        return [text]
    chunks: List[str] = []
    current = ""
    for line in text.split("\n"):
        piece = line + "\n"
        if measure(current + piece) <= limit:
            current += piece
            continue
        if current:
            chunks.append(current)
            current = ""
        if measure(piece) <= limit:
            current = piece
        else:
            parts = split_long_line(line, limit - measure("\n"), format_type)
            chunks.extend(p + "\n" for p in parts[:-1])
            current = parts[-1] + "\n"
    if current:
        chunks.append(current)
    # 去掉原文没有的最后一个换行
    if chunks and not text.endswith("\n") and chunks[-1].endswith("\n"):
        chunks[-1] = chunks[-1][:-1]
    return [c for c in chunks if c]


_FOOTER_ANCHOR = "更新时间："
_MAX_FOOTER_LEN = 600


def split_footer(content: str) -> tuple:
    """从批次中拆出 splitter 追加的 footer（以「更新时间：」开头的尾部段落）

    只把最后一个「更新时间：」所在的、紧贴文末的短段落识别为 footer，
    正文里出现 "\\n\\n> " / "\\n\\n_" 之类序列（如 AI 引用块）不会再被误判。

    Returns:
        (body, footer)；没有 footer 时 footer 为空串
    """
    anchor = content.rfind(_FOOTER_ANCHOR)
    if anchor < 0:
        return content, ""
    start = content.rfind("\n\n", 0, anchor)
    if start < 0:
        return content, ""
    # footer 起点向前吞掉连续换行
    while start > 0 and content[start - 1] == "\n":
        start -= 1
    footer = content[start:]
    if len(footer) > _MAX_FOOTER_LEN:
        return content, ""
    return content[:start], footer


def _split_oversized_batch(content: str, max_size: int, format_type: Optional[str] = None) -> List[str]:
    """将超限批次拆分成多个子批次（每个都保留 footer，不丢内容）"""
    measure = get_measure(format_type)
    body, footer = split_footer(content)
    available = max_size - measure(footer)
    if footer and available < max_size // 4:
        # footer 太大，放弃逐批保留 footer
        body, footer, available = content, "", max_size
    parts = split_text_to_limit(body, available, format_type)
    return [p + footer for p in parts if p.strip()] or [content]


def add_batch_headers(
    batches: List[str], format_type: str, max_bytes: int
) -> List[str]:
    """为批次添加头部，超限时拆分成多个子批次（不丢弃内容）

    Args:
        batches: 原始批次列表
        format_type: 推送类型（bark, telegram, feishu 等）
        max_bytes: 该推送类型的单条上限（Telegram 为字符数，其余为字节数）

    Returns:
        添加头部后的批次列表
    """
    if not batches:
        return batches

    measure = get_measure(format_type)
    max_header_size = get_max_batch_header_size(format_type)

    # 第一遍：拆分超限批次（包括只有一批的情况）
    expanded: List[str] = []
    for content in batches:
        if measure(content) > max_bytes or (len(batches) > 1 and measure(content) + max_header_size > max_bytes):
            expanded.extend(_split_oversized_batch(content, max_bytes - max_header_size, format_type))
        else:
            expanded.append(content)

    if len(expanded) <= 1:
        return expanded

    # 第二遍：添加头部（已预留头部空间，不会超限）
    total = len(expanded)
    return [get_batch_header(format_type, i, total) + content for i, content in enumerate(expanded, 1)]
