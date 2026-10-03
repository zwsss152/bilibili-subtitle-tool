"""Faithful raw records plus a derived, readable Simplified Chinese transcript."""
import datetime
import json
import os
from pathlib import Path
import re
import threading
import unicodedata
from .bilibili import fmt_ts
from .models import PUNCTUATION_PATH
from ..config import atomic_json

_CONVERTER = None
_PUNCTUATOR = None
_LOCK = threading.Lock()
_PUNCT_MAP = str.maketrans({"﹐": "，", "﹑": "、", "﹒": "。", "．": "。", "｡": "。",
                          "､": "、", "﹔": "；", "﹕": "：", "﹖": "？", "﹗": "！"})


def to_simplified(text):
    global _CONVERTER
    if _CONVERTER is None:
        import opencc
        _CONVERTER = opencc.OpenCC("t2s")
    return _CONVERTER.convert(text)


def normalize_text(text):
    text = to_simplified(text).translate(_PUNCT_MAP)
    for old, new in ((",", "，"), ("?", "？"), ("!", "！"), (";", "；"), (":", "："), (".", "。")):
        text = re.sub(r"(?<=[\u4e00-\u9fff])" + re.escape(old), new, text)
    return text.strip()


def collapse_repeats(text):
    """Compatibility: preserve all words and numbers, including intentional repeats."""
    return text


def content_key(text):
    return "".join(ch for ch in text if not ch.isspace() and not unicodedata.category(ch).startswith("P"))


def add_punctuation(text, log=None, punctuator=None):
    global _PUNCTUATOR
    # Existing punctuation is retained instead of reinterpreting already readable speech.
    if len(re.findall(r"[，。！？；：,.!?;:]", text)) >= max(1, len(text) // 80):
        return text
    try:
        with _LOCK:
            if punctuator is None:
                if not PUNCTUATION_PATH.exists():
                    if log:
                        log("本地标点模型未就绪，暂时保留原文；可在设置中下载")
                    return text
                if _PUNCTUATOR is None:
                    import sherpa_onnx
                    config = sherpa_onnx.OfflinePunctuationConfig(
                        model=sherpa_onnx.OfflinePunctuationModelConfig(
                            ct_transformer=str(PUNCTUATION_PATH), num_threads=4, provider="cpu"))
                    _PUNCTUATOR = sherpa_onnx.OfflinePunctuation(config)
                punctuator = _PUNCTUATOR.add_punctuation
            result = punctuator(text)
        if content_key(result) != content_key(text):
            if log:
                log("标点模型改变了文字顺序，本段保留原文")
            return text
        return result
    except Exception as exc:
        if log:
            log(f"标点恢复不可用，保留原文（{type(exc).__name__}）")
        return text


def _join(left, right):
    if left and right and left[-1].isascii() and left[-1].isalnum() and right[0].isascii() and right[0].isalnum():
        return left + " " + right
    return left + right


def build_paragraphs(items, gap_seconds=2.0, max_chars=220, punctuate=True, log=None):
    paragraphs, buffer, start, previous_end = [], "", 0.0, None

    def flush():
        nonlocal buffer
        if buffer:
            value = add_punctuation(buffer, log) if punctuate else buffer
            # Never fabricate a finer timestamp: splits within a segment retain its start.
            paragraphs.append((start, value))
            buffer = ""

    for item in items:
        if isinstance(item, dict):
            frm, end, text = item["start"], item["end"], item["text"]
        else:
            frm, end, text = item
        text = normalize_text(text)
        if not text:
            continue
        pause = previous_end is not None and frm - previous_end > gap_seconds
        if buffer and (pause or len(buffer) + len(text) > max_chars * 1.5 or
                       (len(buffer) >= max_chars and re.search(r"[。！？!?]$", buffer))):
            flush()
        if not buffer:
            start = float(frm)
        buffer = _join(buffer, text)
        previous_end = float(end)
    flush()
    return paragraphs


def sanitize_filename(name):
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(name)).strip().strip(". ")[:80]
    if not value:
        value = "未命名"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", value, re.I):
        value = "_" + value
    return value


def save_result(directory, title, url, platform, method, segments, identity, metadata=None, log=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    base = sanitize_filename(title)[:64] + "_" + identity
    from .documents import record_path
    txt = directory / (base + ".txt")
    raw = record_path(txt)
    paragraphs = build_paragraphs(segments, log=log)
    if not paragraphs:
        raise RuntimeError("未生成文字，音频可能无人声")
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    record = dict(version=1, title=title, url=url, platform=platform, method=method,
                  generated_at=stamp, segments=segments, paragraphs=[dict(start=t, text=s) for t, s in paragraphs],
                  recognition=metadata or {})
    atomic_json(raw, record)
    content = [f"# {title}", f"来源：{url}", f"平台：{platform}    提取方式：{method}    生成时间：{stamp}", ""]
    for time, paragraph in paragraphs:
        content.extend([f"[{fmt_ts(time)}] {paragraph}", ""])
    temporary = txt.with_suffix(".txt.tmp")
    temporary.write_text("\n".join(content).rstrip() + "\n", encoding="utf-8")
    os.replace(temporary, txt)
    return str(txt), str(raw)


def read_document(path):
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    from .documents import record_path
    raw_path = record_path(path)
    if not raw_path.exists():
        raw_path = path.with_suffix('.json')
    record = json.loads(raw_path.read_text(encoding="utf-8")) if raw_path.exists() else None
    lines = text.splitlines()
    title = lines[0].lstrip("# ") if lines else path.stem
    url = next((line.removeprefix("来源：").strip() for line in lines if line.startswith("来源：")), "")
    return dict(path=str(path), title=title, url=url, text=text, raw=record)
