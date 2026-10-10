"""Bailian HTTP client, Windows-encrypted credentials and resumable audio chunks.

No model SDK or cloud storage is required. Cookies and source URLs are never
sent to these endpoints. API errors deliberately omit response bodies/secrets.
"""
import base64
import ctypes
from ctypes import wintypes
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave

from ..config import atomic_json, read_json
from ..paths import PROJECT_ROOT
from .download import find_ffmpeg, probe_duration
from .net import JobCancelled, check_cancel

DATA = PROJECT_ROOT / '.data'
SETTINGS = DATA / 'cloud-settings.json'
SECRET = DATA / 'cloud-key.dpapi'
ASR_MODEL = 'qwen-audio-3.0-asr-flash'
EDITOR_MODEL = 'qwen3.5-plus'
REVIEW_MODEL = 'qwen3-max'
VERSION = 3
_USAGE_LOCK = threading.Lock()


class CloudError(RuntimeError):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def _error_message(code='', status=0):
    value = str(code).lower()
    if any(word in value for word in ('arrearage','insufficientbalance','quotaexceeded')) or status == 402:
        return '在线服务额度或余额不足，任务已保留，请检查百炼账户'
    if any(word in value for word in ('invalidapikey','unauthorized','accessdenied')) or status in (401,403):
        return 'API 鉴权失败，请检查密钥权限与北京地域'
    if status == 429 or 'throttl' in value:
        return '在线请求被限流，已完成分块已保留，稍后可重试'
    return f'在线请求失败（HTTP {status}），任务已保留' if status else '在线服务返回错误，任务已保留；请检查账户权限与额度'


class _Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(value, decrypt=False):
    if os.name != 'nt':
        raise CloudError('API 密钥加密需要 Windows')
    buffer = ctypes.create_string_buffer(value)
    incoming = _Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = _Blob()
    function = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise CloudError('无法保存或解密 API 密钥，请在设置中重新填写')
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(ctypes.cast(outgoing.data, ctypes.c_void_p))


def save_credentials(key='', workspace=''):
    workspace = workspace.strip()
    if workspace and not re.fullmatch(r'[A-Za-z0-9-]{1,100}', workspace):
        raise ValueError('业务空间 ID 只能包含字母、数字和短横线')
    if key.strip():
        encrypted = _crypt(key.strip().encode('utf-8'))
        SECRET.parent.mkdir(parents=True, exist_ok=True)
        temp = SECRET.with_suffix('.tmp')
        temp.write_bytes(encrypted)
        os.replace(temp, SECRET)
    atomic_json(SETTINGS, dict(workspace=workspace, region='beijing'))


def configured():
    return SECRET.is_file() or bool(os.environ.get('DASHSCOPE_API_KEY', '').strip())


def key_value():
    if SECRET.is_file():
        return _crypt(SECRET.read_bytes(), decrypt=True).decode('utf-8')
    key = os.environ.get('DASHSCOPE_API_KEY', '').strip()
    if not key:
        raise CloudError('在线方式尚未配置，请在设置中填写阿里云百炼 API Key')
    return key


def history_enabled():
    profile = read_json(DATA / 'cloud-profile.json', {})
    return configured() and profile.get('version') == VERSION and profile.get('validated') is True


def checkpoint_path(jid, page):
    identity = hashlib.sha256(str(jid).encode('utf-8')).hexdigest()
    return DATA / 'cloud-asr' / f'{identity}_p{int(page)}.json'


def discard_checkpoints(jid):
    root = DATA / 'cloud-asr'
    identity = hashlib.sha256(str(jid).encode('utf-8')).hexdigest()
    for path in root.glob(identity + '_p*.json'):
        if path.resolve().parent != root.resolve():
            raise ValueError('拒绝清理项目外的识别记录')
        path.unlink()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise CloudError('在线接口返回重定向，已停止以保护密钥')


