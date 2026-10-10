"""Recoverable layout migration; file bytes and filenames are preserved."""
import copy
import hashlib
import json
from pathlib import Path
import time
from ..config import atomic_json
from ..paths import OUTPUT_ROOT, PROJECT_ROOT, RECORDS_DIR, relative
from .documents import record_path
from .library import ORIGINAL, READING, original_path, reading_path
from . import rename


def _folder(state_path):
    key = hashlib.sha256(str(Path(state_path).resolve()).encode()).hexdigest()
    return PROJECT_ROOT / '.data' / 'library-migration' / key


def _validate(data, root):
    for entry in data['entries']:
        for key in ('old', 'new'):
            path = Path(entry[key]).resolve()
            if not (path.is_relative_to(root.resolve()) or path.is_relative_to(RECORDS_DIR.resolve())):
                raise ValueError('迁移恢复路径超出稿件目录')


def _finish_topics(root):
    if not root.exists():
        return
    for topic in root.iterdir():
        if not topic.is_dir() or topic.name in (ORIGINAL,READING):
            continue
        if not topic.resolve().is_relative_to(root):
            raise ValueError('旧主题路径超出稿件目录')
        for category in (ORIGINAL,READING):
            (root/category/topic.name).mkdir(parents=True,exist_ok=True)
        try:
            topic.rmdir()  # Empty folders only; unknown user files are preserved.
        except OSError:
            pass


def migrate_library(state_path, log=lambda message: None, root=None):
    root = Path(root or OUTPUT_ROOT).resolve()
    state_path = Path(state_path)
    folder = _folder(state_path)
    journal = folder / 'pending.json'
    if journal.exists():
        data = json.loads(journal.read_text(encoding='utf-8'))
        _validate(data, root)
        # A saved queue is the commit marker even if the final journal write failed.
        current = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else None
        committed = data.get('committed') or data['after_state'] is not None and current == data['after_state']
        if not committed:
            rename.rollback(data)
        journal.unlink()
        log('已恢复稿件目录迁移')
    old_files = [p for p in root.rglob('*.txt') if p.relative_to(root).parts[0] not in (ORIGINAL, READING)]
    try:
        state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else None
    except (OSError, ValueError):
        log('队列记录异常，目录迁移暂缓；现存稿件仍可阅读，队列由任务服务备份恢复')
        return
    if state is not None and not isinstance(state,dict):
        log('队列格式异常，目录迁移暂缓')
        return
    after_state = copy.deepcopy(state)
    entries, paths = [], {}
    for old in old_files:
        new = original_path(old, root)
        if new.exists():
            raise ValueError(f'迁移存在同名稿件，未覆盖：{new.name}')
        content = old.read_bytes()
        entries.append(rename._entry(old, new, content, content))
        old_raw, new_raw = record_path(old), record_path(new)
        if old_raw.exists():
            if new_raw.exists():
                raise ValueError('迁移目标已有内部记录，未覆盖')
            content = old_raw.read_bytes()
            entries.append(rename._entry(old_raw, new_raw, content, content))
        paths[relative(old)] = (relative(new), relative(new_raw), relative(reading_path(new, root)))
    if after_state is not None:
        for job in after_state.get('jobs', []):
            if 'saved' in job:
                migrated=[]
                for value in job['saved']:
                    legacy=Path(value)
                    if str(legacy) not in paths and root.name in legacy.parts:
                        legacy=Path(*legacy.parts[legacy.parts.index(root.name):])
                    migrated.append(paths.get(str(legacy),(value,))[0])
                job['saved']=migrated
            topic = job.get('topic') or '未分类'
            directory = root / ORIGINAL / topic
            if not directory.resolve().is_relative_to(root):
                raise ValueError('历史任务主题路径不合法')
            job['output_dir'] = relative(directory)
            for page in job.get('pages', []):
                if page.get('txt') in paths:
                    page['txt'], page['raw'], page['reading'] = paths[page['txt']]
                    page['raw'] = page['raw'] if (PROJECT_ROOT / page['raw']).exists() or any(
                        e['new'] == str(PROJECT_ROOT / page['raw']) for e in entries) else ''
                elif page.get('txt'):
                    path = PROJECT_ROOT / page['txt']
                    if path.resolve().is_relative_to(root):
                        page['txt'] = relative(original_path(path, root))
                        page['reading'] = relative(reading_path(path, root))
                page.setdefault('reading_status', '待整理')
    if not entries and after_state == state:
        _finish_topics(root)
        return
    data = dict(entries=entries, after_state=after_state)
    _validate(data, root)
    folder.mkdir(parents=True, exist_ok=True)
    # Durable backup is retained inside .data, never in either output folder.
    backup = folder / ('backup-' + str(time.time_ns()) + '.json')
    atomic_json(backup, dict(before_state=state, entries=entries))
    atomic_json(journal, data)
    try:
        rename.apply(data)
        if after_state is not None:
            atomic_json(state_path, after_state)
    except Exception:
        rename.rollback(data)
        journal.unlink()
        raise
    # Do not roll back after the queue has committed.
    data['committed'] = True
    atomic_json(journal, data)
    journal.unlink()
    for old in old_files:
        try:
            old.parent.rmdir()
        except OSError:
            pass
    _finish_topics(root)
    log(f'稿件目录迁移完成：{len(old_files)} 份，原始内容保持不变')
