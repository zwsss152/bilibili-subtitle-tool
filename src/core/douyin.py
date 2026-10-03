"""Public Douyin share-page metadata, with the pinned yt-dlp extractor fallback."""
import html
import json
import re
import urllib.parse
import urllib.request
from .net import check_cancel

MOBILE_UA = 'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Mobile Safari/537.36'


def find_video_record(value, video_id):
    if isinstance(value, dict):
        if str(value.get('aweme_id', value.get('id', ''))) == str(video_id) and isinstance(value.get('video'), dict):
            return value
        for item in value.values():
            found = find_video_record(item, video_id)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_video_record(item, video_id)
            if found:
                return found


def parse_share_page(page, video_id):
    payloads = []
    for match in re.finditer(r'(?:window\.)?(?:_ROUTER_DATA|__INITIAL_STATE__)\s*=\s*', page):
        try:
            data, _ = json.JSONDecoder().raw_decode(page[match.end():])
            payloads.append(json.dumps(data))
        except ValueError:
            continue
    payloads += re.findall(r'<script[^>]*id=["\'](?:RENDER_DATA|__NEXT_DATA__)["\'][^>]*>(.*?)</script>', page, re.S)
    for payload in payloads:
        try:
            payload = html.unescape(payload).strip()
            data = json.loads(urllib.parse.unquote(payload) if payload.startswith('%') else payload)
        except ValueError:
            continue
        record = find_video_record(data, video_id)
        if record:
            video = record['video']
            # The music URL can contain only background music. Always use the
            # actual video playback stream to retain the spoken content.
            addresses = video.get('play_addr', {}).get('url_list', [])
            media = next((u for u in addresses if urllib.parse.urlsplit(u).scheme in ('http', 'https')), None)
            if media:
                return dict(title=record.get('desc') or f'抖音视频_{video_id}',
                            duration=float(video.get('duration', 0)) / 1000,
                            media_url=media, source_url=f'https://www.douyin.com/video/{video_id}')
    raise ValueError('抖音分享页未提供可获取的视频音频')


def fetch_douyin_video(video_id, cancel=None):
    check_cancel(cancel)
    try:
        request = urllib.request.Request(f'https://www.iesdouyin.com/share/video/{video_id}/',
                                        headers={'User-Agent': MOBILE_UA})
        with urllib.request.urlopen(request, timeout=20) as response:
            page = response.read(8 * 1024 * 1024).decode('utf-8')
        check_cancel(cancel)
        return parse_share_page(page, video_id)
    except (OSError, UnicodeError, ValueError):
        pass
    import yt_dlp
    check_cancel(cancel)
    try:
        with yt_dlp.YoutubeDL(dict(quiet=True, no_warnings=True, noplaylist=True, socket_timeout=20,
                                  cachedir=False, logger=_Logger())) as downloader:
            info = downloader.extract_info(f'https://www.douyin.com/video/{video_id}', download=False)
        check_cancel(cancel)
        return dict(title=info.get('title') or f'抖音视频_{video_id}', duration=info.get('duration') or 0,
                    source_url=f'https://www.douyin.com/video/{video_id}', media_url='')
    except yt_dlp.utils.DownloadError as exc:
        raise RuntimeError('抖音限制了直接访问，当前无法获取视频音频；需要可用的平台访问会话。不支持已删除、私密视频或图集') from exc


class _Logger:
    def debug(self, message):
        pass
    warning = debug
    error = debug
