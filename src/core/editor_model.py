"""Pinned official local resources. No manuscript is sent to remote services."""
import json
import os
from pathlib import Path
import socket
import secrets
import subprocess
import threading
import time
import urllib.request
import zipfile
from ..paths import PROJECT_ROOT, MODELS_DIR
from ..config import atomic_json, read_json
from .models import download_file
from .net import check_cancel

MODEL_REVISION = '7c41481f57cb95916b40956ab2f0b139b296d974'
MODEL_NAME = 'Qwen3-8B-Q4_K_M.gguf'
MODEL_SHA = 'd98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785'
MODEL_SIZE = 5027783488
MODEL = MODELS_DIR / 'reading-editor' / MODEL_NAME
MODEL_PROFILES = {
    'qwen3-8b': dict(repository='Qwen/Qwen3-8B-GGUF', revision=MODEL_REVISION,
                   filename=MODEL_NAME, size=MODEL_SIZE, sha256=MODEL_SHA),
}
RUNTIME = PROJECT_ROOT / '.data' / 'runtime' / 'llama-b11146-cuda12.4'
SERVER = RUNTIME / 'llama-server.exe'
ASSETS = (
    ('llama-b11146-bin-win-cuda-12.4-x64.zip', 253869799,
     '3c806a6ceccc3dae1c743ceb1a1fb2cce5b76f40bfbd4c6b7b8afb6ef45a5807'),
    ('cudart-llama-bin-win-cuda-12.4-x64.zip', 391443627,
     '8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6'),
)


def selected_model():
    profile = read_json(PROJECT_ROOT / '.data' / 'reading-profile.json', {})
    key = profile.get('model_id', 'qwen3-8b') if isinstance(profile,dict) else 'qwen3-8b'
    return key if key in MODEL_PROFILES else 'qwen3-8b'


def resources_ready(model_id=None):
    spec = MODEL_PROFILES[model_id or selected_model()]
    path = MODELS_DIR / 'reading-editor' / spec['filename']
    return path.is_file() and path.stat().st_size == spec['size'] and SERVER.is_file()


def ensure_resources(progress=lambda *args: None, cancel=None, model_id=None):
    RUNTIME.mkdir(parents=True, exist_ok=True)
    for name, size, digest in ASSETS:
        marker = RUNTIME / (name + '.installed.json')
        if marker.exists() and SERVER.exists():
            continue
        archive = RUNTIME / name
        download_file('https://github.com/ggml-org/llama.cpp/releases/download/b11146/' + name,
                      archive, progress, cancel, size, digest)
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                check_cancel(cancel)
                if member.is_dir():
                    continue
                target = (RUNTIME / member.filename).resolve()
                if not target.is_relative_to(RUNTIME.resolve()):
                    raise ValueError('运行环境压缩包路径异常')
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + '.part')
                with bundle.open(member) as source, temporary.open('wb') as output:
                    while chunk := source.read(1024 * 1024):
                        check_cancel(cancel)
                        output.write(chunk)
                os.replace(temporary, target)
        atomic_json(marker, dict(version='b11146', sha256=digest))
        archive.unlink()
    spec = MODEL_PROFILES[model_id or selected_model()]
    model = MODELS_DIR / 'reading-editor' / spec['filename']
    if not model.exists() or model.stat().st_size != spec['size']:
        download_file(f'https://huggingface.co/{spec["repository"]}/resolve/{spec["revision"]}/{spec["filename"]}',
                      model, progress, cancel, spec['size'], spec['sha256'])
    atomic_json(model.with_suffix('.manifest.json'), spec)


class LocalEditor:
    engine = 'local'
    """Single-slot loopback server; its owner must first release the ASR GPU."""
    def __init__(self, model_id=None):
        self.model_id = model_id or selected_model()
        spec = MODEL_PROFILES[self.model_id]
        self.model_name, self.model_revision = spec['filename'], spec['revision']
        self.model_path = MODELS_DIR / 'reading-editor' / self.model_name
        self.process = None
        self.stream = None
        self.url = None
        self.token = None
        self.device = 'unknown'
        self._close_lock = threading.RLock()

    def start(self, cancel=None):
        if self.process and self.process.poll() is None:
            return
        if not resources_ready(self.model_id):
            raise RuntimeError('本地整理模型尚未安装完成')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{port}'
        self.token = secrets.token_urlsafe(32)
        log = RUNTIME / 'server.log'
        self.stream = log.open('w', encoding='utf-8')
        command = [str(SERVER), '-m', str(self.model_path), '--host', '127.0.0.1', '--port', str(port),
                   '-ngl', '99', '-c', '8192', '-np', '1', '-t', '8', '--jinja',
                   '--reasoning', 'off', '--api-key', self.token, '--no-webui', '--no-context-shift']
        self.process = subprocess.Popen(command, cwd=RUNTIME, stdout=self.stream, stderr=self.stream,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        deadline = time.monotonic() + 120
        try:
            while time.monotonic() < deadline:
                check_cancel(cancel)
                if self.process.poll() is not None:
                    raise RuntimeError('本地整理进程启动失败，请查看内部运行日志')
                try:
                    request = urllib.request.Request(self.url + '/health', headers={'Authorization':'Bearer '+self.token})
                    with urllib.request.urlopen(request, timeout=1) as response:
                        if response.status == 200:
                            try:
                                pids = subprocess.check_output(['nvidia-smi','--query-compute-apps=pid','--format=csv,noheader,nounits'],
                                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0), timeout=3).decode().split()
                                self.device = 'CUDA' if str(self.process.pid) in pids else 'CPU'
                            except (OSError, subprocess.SubprocessError):
                                self.device = 'unknown'
                            return
                except OSError:
                    pass
                time.sleep(.15)
            raise RuntimeError('本地整理模型加载超时')
        except BaseException:
            self.close()
            raise

    def generate(self, system, content, schema, cancel=None):
        check_cancel(cancel)
        task = ('请按书面化示例真正改写以下口语稿，不要照抄原句。保留全部实质信息，直接输出规定的JSON。\n'
                if 'units' in schema.get('properties', {}) else
                '请逐项对照原文与候选，核查每个实质信息点是否完整保留，直接输出规定的JSON。\n')
        array = schema.get('properties', {}).get('units', {})
        written_policy = ('information_points' in array.get('items', {}).get('properties', {})
                          or '完整书面稿的信息核对员' in system)
        payload = dict(messages=[dict(role='system', content=system), dict(role='user', content=task + content if written_policy else content)],
            temperature=.2, top_p=.8, top_k=20, min_p=0, seed=42, max_tokens=4096,
            stream=False, response_format=dict(type='json_schema', json_schema=dict(name='reading', schema=schema)),
            chat_template_kwargs=dict(enable_thinking=False))
        request = urllib.request.Request(self.url + '/v1/chat/completions',
                data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                headers={'Content-Type':'application/json', 'Authorization':'Bearer '+self.token})
        done = threading.Event()
        def watch():
            while not done.wait(.1):
                if cancel and cancel():
                    self.close()
                    return
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.load(response)
        except Exception:
            check_cancel(cancel)
            raise
        finally:
            done.set()
            watcher.join(timeout=.5)
        check_cancel(cancel)
        if result['choices'][0].get('finish_reason') != 'stop':
            raise RuntimeError('整理结果截断，已使用原文')
        return json.loads(result['choices'][0]['message']['content'])

    def close(self):
        with self._close_lock:
            process, self.process = self.process, None
            if process:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
            if self.stream:
                self.stream.close()
                self.stream = None
