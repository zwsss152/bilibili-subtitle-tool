"""Create archive folders without allowing paths outside the transcript library."""
from pathlib import Path
from .textout import sanitize_filename
from ..paths import OUTPUT_ROOT


def create_topic(name, root=OUTPUT_ROOT):
    if not name.strip():
        raise ValueError("请先输入主题名称")
    root = Path(root).resolve()
    folder = root / sanitize_filename(name.strip())
    if not folder.resolve().is_relative_to(root):
        raise ValueError("主题文件夹必须位于文字稿目录内")
    existed = folder.is_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return folder, existed
