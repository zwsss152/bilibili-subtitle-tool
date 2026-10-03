"""TXT-only export folders; internal records follow their owning transcript."""
import hashlib
import json
from pathlib import Path
from ..paths import OUTPUT_ROOT, RECORDS_DIR, PROJECT_ROOT
from ..config import atomic_json


def record_path(txt):
    path = Path(txt).resolve()
    key = hashlib.sha256(str(path).casefold().encode('utf-8')).hexdigest()
    return RECORDS_DIR / (key + '.json')


def migrate_records(log=lambda *a: None):
    moved = {}
    for old in OUTPUT_ROOT.rglob('*.json'):
        if not old.resolve().is_relative_to(OUTPUT_ROOT.resolve()):
            continue
        try:
            record = json.loads(old.read_text(encoding='utf-8'))
            if not isinstance(record, dict) or 'segments' not in record or 'title' not in record:
                continue
            new = record_path(old.with_suffix('.txt'))
            atomic_json(new, record)
            old.unlink()
            moved[str(old.relative_to(PROJECT_ROOT))] = str(new.relative_to(PROJECT_ROOT))
        except (OSError, ValueError) as exc:
            log(f'内部记录迁移失败：{old.name}（{type(exc).__name__}）')
    return moved


def delete_outputs(pages, protected=()):
    protected = {Path(p).resolve() for p in protected}
    paths = set()
    for page in pages:
        if page.txt:
            txt = (PROJECT_ROOT / page.txt).resolve()
            if not txt.is_relative_to(OUTPUT_ROOT.resolve()) or txt.suffix.lower() != '.txt':
                raise ValueError('稿件路径不在文字稿目录内，未删除')
            if txt in protected:
                continue
            paths.update((txt, txt.with_suffix('.json'), record_path(txt)))
        if page.raw:
            raw = (PROJECT_ROOT / page.raw).resolve()
            if raw.suffix.lower() != '.json' or not (
                raw.is_relative_to(OUTPUT_ROOT.resolve()) or raw.is_relative_to(RECORDS_DIR.resolve())):
                raise ValueError('识别记录路径超出允许范围，未删除')
            if raw not in protected:
                paths.add(raw)
    # Validate every resolved target before unlinking any; never remove folders.
    for path in paths:
        if not (path.resolve().is_relative_to(OUTPUT_ROOT.resolve()) or
                path.resolve().is_relative_to(RECORDS_DIR.resolve())):
            raise ValueError('删除目标超出项目稿件目录')
    for path in paths:
        path.unlink(missing_ok=True)
