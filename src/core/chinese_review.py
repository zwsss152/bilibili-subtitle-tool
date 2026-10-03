"""Independently decode Chinese speech; keep both readings and original timings."""
from difflib import SequenceMatcher
from functools import lru_cache
import re
import unicodedata
from .models import SENSE_VOICE_DIR, ensure_sense_voice
from .net import JobCancelled, check_cancel
from .textout import content_key, to_simplified

_ENGINE = None


@lru_cache(maxsize=4096)
def _pronunciations(char):
    from pypinyin import pinyin, Style
    return set(pinyin(char, style=Style.NORMAL, heteronym=True)[0])


def _same_sound(first, second):
    return (re.fullmatch(r'[\u4e00-\u9fff]', first) and re.fullmatch(r'[\u4e00-\u9fff]', second)
            and bool(_pronunciations(first).intersection(_pronunciations(second))))


def _homophone_pairs(old, new):
    """Align a small differing span, ignoring extra filler in the second reading."""
    scores = [[0] * (len(new) + 1) for _ in range(len(old) + 1)]
    moves = {}
    for i in range(len(old) + 1):
        scores[i][0] = i
    for j in range(len(new) + 1):
        scores[0][j] = j
    for i, first in enumerate(old, 1):
        for j, second in enumerate(new, 1):
            cost = 0 if first == second else 0.2 if _same_sound(first, second) else 2
            scores[i][j], moves[i, j] = min((scores[i-1][j-1] + cost, 'pair'),
                                          (scores[i-1][j] + 1, 'delete'), (scores[i][j-1] + 1, 'insert'))
    i, j, pairs = len(old), len(new), []
    while i and j:
        move = moves[i, j]
        if move == 'pair':
            if old[i-1] != new[j-1] and _same_sound(old[i-1], new[j-1]):
                pairs.append((i-1, new[j-1]))
            i, j = i-1, j-1
        elif move == 'delete':
            i -= 1
        else:
            j -= 1
    return pairs


def engine(progress=lambda *a: None, cancel=None):
    global _ENGINE
    if _ENGINE is None:
        ensure_sense_voice(progress, cancel)
        check_cancel(cancel)
        import sherpa_onnx
        _ENGINE = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(SENSE_VOICE_DIR / 'model.int8.onnx'),
            tokens=str(SENSE_VOICE_DIR / 'tokens.txt'), language='zh',
            num_threads=8, use_itn=True, provider='cpu')
    return _ENGINE


def choose_reading(original, candidate):
    """Reject empty, truncated, unrelated or numerically conflicting second readings."""
    a, b = content_key(to_simplified(original)), content_key(to_simplified(candidate))
    if not b or len(re.findall(r'[\u4e00-\u9fff]', a)) < 4:
        return original, '保留原识别：无有效中文复核'
    if not 0.7 <= len(b) / max(1, len(a)) <= 1.4:
        return original, '保留原识别：复核长度异常'
    if re.findall(r'\d+(?:[.,]\d+)*', original) != re.findall(r'\d+(?:[.,]\d+)*', candidate):
        return original, '保留原识别：数字结果不一致'
    alignment = SequenceMatcher(None, a, b, autojunk=False)
    if alignment.ratio() < 0.7:
        return original, '保留原识别：两次识别差异过大'
    # The second model is evidence for small homophone substitutions, not
    # permission to rewrite the first transcript, add filler or drop sentences.
    text = to_simplified(original)
    indices = [i for i, char in enumerate(text)
               if not char.isspace() and not unicodedata.category(char).startswith('P')]
    result, changed = list(text), False
    for operation, i, j, k, l in alignment.get_opcodes():
        old, new = a[i:j], b[k:l]
        if operation != 'replace' or not 1 <= len(old) <= 8 or not 1 <= len(new) <= 16:
            continue
        for offset, char in _homophone_pairs(old, new):
            result[indices[i + offset]] = char
            changed = True
    return (''.join(result), '采用中文声学复核的同音字修正') if changed else (original, '保留原识别：未发现可信的同音字修正')


def review(audio_path, segments, progress=lambda *a: None, cancel=None, log=lambda *a: None):
    from faster_whisper.audio import decode_audio
    try:
        recognizer = engine(lambda message, pct=None: log(message), cancel)
        audio = decode_audio(str(audio_path), sampling_rate=16000)
    except JobCancelled:
        raise
    except Exception as exc:
        log(f'中文复核不可用，保留语音识别结果（{type(exc).__name__}: {exc}）')
        return segments, dict(engine='sense-voice-int8', available=False, error=str(exc))
    output, changed = [], 0
    for index, segment in enumerate(segments):
        check_cancel(cancel)
        item = dict(segment)
        start = max(0, round(segment['start'] * 16000))
        end = min(len(audio), round(segment['end'] * 16000))
        # Whisper normally returns windows shorter than 30 s. Do not cut a long
        # exceptional segment at an arbitrary word boundary just to review it.
        if 0 < end - start <= 60 * 16000:
            try:
                stream = recognizer.create_stream()
                stream.accept_waveform(16000, audio[start:end])
                recognizer.decode_stream(stream)
                candidate = stream.result.text
                selected, reason = choose_reading(segment['text'], candidate)
                item.update(text=selected, whisper_text=segment['text'],
                            sense_voice_text=candidate, review_reason=reason)
                changed += selected != segment['text']
            except Exception as exc:
                item['review_reason'] = f'保留原识别：中文复核失败（{type(exc).__name__}）'
        output.append(item)
        progress(80 + int(19 * (index + 1) / max(1, len(segments))), '中文语音复核')
    check_cancel(cancel)
    log(f'中文语音复核完成：{changed} / {len(segments)} 条采用复核结果')
    return output, dict(engine='sense-voice-int8', strategy='homophone-only', available=True,
                        reviewed=len(segments), changed=changed)
