"""Audio acquisition with integrity checks and task-owned temporary files."""
import gc
import os
from pathlib import Path
import shutil
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from .net import JobCancelled, check_cancel, headers_for
from ..paths import ASR_CACHE_DIR


def find_ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return shutil.which("ffmpeg")


def download_audio(url, tag, workdir, progress_cb, cookie="", cancel=None):
    import yt_dlp
    check_cancel(cancel)
    directory = Path(workdir)
    directory.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("缺少音频转换程序，请运行环境诊断")

    def hook(data):
        check_cancel(cancel)
        if data.get("status") == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            done = data.get("downloaded_bytes", 0)
            progress_cb(f"下载音频 {done >> 20}MB", min(99, done * 100 // total) if total else None)

    options = dict(format="bestaudio/best", outtmpl=str(directory / f"{tag}.%(ext)s"),
                   cachedir=str(directory / 'yt-dlp-cache'),
                   quiet=True, noprogress=True, no_warnings=True, noplaylist=True, retries=2,
                   fragment_retries=2, socket_timeout=20, ffmpeg_location=ffmpeg,
                   http_headers=headers_for(url, cookie), progress_hooks=[hook],
                   postprocessors=[dict(key="FFmpegExtractAudio", preferredcodec="m4a")])
    with yt_dlp.YoutubeDL(options) as downloader:
        downloader.download([url])
    check_cancel(cancel)
    target = directory / f"{tag}.m4a"
    if not target.is_file() or target.stat().st_size < 100:
        raise RuntimeError("音频下载没有产生有效文件")
    return str(target)


def _probe_url(url):
    request = urllib.request.Request(url, headers=headers_for(url), method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return int(response.headers.get("Content-Length") or 0), response.headers.get("Accept-Ranges") == "bytes"
    except OSError:
        return 0, False


def download_direct_audio(url, dest_path, progress_cb, threads=4, cancel=None):
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_name(dest.name + ".part")
    size, ranges = _probe_url(url)
    check_cancel(cancel)
    counts, lock = {}, threading.Lock()

    def transfer(start=None, end=None, index=0):
        headers = headers_for(url)
        if start is not None:
            headers["Range"] = f"bytes={start}-{end}"
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=20) as response:
            if start is not None:
                expected_range = f"bytes {start}-{end}/{size}"
                if response.status != 206 or response.headers.get("Content-Range") != expected_range:
                    raise RuntimeError("服务器返回了错误的音频分块")
                expected = end - start + 1
            else:
                expected = int(response.headers.get("Content-Length") or size or 0)
            done = 0
            with temporary.open("r+b" if start is not None else "wb") as stream:
                stream.seek(start or 0)
                while True:
                    check_cancel(cancel)
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    done += len(chunk)
                    if expected and done > expected:
                        raise RuntimeError("音频响应超过预期大小")
                    stream.write(chunk)
                    with lock:
                        counts[index] = done
                        total_done = sum(counts.values())
                    total = size or expected
                    progress_cb(f"下载音频 {total_done >> 20}MB", min(99, total_done * 100 // total) if total else None)
            if expected and done != expected:
                raise RuntimeError(f"音频下载不完整：收到 {done} / {expected} 字节")
            if done == 0:
                raise RuntimeError("音频响应为空")

    try:
        if ranges and size >= 2 * 1024 * 1024:
            count = max(1, min(threads, size // (1024 * 1024)))
            with temporary.open("wb") as stream:
                stream.truncate(size)
            step = size // count
            with ThreadPoolExecutor(max_workers=count) as pool:
                futures = [pool.submit(transfer, i * step, size - 1 if i == count - 1 else (i + 1) * step - 1, i)
                           for i in range(count)]
                for future in futures:
                    future.result()
            if sum(counts.values()) != size:
                raise RuntimeError("音频分块总长度异常")
        else:
            transfer()
        check_cancel(cancel)
        os.replace(temporary, dest)
        return str(dest)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def probe_duration(path):
    import av
    with av.open(str(path)) as container:
        return container.duration / av.time_base if container.duration else 0.0


def cleanup_owned(directory):
    """The resolved target must be a child of this checkout's audio cache."""
    target = Path(directory).resolve()
    root = ASR_CACHE_DIR.resolve()
    if not target.is_relative_to(root) or target == root:
        raise ValueError("拒绝清理不属于任务的目录")
    for attempt in range(5):
        if not target.exists():
            return
        try:
            shutil.rmtree(target)
            return
        except OSError:
            gc.collect()
            time.sleep(0.15 * (attempt + 1))
    raise RuntimeError("音频缓存暂时被占用，将在下次启动时清理")


def cleanup_stale_cache(log=None):
    if ASR_CACHE_DIR.exists():
        for directory in ASR_CACHE_DIR.iterdir():
            if directory.is_dir() and directory.name.startswith("job_"):
                try:
                    cleanup_owned(directory)
                except (OSError, RuntimeError, ValueError) as exc:
                    if log:
                        log(str(exc))
