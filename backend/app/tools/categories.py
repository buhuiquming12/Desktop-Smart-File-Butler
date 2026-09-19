"""按扩展名的粗分类规则。

细分类别由 LLM 依据文件名与内容给出（见 ``agent/graph.py:_llm_classify``）；
这里只提供不依赖模型的兜底分类，用于 LLM 不可用或缺内容时回退。
"""
from __future__ import annotations

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
    """基于扩展名的粗分类，未知扩展名归入「其他」。"""
    return EXT_CATEGORY.get(ext.lower().lstrip("."), "其他")
