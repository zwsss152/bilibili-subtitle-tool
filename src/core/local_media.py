"""Read-only access to user-owned media; originals never enter cache cleanup."""
import math
from pathlib import Path
import urllib.parse
import urllib.request

MEDIA_EXTENSIONS = frozenset('.mp4 .mkv .mov .webm .avi .m4v .flv .ts .mpg .mpeg .wav .mp3 .m4a .aac .flac .ogg .opus .wma'.split())


def media_path(value):
    value = str(value).strip().strip('"')
    if value.lower().startswith('file:'):
        parsed = urllib.parse.urlsplit(value)
        if parsed.netloc not in ('', 'localhost'):
            raise ValueError('请选择本机文件，不支持远程文件地址')
        value = urllib.request.url2pathname(parsed.path)
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError('请输入本地媒体文件的完整路径')
    try:
        path = path.resolve()
    except OSError as exc:
        raise ValueError('无法访问本地媒体路径') from exc
    if path.suffix.lower() not in MEDIA_EXTENSIONS:
        raise ValueError('请选择视频或音频文件')
    try:
        if not path.is_file():
            raise ValueError('本地媒体文件不存在或已移动，请重新选择')
        if not path.stat().st_size:
            raise ValueError('本地媒体文件为空')
    except OSError as exc:
        raise ValueError('无法读取本地媒体，请检查文件访问权限') from exc
    return path


def inspect_media(value):
    import av
    path = media_path(value)
    try:
        with av.open(str(path)) as container:
            if not container.streams.audio:
                raise ValueError('这个文件没有音轨，无法转录')
            stream = container.streams.audio[0]
            duration = (float(stream.duration * stream.time_base) if stream.duration and stream.time_base
                        else float(container.duration / av.time_base) if container.duration else 0)
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError('无法读取媒体时长，请检查文件是否完整')
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('无法读取本地媒体，请检查文件是否完整或被加密') from exc
    return {'path': str(path), 'title': path.stem, 'duration': duration, 'url': path.as_uri()}
