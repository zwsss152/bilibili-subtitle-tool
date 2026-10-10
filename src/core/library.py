"""One document identity with two exported TXT variants."""
from pathlib import Path
from ..paths import OUTPUT_ROOT

ORIGINAL = '时间戳原稿'
READING = '整理阅读稿'


def original_path(path, root=None):
    root = Path(root or OUTPUT_ROOT).resolve()
    path = Path(path).resolve()
    parts = path.relative_to(root).parts
    if not parts:
        raise ValueError('稿件路径无效')
    if parts[0] in (ORIGINAL, READING):
        parts = parts[1:]
    return root / ORIGINAL / Path(*parts)


def reading_path(path, root=None):
    root = Path(root or OUTPUT_ROOT).resolve()
    original = original_path(path, root)
    return root / READING / original.relative_to(root / ORIGINAL)


def originals(root=None):
    root = Path(root or OUTPUT_ROOT)
    return [p for p in root.rglob('*.txt') if p.relative_to(root).parts[0] != READING]


def topics(root=None):
    root = Path(root or OUTPUT_ROOT)
    names = set()
    folder = root / ORIGINAL
    if folder.exists():
        names.update(p.name for p in folder.iterdir() if p.is_dir())
    return sorted(names)
