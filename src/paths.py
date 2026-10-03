"""All application data belongs to this checkout."""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = PROJECT_ROOT / "文字稿"
RECORDS_DIR = PROJECT_ROOT / '.data' / 'transcripts'
MODELS_DIR = PROJECT_ROOT / "models"
ASR_CACHE_DIR = PROJECT_ROOT / "asr_cache"
ASSETS_DIR = PROJECT_ROOT / "assets"
APP_ICON = ASSETS_DIR / "视频列表.ico"
CONFIG_PATH = PROJECT_ROOT / "app_config.json"
QUEUE_STATE_PATH = PROJECT_ROOT / "queue_state.json"
LOGS_DIR = PROJECT_ROOT / "logs"
PROFILE_PATH = PROJECT_ROOT / "recognition_profile.json"


def initialize():
    for directory in (OUTPUT_ROOT, RECORDS_DIR, MODELS_DIR, ASR_CACHE_DIR, LOGS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def relative(path):
    return str(Path(path).resolve().relative_to(PROJECT_ROOT))


def resolve_owned(path):
    """Reject paths outside the new project before file operations."""
    result = (PROJECT_ROOT / path).resolve()
    if not result.is_relative_to(PROJECT_ROOT):
        raise ValueError("文件路径不在当前项目中")
    return result
