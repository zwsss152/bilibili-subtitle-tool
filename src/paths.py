"""Separate immutable application resources from writable user data."""
import os
import sys
from pathlib import Path

APP_ID = 'BilibiliSubtitleTool'


def application_paths(*, frozen=None, bundle=None, local_app_data=None, override=None):
    frozen = bool(getattr(sys, 'frozen', False)) if frozen is None else frozen
    resources = Path(bundle or getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1])).resolve()
    override = os.environ.get('BILIBILI_SUBTITLE_DATA_DIR') if override is None else override
    if override:
        data = Path(override).expanduser()
        if not data.is_absolute():
            raise ValueError('数据目录必须使用完整路径')
    elif frozen:
        data = Path(local_app_data or os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local') / APP_ID
    else:
        data = resources
    return resources, data.resolve()


BUNDLE_ROOT, DATA_ROOT = application_paths()
# Compatibility name for persisted paths and existing ownership checks.
PROJECT_ROOT = DATA_ROOT
OUTPUT_ROOT = PROJECT_ROOT / "文字稿"
RECORDS_DIR = PROJECT_ROOT / '.data' / 'transcripts'
ASR_CACHE_DIR = PROJECT_ROOT / "asr_cache"
ASSETS_DIR = BUNDLE_ROOT / "assets"
APP_ICON = ASSETS_DIR / "视频列表.ico"
CONFIG_PATH = PROJECT_ROOT / "app_config.json"
QUEUE_STATE_PATH = PROJECT_ROOT / "queue_state.json"
LOGS_DIR = PROJECT_ROOT / "logs"


def initialize():
    from .core.library import ORIGINAL, READING
    for directory in (OUTPUT_ROOT / ORIGINAL, OUTPUT_ROOT / READING, RECORDS_DIR, ASR_CACHE_DIR, LOGS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def relative(path):
    return str(Path(path).resolve().relative_to(PROJECT_ROOT))


def resolve_owned(path):
    """Reject paths outside the new project before file operations."""
    result = (PROJECT_ROOT / path).resolve()
    if not result.is_relative_to(PROJECT_ROOT):
        raise ValueError("文件路径不在当前项目中")
    return result