class CloudClient:
    engine = 'cloud'
    model_name = EDITOR_MODEL
    model_revision = 'bailian-http-v1'
    device = 'cloud'
    process = None

    def __init__(self, transport=None):
        self.transport = transport
        self.response = None

    def start(self, cancel=None):
        check_cancel(cancel)
        if self.transport is None:
            key_value()

    def close(self):
        response, self.response = self.response, None
        if response is not None:
            response.close()

    def _base(self):
        workspace = read_json(SETTINGS, {}).get('workspace', '')
        return 'https://' + (workspace + '.cn-beijing.maas.aliyuncs.com' if workspace else 'dashscope.aliyuncs.com')

    def _request(self, endpoint, payload, cancel=None, streaming=False):
        check_cancel(cancel)
        if self.transport is not None:
            return self.transport(endpoint, payload, streaming)
        headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key_value()}
        if endpoint.startswith('/api/v1/'):
            headers['X-DashScope-SSE'] = 'enable' if streaming else 'disable'
        request = urllib.request.Request(self._base() + endpoint,
            json.dumps(payload, ensure_ascii=False).encode('utf-8'), headers)
        for attempt in range(3):
            check_cancel(cancel)
            try:
                response = urllib.request.build_opener(_NoRedirect()).open(request, timeout=60)
                self.response = response
                with response:
                    if 'text/event-stream' in response.headers.get('Content-Type', ''):
                        results, lines = [], []
                        for raw in response:
                            check_cancel(cancel)
                            line = raw.decode('utf-8').rstrip('\r\n')
                            if line.startswith('data:'):
                                lines.append(line[5:].lstrip())
                            elif not line and lines:
                                body = '\n'.join(lines)
                                if body != '[DONE]':
                                    results.append(json.loads(body))
                                lines = []
                        if lines and '\n'.join(lines) != '[DONE]':
                            results.append(json.loads('\n'.join(lines)))
                    else:
                        results = [json.load(response)]
                check_cancel(cancel)
                if not results:
                    raise CloudError('在线响应为空，未使用不完整结果')
                for item in results:
                    if item.get('code') or item.get('error'):
                        error_value = item.get('error')
                        code = item.get('code') or (error_value.get('code','') if isinstance(error_value,dict) else '')
                        raise CloudError(_error_message(code))
                return results
            except urllib.error.HTTPError as exc:
                retryable = exc.code == 429 or exc.code >= 500
                try:
                    detail = json.loads(exc.read(65536))
                    code = detail.get('code') or (detail.get('error') or {}).get('code','')
                except (ValueError,AttributeError,OSError):
                    code = ''
                finally:
                    exc.close()
                error = CloudError(_error_message(code, exc.code), retryable)
            except (OSError, urllib.error.URLError):
                error = CloudError('在线连接中断或超时，已完成分块已保留', True)
            except (ValueError, UnicodeError):
                raise CloudError('在线响应格式异常，未使用不完整结果') from None
            finally:
                self.close()
            check_cancel(cancel)
            if not error.retryable or attempt == 2:
                raise error from None
            end = time.monotonic() + 2 ** attempt
            while time.monotonic() < end:
                check_cancel(cancel)
                time.sleep(.05)

    def _usage(self, model, results):
        # Some SSE events repeat cumulative usage. Keep the final total only.
        usage = next((item['usage'] for item in reversed(results) if item.get('usage')), {})
        item = dict(time=time.time(), model=model, usage=usage,
                    request_id=results[-1].get('request_id', results[-1].get('id', '')))
        if self.transport is None:
            with _USAGE_LOCK:
                DATA.mkdir(exist_ok=True)
                path = DATA / 'cloud-usage.jsonl'
                if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
                    backup = path.with_suffix('.jsonl.1')
                    os.replace(path, backup)
                with path.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(item, ensure_ascii=False) + '\n')
        return item

    def generate(self, system, content, schema, cancel=None):
        audit = 'checks' in schema.get('properties', {})
        model = REVIEW_MODEL if audit else EDITOR_MODEL
        # JSON mode guarantees syntax, not this application's object fields.
        # Supply the schema explicitly; do not assume the model knows it.
        system += '\n严格按以下 JSON Schema 返回一个 JSON 对象：\n' + json.dumps(schema, ensure_ascii=False)
        payload = dict(model=model, messages=[dict(role='system', content=system), dict(role='user', content=content)],
                       temperature=.2, max_tokens=8192, enable_thinking=False, stream=False,
                       response_format={'type': 'json_object'})
        results = self._request('/compatible-mode/v1/chat/completions', payload, cancel)
        self._usage(model, results)
        try:
            choice = results[-1]['choices'][0]
            if choice.get('finish_reason') != 'stop':
                raise CloudError('整理输出截断，未使用不完整结果')
            value = json.loads(choice['message']['content'])
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (KeyError, IndexError, ValueError, TypeError):
            raise CloudError('整理响应不完整，原稿已保留') from None

    def recognize(self, wav, context='', cancel=None):
        encoded = base64.b64encode(wav).decode('ascii')
        if len(encoded) > 9 * 1024 * 1024:
            raise ValueError('音频分块超出在线直传限制')
        messages = []
        if context:
            messages.append(dict(role='user', content=[dict(type='input_text', text=context[:400])]))
        messages.append(dict(role='user', content=[dict(type='input_audio', input_audio=dict(data='data:audio/wav;base64,' + encoded))]))
        results = self._request('/api/v1/services/aigc/multimodal-generation/generation',
            dict(model=ASR_MODEL, input=dict(messages=messages), parameters=dict(format='wav', sample_rate='16000')),
            cancel, streaming=False)
        usage = self._usage(ASR_MODEL, results)
        sentences, words, full = {}, {}, ''
        final = results[-1].get('output', {}).get('sentence', {})
        if final.get('sentence_end') is False:
            raise CloudError('在线识别流提前结束，未使用中间结果')
        for event in results:
            output = event.get('output', {})
            full = output.get('text') or full
            for sentence in output.get('sentences', []) or [output.get('sentence', {})]:
                if not sentence:
                    continue
                if sentence.get('sentence_end') is True:
                    sentences[sentence.get('sentence_id', len(sentences))] = sentence
                for word in sentence.get('words', []):
                    if word.get('fixed') is not False:
                        words[(word['begin_time'], word['end_time'])] = word
        def letters(text):
            return re.sub(r'[\s，。！？、；：,.!?;:]+', '', text)
        fine = sorted(words.values(), key=lambda word: (word['begin_time'], word['end_time']))
        covered = ''.join(word['text'] + word.get('punctuation', '') for word in fine)
        segments = [dict(start=word['begin_time']/1000, end=word['end_time']/1000,
                         text=word['text'] + word.get('punctuation', '')) for word in fine]
        if not full.strip():
            full = ''.join(s.get('text', '') for s in sorted(sentences.values(), key=lambda s:s['begin_time']))
        if not full.strip():
            raise CloudError('在线识别未产生有效文字，未判定为成功')
        # A final-only response may cover just its last sentence. Never discard
        # its accumulated text or invent timestamps for the missing sentences.
        precise = bool(segments) and letters(covered) == letters(full)
        if precise:
            positions = [i for i,char in enumerate(full) if letters(char)]
            cursor, previous = 0, -1
            for segment in segments:
                width = len(letters(segment['text']))
                if width:
                    first, last = positions[cursor], positions[cursor+width-1]
                    gap = full[previous+1:first]
                    segment['joiner'] = ' ' if any(char.isspace() for char in gap) else ''
                    previous = last
                    cursor += width
                else:
                    segment['joiner'] = ''
        return dict(text=full, segments=segments if precise else [], precise=precise, usage=usage)


