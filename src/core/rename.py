"""Recoverable, same-folder transcript renames; queue paths are the commit marker."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
from ..config import atomic_json
from ..paths import PROJECT_ROOT, OUTPUT_ROOT, RECORDS_DIR
from .documents import record_path
from .library import reading_path


def validate_name(value):
    name = str(value).strip()
    if name.lower().endswith('.txt'):
        name = name[:-4]
    if not name or name in ('.', '..') or name.endswith(('.', ' ')):
        raise ValueError('请输入有效的稿件名称，不能以句点或空格结尾')
    if len(name) > 100 or re.search(r'[\\/:*?"<>|\x00-\x1f]', name):
        raise ValueError('名称不能超过 100 个字，或包含 \\ / : * ? " < > | 等字符')
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', name, re.I):
        raise ValueError('这个名称是 Windows 保留名称，请换一个名称')
    return name


def _key(path):
    return str(Path(path).resolve()).casefold()


def _owned(path, record=False):
    requested = Path(path)
    path = requested.resolve()
    roots = (OUTPUT_ROOT, RECORDS_DIR) if record else (OUTPUT_ROOT,)
    if not any(path.is_relative_to(root.resolve()) for root in roots):
        raise ValueError('稿件或识别记录路径超出项目目录')
    if path.suffix.lower() != ('.json' if record else '.txt'):
        raise ValueError('稿件或识别记录扩展名不正确')
    # Windows resolve returns the existing spelling; retain a requested case change.
    return requested.parent.resolve() / requested.name


def _folder(state_path):
    requested = Path(state_path)
    # Windows can report a package-cache name for an ordinary AppData file
    # handle. Check its canonical parent, retaining the ordinary file name;
    # actual symlinks still require validation against their final target.
    state_path = requested.resolve() if requested.is_symlink() else requested.parent.resolve() / requested.name
    if not state_path.is_relative_to(PROJECT_ROOT.resolve()):
        raise ValueError('队列路径超出项目目录')
    key = hashlib.sha256(str(state_path).casefold().encode()).hexdigest()
    return PROJECT_ROOT / '.data' / 'renames' / key


def _bytes(value):
    return base64.b64decode(value, validate=True)


def _entry(old, new, before, after):
    return dict(old=str(old), new=str(new), before=base64.b64encode(before).decode(),
                after=base64.b64encode(after).decode())


def prepare(job, name, state_path):
    name = validate_name(name)
    entries, pages = [], []
    for page in job.pages:
        old = _owned(PROJECT_ROOT / page.txt)
        title = name if len(job.pages) == 1 else f'{name}_P{page.number}'
        new = _owned(old.with_name(title + '.txt'))
        if len(str(new)) >= 250:
            raise ValueError('文件路径过长，请缩短稿件名称')
        raw_old = record_path(old)
        if not raw_old.exists() and page.raw:
            raw_old = _owned(PROJECT_ROOT / page.raw, record=True)
        if not raw_old.exists():
            raw_old = old.with_suffix('.json')
        raw_new = record_path(new)
        before = old.read_bytes()
        # Change only the first line. Preserve the body and its line endings.
        first, separator, body = before.decode('utf-8-sig').partition('\n')
        ending = '\r\n' if first.endswith('\r') else '\n'
        after = ('# ' + title + (ending + body if separator else '')).encode('utf-8')
        entries.append(_entry(old, new, before, after))
        read_old = _owned(PROJECT_ROOT / page.reading) if getattr(page, 'reading', '') else reading_path(old, OUTPUT_ROOT)
        read_new = _owned(read_old.with_name(title + '.txt'))
        if read_new.exists() and _key(read_new) != _key(read_old):
            raise ValueError(f'已存在同名阅读稿：{read_new.name}')
        if read_old.exists():
            read_old = _owned(read_old)
            read_before = read_old.read_bytes()
            first, separator, body = read_before.decode('utf-8-sig').partition('\n')
            ending = '\r\n' if first.endswith('\r') else '\n'
            read_after = ('# ' + title + (ending + body if separator else '')).encode('utf-8')
            entries.append(_entry(read_old, read_new, read_before, read_after))
        raw = ''
        if raw_old.exists():
            raw_old = _owned(raw_old, record=True)
            original = raw_old.read_bytes()
            record = json.loads(original.decode('utf-8-sig'))
            record['title'] = title
            if record.get('reading'):
                record['reading']['path'] = str(read_new.relative_to(PROJECT_ROOT))
                if read_old.exists():
                    record['reading']['file_hash'] = hashlib.sha256(read_after).hexdigest()
                    if record['reading'].get('pending_file_hash'):
                        record['reading']['pending_file_hash'] = hashlib.sha256(read_after).hexdigest()
            entries.append(_entry(raw_old, raw_new, original,
                                  json.dumps(record, ensure_ascii=False, indent=2).encode('utf-8')))
            raw = str(raw_new.relative_to(PROJECT_ROOT))
        elif raw_new.exists():
            raise ValueError('这个名称已有识别记录，请换一个名称')
        pages.append(dict(number=page.number, title=title, txt=str(new.relative_to(PROJECT_ROOT)), raw=raw,
                          reading=str(read_new.relative_to(PROJECT_ROOT))))
    targets, originals = set(), set()
    for entry in entries:
        target = Path(entry['new'])
        if _key(target) in targets or _key(entry['old']) in originals:
            raise ValueError('任务中存在重复稿件路径，不能重命名')
        targets.add(_key(target))
        originals.add(_key(entry['old']))
        if target.exists() and _key(target) != _key(entry['old']):
            raise ValueError(f'已存在同名文件：{target.name}，请换一个名称')
    journal = _folder(state_path) / (uuid.uuid4().hex + '.json')
    data = dict(jid=job.jid, title=name, pages=pages, entries=entries)
    atomic_json(journal, data)
    return journal, data


def _write(path, content, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = PROJECT_ROOT / '.data' / 'rename-staging'
    staging.mkdir(parents=True, exist_ok=True)
    stage = staging / (uuid.uuid4().hex + '.tmp')
    try:
        with stage.open('xb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows rename refuses to replace an existing destination.
        if exclusive:
            os.rename(stage, path)
        else:
            os.replace(stage, path)
    finally:
        stage.unlink(missing_ok=True)


def apply(data):
    for entry in data['entries']:
        entry['installed'] = False
    for entry in data['entries']:
        old, new = Path(entry['old']), Path(entry['new'])
        if old.read_bytes() != _bytes(entry['before']):
            raise RuntimeError('稿件在重命名期间被修改，请重新尝试')
        same = _key(old) == _key(new)
        if same and str(old) != str(new):
            os.rename(old, new)
            entry['installed'] = True
        _write(new, _bytes(entry['after']), exclusive=not same)
        entry['installed'] = True
        if not same:
            old.unlink()


def rollback(data):
    for entry in reversed(data['entries']):
        if entry.get('installed') is False:
            continue
        old, new = Path(entry['old']), Path(entry['new'])
        before, after = _bytes(entry['before']), _bytes(entry['after'])
        same = _key(old) == _key(new)
        if ((old.exists() and old.read_bytes() not in (before, after)) or
                (new.exists() and new.read_bytes() not in (before, after))):
            raise RuntimeError('稿件已被外部修改，重命名恢复记录已保留')
        if same and str(old) != str(new) and new.exists():
            os.rename(new, old)
        if not old.exists() or old.read_bytes() != before or (same and str(old) != str(new)):
            _write(old, before, exclusive=not old.exists())
        if not same and new.exists():
            new.unlink()


def recover(state_path, log=lambda message: None):
    folder = _folder(state_path)
    if not folder.exists():
        return
    for journal in folder.glob('*.json'):
        data = json.loads(journal.read_text(encoding='utf-8'))
        for entry in data['entries']:
            record = Path(entry['old']).suffix.lower() == '.json'
            old, new = _owned(entry['old'], record), _owned(entry['new'], record)
            if not record and old.parent != new.parent:
                raise ValueError('重命名恢复路径不在原主题文件夹')
        # Read strictly: a corrupt queue must never be mistaken for an uncommitted rename.
        stored = json.loads(Path(state_path).read_text(encoding='utf-8'))
        job = next((j for j in stored.get('jobs', []) if j['jid'] == data['jid']), None)
        committed = data.get('committed') or job and job['title'] == data['title'] and all(
            any(all(page.get(k) == value for k, value in wanted.items()) for page in job['pages'])
            for wanted in data['pages'])
        if not committed:
            rollback(data)
        journal.unlink()
        log('已恢复未完成的稿件重命名' if not committed else '已完成稿件重命名的清理')
