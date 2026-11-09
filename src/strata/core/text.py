"""跨模块复用的文本工具。"""

__all__ = ["clip"]


def clip(text: str, limit: int) -> str:
    """压成一行并按 ``limit`` 截断，用于历史列表与错误摘要的展示。"""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return f"{flat[: max(0, limit - 1)]}…"