def _run(command, cancel=None):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        while True:
            check_cancel(cancel)
            try:
                output, error = process.communicate(timeout=.1)
                break
            except subprocess.TimeoutExpired:
                pass
        if process.returncode:
            raise RuntimeError('音频分块转换失败，请检查源音频')
        return output
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def _own_words(segments, start, core_start, core_end, previous_edge):
    """Use actual word times, matching shared words before assigning a boundary.

    Two decodes can put the same word's midpoint on opposite sides. Ownership
    follows the preceding decode in that case, preventing a dropped/duplicate
    word without inventing new text or finer timestamps.
    """
    from .textout import content_key
    owned, edge = [], []
    for segment in segments:
        item = dict(segment, start=start+segment['start'],end=start+segment['end'])
        midpoint = (item['start']+item['end'])/2
        belongs = core_start <= midpoint < core_end
        if core_start-2 <= midpoint <= core_start+2:
            pairs = [old for old in previous_edge
                     if content_key(old['text']) == content_key(item['text'])
                     and min(old['end'],item['end']) > max(old['start'],item['start'])]
            if len(pairs) == 1:
                belongs = not pairs[0]['owned']
        if core_end-2 <= midpoint <= core_end+2:
            edge.append(dict(item,owned=belongs))
        if belongs:
            owned.append(item)
    return owned, edge


