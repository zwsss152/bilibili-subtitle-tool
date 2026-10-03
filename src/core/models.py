"""Explicit model downloads: resumable, cancellable and manifest driven."""
import hashlib
import json
import os
from pathlib import Path
import threading
import urllib.request
from .net import check_cancel
from ..paths import MODELS_DIR

REPOSITORIES = {"tiny": "Systran/faster-whisper-tiny", "small": "Systran/faster-whisper-small",
                "medium": "Systran/faster-whisper-medium", "large-v3": "Systran/faster-whisper-large-v3",
                "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo"}
PUNCTUATION_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/punctuation-models/sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8.tar.bz2"
PUNCTUATION_PATH = MODELS_DIR / "punctuation" / "model.int8.onnx"
SENSE_VOICE_DIR = MODELS_DIR / "sense-voice"
SENSE_VOICE_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2"
_DOWNLOAD_LOCK = threading.Lock()


def sense_voice_is_ready():
    model, tokens = SENSE_VOICE_DIR / 'model.int8.onnx', SENSE_VOICE_DIR / 'tokens.txt'
    return model.is_file() and model.stat().st_size > 1_000_000 and tokens.is_file() and tokens.stat().st_size > 1000


def ensure_sense_voice(progress=lambda *args: None, cancel=None):
    if sense_voice_is_ready():
        return SENSE_VOICE_DIR
    import tarfile
    with _DOWNLOAD_LOCK:
        if sense_voice_is_ready():
            return SENSE_VOICE_DIR
        archive = SENSE_VOICE_DIR / 'download.tar.bz2'
        download_file(SENSE_VOICE_URL, archive, progress, cancel)
        with tarfile.open(archive) as bundle:
            for filename in ('model.int8.onnx', 'tokens.txt'):
                member = next(m for m in bundle.getmembers() if m.isfile() and Path(m.name).name == filename)
                temporary = SENSE_VOICE_DIR / (filename + '.part')
                count = 0
                with bundle.extractfile(member) as source, temporary.open('wb') as dest:
                    while chunk := source.read(1024 * 1024):
                        check_cancel(cancel)
                        count += dest.write(chunk)
                if count != member.size:
                    raise RuntimeError('中文识别模型解压不完整')
                os.replace(temporary, SENSE_VOICE_DIR / filename)
        archive.unlink()
    return SENSE_VOICE_DIR


def model_path(name):
    return MODELS_DIR / f"faster-whisper-{name}"


def model_is_ready(name):
    directory = model_path(name)
    if not (directory / "model.bin").is_file() or (directory / "model.bin").stat().st_size < 1_000_000:
        return False
    try:
        json.loads((directory / "config.json").read_text(encoding="utf-8"))
        json.loads((directory / "tokenizer.json").read_text(encoding="utf-8"))
        return any((directory / filename).is_file() for filename in ("vocabulary.txt", "vocabulary.json"))
    except (OSError, ValueError):
        return False


def download_file(url, destination, progress=lambda *args: None, cancel=None, expected_size=None, sha256=None):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    last_error = None
    for attempt in range(3):
        check_cancel(cancel)
        done = temporary.stat().st_size if temporary.exists() else 0
        headers = {"User-Agent": "python-urllib/3"}
        if done:
            headers["Range"] = f"bytes={done}-"
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                if done and response.status != 206:
                    done = 0
                elif done and not response.headers.get("Content-Range", "").startswith(f"bytes {done}-"):
                    raise RuntimeError("模型续传范围不匹配")
                total = int(response.headers.get("Content-Length") or 0) + done
                with temporary.open("ab" if done else "wb") as stream:
                    while True:
                        check_cancel(cancel)
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        stream.write(chunk)
                        done += len(chunk)
                        progress(f"下载 {destination.name}：{done >> 20}MB / {total >> 20}MB", done * 100 // total if total else None)
                if (total and done != total) or (expected_size and done != expected_size):
                    raise RuntimeError("模型下载不完整")
            with temporary.open("rb") as stream:
                head = stream.read(100).lstrip()
                if head.lower().startswith((b"<!doctype", b"<html")):
                    temporary.unlink()
                    raise RuntimeError("模型地址返回了网页")
            if sha256:
                with temporary.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if digest != sha256:
                    temporary.unlink()
                    raise RuntimeError("模型校验失败")
            os.replace(temporary, destination)
            return destination
        except (OSError, RuntimeError) as exc:
            last_error = exc
            if cancel and cancel():
                check_cancel(cancel)
    raise RuntimeError(f"模型下载失败（可续传）：{last_error}")


def ensure_model(name, progress=lambda *args: None, cancel=None):
    if name not in REPOSITORIES:
        raise ValueError("未知识别模型")
    if model_is_ready(name):
        return str(model_path(name))
    with _DOWNLOAD_LOCK:
        repo = REPOSITORIES[name]
        metadata_url = f"https://huggingface.co/api/models/{repo}?blobs=true"
        try:
            with urllib.request.urlopen(metadata_url, timeout=20) as response:
                metadata = json.load(response)
            endpoint = "https://huggingface.co"
        except OSError:
            endpoint = "https://hf-mirror.com"
            with urllib.request.urlopen(metadata_url.replace("https://huggingface.co", endpoint), timeout=20) as response:
                metadata = json.load(response)
        revision = metadata["sha"]
        allowed = {"model.bin", "config.json", "tokenizer.json", "vocabulary.txt", "vocabulary.json", "preprocessor_config.json"}
        for file in metadata["siblings"]:
            filename = file["rfilename"]
            if filename not in allowed:
                continue
            destination = model_path(name) / filename
            if destination.exists() and destination.stat().st_size == file.get("size"):
                continue
            sha256 = (file.get("lfs") or {}).get("sha256")
            download_file(f"{endpoint}/{repo}/resolve/{revision}/{filename}", destination,
                          progress, cancel, file.get("size"), sha256)
        (model_path(name) / "download_manifest.json").write_text(json.dumps({"repository": repo, "revision": revision}, indent=2), encoding="utf-8")
    if not model_is_ready(name):
        raise RuntimeError("识别模型不完整，请重试下载")
    return str(model_path(name))


def ensure_punctuation(progress=lambda *args: None, cancel=None):
    if PUNCTUATION_PATH.is_file():
        return PUNCTUATION_PATH
    import tarfile
    with _DOWNLOAD_LOCK:
        archive = MODELS_DIR / "punctuation-model.tar.bz2"
        download_file(PUNCTUATION_URL, archive, progress, cancel)
        with tarfile.open(archive) as bundle:
            member = next(m for m in bundle.getmembers() if m.name.endswith("/model.int8.onnx"))
            check_cancel(cancel)
            PUNCTUATION_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary = PUNCTUATION_PATH.with_suffix(".tmp")
            with bundle.extractfile(member) as source, temporary.open("wb") as destination:
                import shutil
                shutil.copyfileobj(source, destination)
            os.replace(temporary, PUNCTUATION_PATH)
        archive.unlink()
    return PUNCTUATION_PATH
