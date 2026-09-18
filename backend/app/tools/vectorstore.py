"""向量存储：Chroma 持久化文件内容向量，用于相似度分类与检索。

分类思路（规则 + 向量结合）：
1. 先按扩展名规则粗分类（图片/文档/压缩包...）。
2. 对有文本内容的文件，用向量检索最相似的历史文件所属类别，做细分类建议。
"""
from __future__ import annotations

from typing import List, Optional

from ..config import get_settings
from ..logging_conf import get_logger

logger = get_logger(__name__)

_collection = None

# 扩展名 -> 粗分类
EXT_CATEGORY = {
    # 图片
    "png": "图片", "jpg": "图片", "jpeg": "图片", "gif": "图片",
    "bmp": "图片", "webp": "图片", "tiff": "图片", "svg": "图片",
    # 文档
    "pdf": "文档", "doc": "文档", "docx": "文档", "txt": "文档",
    "md": "文档", "ppt": "文档", "pptx": "文档", "xls": "表格", "xlsx": "表格",
    # 压缩
    "zip": "压缩包", "rar": "压缩包", "7z": "压缩包", "tar": "压缩包", "gz": "压缩包",
    # 音视频
    "mp3": "音频", "wav": "音频", "flac": "音频",
    "mp4": "视频", "mkv": "视频", "avi": "视频", "mov": "视频",
    # 安装 / 可执行
    "exe": "安装程序", "msi": "安装程序", "dmg": "安装程序",
}


def rule_category(ext: str) -> str:
    """基于扩展名的粗分类。"""
    return EXT_CATEGORY.get(ext.lower().lstrip("."), "其他")


def _get_collection():
    """惰性初始化 Chroma collection，失败时返回 None（降级为纯规则分类）。"""
    global _collection
    if _collection is not None:
        return _collection
    try:
        import chromadb

        client = chromadb.PersistentClient(path=get_settings().chroma_dir)
        _collection = client.get_or_create_collection(
            name="file_contents",
            metadata={"hnsw:space": "cosine"},
        )
        return _collection
    except Exception as exc:  # noqa: BLE001
        logger.warning("Chroma 初始化失败，降级为纯规则分类: %s", exc)
        return None


def index_file(file_id: str, text: str, category: str, metadata: dict) -> None:
    """把文件内容写入向量库，携带类别标签（供后续相似度分类参考）。"""
    col = _get_collection()
    if col is None or not text.strip():
        return
    try:
        meta = {**metadata, "category": category}
        col.upsert(ids=[file_id], documents=[text[:4000]], metadatas=[meta])
    except Exception as exc:  # noqa: BLE001
        logger.warning("写入向量库失败 %s: %s", file_id, exc)


def suggest_category(text: str, top_k: int = 3) -> Optional[str]:
    """根据内容，用最相似历史文件的多数类别给出细分类建议。"""
    col = _get_collection()
    if col is None or not text.strip():
        return None
    try:
        res = col.query(query_texts=[text[:4000]], n_results=top_k)
        metas = res.get("metadatas") or [[]]
        cats: List[str] = [m.get("category") for m in metas[0] if m.get("category")]
        if not cats:
            return None
        # 多数投票
        return max(set(cats), key=cats.count)
    except Exception as exc:  # noqa: BLE001
        logger.warning("向量检索失败: %s", exc)
        return None
