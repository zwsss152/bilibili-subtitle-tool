"""Bounded network operations, cancellation and per-host throttling."""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

UA_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"}
REQUEST_GAP_SECONDS = 0.4
_LOCK = threading.Lock()
_LAST = {}


class JobCancelled(Exception):
    pass


def check_cancel(cancel=None):
    if cancel and cancel():
        raise JobCancelled()


def throttle(url="https://api.bilibili.com", cancel=None):
    host = urllib.parse.urlsplit(url).hostname
    with _LOCK:
        delay = max(0, REQUEST_GAP_SECONDS - (time.monotonic() - _LAST.get(host, 0)))
        end = time.monotonic() + delay
        while time.monotonic() < end:
            check_cancel(cancel)
            time.sleep(min(0.05, max(0, end - time.monotonic())))
        _LAST[host] = time.monotonic()


def headers_for(url, cookie=""):
    headers = dict(UA_HEADERS)
    host = urllib.parse.urlsplit(url).hostname or ""
    if host == "bilibili.com" or host.endswith(".bilibili.com") or host.endswith(".hdslb.com"):
        headers["Referer"] = "https://www.bilibili.com/"
        if cookie.strip() and (host == "bilibili.com" or host.endswith(".bilibili.com")):
            headers["Cookie"] = cookie.strip()
    elif host == "gcores.com" or host.endswith(".gcores.com"):
        headers["Referer"] = "https://www.gcores.com/"
    elif host in ('douyin.com', 'iesdouyin.com') or host.endswith(('.douyin.com', '.iesdouyin.com', '.douyinvod.com', '.amemv.com')):
        headers['Referer'] = 'https://www.douyin.com/'
    return headers


def http_get_json(url, cookie="", cancel=None):
    check_cancel(cancel)
    throttle(url, cancel)
    request = urllib.request.Request(url, headers=headers_for(url, cookie))
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
        check_cancel(cancel)
        return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"网络请求失败（HTTP {exc.code}），请稍后重试") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("网络连接失败，请检查网络后重试") from exc
    except (UnicodeError, ValueError) as exc:
        raise RuntimeError("接口返回格式异常，可能需要登录或接口已变化") from exc


urlencode = urllib.parse.urlencode
