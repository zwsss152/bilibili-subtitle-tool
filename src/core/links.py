"""Parse supported links without discarding a selected Bilibili page."""
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from .bilibili import parse_video_id
from .net import UA_HEADERS, check_cancel

URL_RE = re.compile(r"https?://[^\s，。；;\"'<>）)】]+", re.I)


@dataclass(frozen=True)
class Source:
    kind: str
    ref: str
    url: str
    page: int | None = None

    @property
    def key(self):
        return f"{self.kind}:{self.ref}:{self.page or 'all'}"


def parse_source(raw):
    raw = raw.strip().rstrip(".,，。；;")
    if raw.startswith(("https://", "http://")):
        parsed = urllib.parse.urlsplit(raw)
        host = (parsed.hostname or "").lower()
        if host == "b23.tv":
            return Source("bilibili", raw, raw)
        if host == 'v.douyin.com':
            return Source('douyin', raw, raw)
        if host in ('douyin.com', 'www.douyin.com', 'iesdouyin.com', 'www.iesdouyin.com'):
            match = re.fullmatch(r'/(?:share/)?video/(\d+)/?', parsed.path)
            query = urllib.parse.parse_qs(parsed.query)
            video_id = match[1] if match else query.get('modal_id', [''])[0]
            if video_id.isdigit():
                return Source('douyin', video_id, f'https://www.douyin.com/video/{video_id}')
            raise ValueError('请输入抖音视频链接或分享短链，暂不支持直播与图集')
        if host in ("gcores.com", "www.gcores.com"):
            match = re.fullmatch(r"/radios/(\d+)/?", parsed.path)
            if match:
                return Source("gcores", match[1], f"https://www.gcores.com/radios/{match[1]}")
            raise ValueError("请输入机核电台节目链接（/radios/编号）")
        if host not in ("bilibili.com", "www.bilibili.com", "m.bilibili.com"):
            raise ValueError("仅支持 B 站、抖音和机核电台")
        query = urllib.parse.parse_qs(parsed.query)
        page = query.get("p", [None])[0]
        if page is not None and (not page.isdigit() or int(page) < 1):
            raise ValueError("分 P 参数必须是正整数")
        kind, value = parse_video_id(parsed.path)
        return Source("bilibili", f"{kind}:{value}", raw, int(page) if page else None)
    kind, value = parse_video_id(raw)
    return Source("bilibili", f"{kind}:{value}", raw)


def extract_sources(text):
    tokens = URL_RE.findall(text)
    remainder = URL_RE.sub(" ", text)
    tokens.extend(re.findall(r"\bBV[0-9A-Za-z]{10}\b|\bav\d{1,12}\b", remainder, re.I))
    if not tokens and text.strip().isdigit():
        tokens = [text.strip()]
    sources, rejected = [], []
    for token in tokens:
        try:
            source = parse_source(token)
            if source.key not in [s.key for s in sources]:
                sources.append(source)
        except ValueError as exc:
            rejected.append(str(exc))
    return sources, rejected


def resolve_source(source, cancel=None):
    if not source.ref.startswith("http"):
        return source
    check_cancel(cancel)
    request = urllib.request.Request(source.url, headers=UA_HEADERS)
    with urllib.request.urlopen(request, timeout=20) as response:
        final_url = response.geturl()
    check_cancel(cancel)
    result = parse_source(final_url)
    if result.kind != source.kind or result.ref.startswith("http"):
        raise ValueError("分享短链没有解析为有效视频")
    return result