def _check_duration(result, expected):
    # usage.duration is a usage metric, not a completion cursor. In the
    # reproduced 124s game clip both JSON and SSE returned 95, exactly the last
    # utterance's end. A lower value cannot establish that audio was truncated.
    duration = result.get('usage', {}).get('usage', {}).get('duration')
    if duration is not None and (type(duration) not in (int, float)
            or not math.isfinite(duration) or not 0 <= duration <= math.ceil(expected)+1):
        raise CloudError('在线服务返回异常用量时长，本分块未保存为成功')


def _audio_chunk(audio_path, start, duration, ffmpeg, cancel=None):
    # A WAV written directly to ffmpeg's stdout has unknown RIFF/data sizes.
    # Decode PCM first, check its actual length, then write a sized WAV header.
    pcm = _run([ffmpeg,'-v','error','-ss',str(start),'-i',str(audio_path),'-t',str(duration),
                '-ar','16000','-ac','1','-f','s16le','pipe:1'], cancel)
    actual = len(pcm)/32000
    if len(pcm) % 2 or not pcm or abs(actual-duration) > .1:
        raise CloudError('音频分块解码不完整，未提交在线识别')
    buffer = io.BytesIO()
    with wave.open(buffer,'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(pcm)
    return buffer.getvalue()


def transcribe(audio_path, checkpoint, context='', progress=lambda *a:None, cancel=None, client=None):
    client = client or CloudClient()
    client.start(cancel)
    audio_path, checkpoint = Path(audio_path), Path(checkpoint)
    with audio_path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    source = hashlib.sha256((digest + ASR_MODEL + str(VERSION) + context[:400]).encode()).hexdigest()
    state = read_json(checkpoint, {})
    if state.get('source') != source:
        state = dict(version=VERSION, source=source, blocks=[])
    duration = probe_duration(audio_path)
    if not duration:
        raise RuntimeError('无法确定音频时长，未提交在线请求')
    count = math.ceil(duration / 120)
    if [b.get('id') for b in state['blocks']] != list(range(len(state['blocks']))) or len(state['blocks']) > count:
        raise CloudError('识别续作记录损坏，请保留记录并检查')
    ffmpeg = find_ffmpeg()
    started = time.perf_counter()
    for index in range(len(state['blocks']), count):
        check_cancel(cancel)
        core_start, core_end = index * 120, min(duration, (index+1)*120)
        start, end = max(0,core_start-2), min(duration,core_end+2)
        wav = _audio_chunk(audio_path,start,end-start,ffmpeg,cancel)
        result = client.recognize(wav, context, cancel)
        from .chinese_review import damaged
        for attempt in range(1):
            if damaged(result['text']):
                result = client.recognize(wav, context, cancel)
        if damaged(result['text']):
            raise CloudError('在线识别仍含缺损字符，本分块未保存为成功')
        _check_duration(result, end-start)
        if result['precise']:
            for segment in result['segments']:
                if not (math.isfinite(segment['start']) and math.isfinite(segment['end'])
                        and 0 <= segment['start'] <= segment['end'] <= end-start+.5):
                    raise CloudError('在线时间戳超出真实音频范围，本分块未保存为成功')
            previous_edge = state['blocks'][-1].get('edge',[]) if state['blocks'] else []
            segments, edge = _own_words(result['segments'],start,core_start,core_end,previous_edge)
        else:
            # Retry without overlap so coarse ranges cannot duplicate adjacent
            # text. The original time range is explicitly stored in the record.
            if start != core_start or end != core_end:
                wav = _audio_chunk(audio_path,core_start,core_end-core_start,ffmpeg,cancel)
                result = client.recognize(wav, context, cancel)
                if damaged(result['text']):
                    raise CloudError('在线识别仍含异常字符，本分块未保存为成功')
                _check_duration(result, core_end-core_start)
            segments = [dict(start=core_start, end=core_end, text=result['text'], precision='chunk')]
            edge = []
        if not segments:
            raise CloudError('识别分块缺少完整时间范围，未保存为成功')
        check_cancel(cancel)
        state['blocks'].append(dict(id=index, start=core_start, end=core_end, segments=segments, edge=edge,usage=result['usage']))
        atomic_json(checkpoint, state)
        progress(int((index+1)*99/count), f'在线识别 {index+1}/{count}')
    return dict(segments=[s for block in state['blocks'] for s in block['segments']],
        recognition=dict(engine='cloud', model=ASR_MODEL, device='cloud', duration=duration,
                         source_hash=source, version=VERSION, elapsed_seconds=time.perf_counter()-started))
