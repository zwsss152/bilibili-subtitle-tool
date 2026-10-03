# -*- coding: utf-8 -*-
"""B 站接口层：视频信息 / CC 字幕（wbi 签名）/ 字幕下载。纯标准库。"""
import hashlib
import re
import time

from .net import http_get_json, urlencode

BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})", re.IGNORECASE)
AV_RE = re.compile(r"av(\d{1,12})", re.IGNORECASE)


class NoSubtitleError(Exception):
    """视频没有可用的 CC 字幕。"""


def parse_video_id(text):
    """从任意输入中解析 BV 号或 av 号。"""
    text = text.strip()
    if not text:
        raise ValueError("请输入 B 站视频链接或 BV 号。")
    m = BV_RE.search(text)
    if m:
        return "bvid", m.group(1)
    m = AV_RE.search(text)
    if m:
        return "aid", m.group(1)
    if text.isdigit():
        return "aid", text
    raise ValueError("无法识别 BV 号 / av 号，请检查输入格式。")


def fetch_video_info(id_type, id_value, cookie=""):
    query = urlencode({id_type: id_value})
    data = http_get_json(f"https://api.bilibili.com/x/web-interface/view?{query}", cookie)
    if data.get("code") != 0:
        raise RuntimeError(
            f"获取视频信息失败（code={data.get('code')}）：{data.get('message', '')}"
        )
    d = data["data"]
    pages = [
        {"cid": p["cid"], "part": p.get("part", ""), "page": p.get("page", 1),
         "duration": p.get("duration", 0)}
        for p in d.get("pages", [])
    ]
    if not pages:
        raise RuntimeError("该视频没有可用分 P（可能已失效）。")
    return {
        "bvid": d.get("bvid", ""),
        "title": d.get("title", ""),
        "owner": (d.get("owner") or {}).get("name", ""),
        "duration": d.get("duration", 0),
        "pages": pages,
    }


def fetch_subtitle_list(bvid, cid, cookie=""):
    """优先 wbi 签名接口（未登录命中率更高），失败回退旧接口。"""
    try:
        subs = _fetch_subtitle_wbi(bvid, cid, cookie)
        if subs:
            return subs
    except Exception:
        pass
    query = urlencode({"bvid": bvid, "cid": cid})
    data = http_get_json(f"https://api.bilibili.com/x/player/v2?{query}", cookie)
    if data.get("code") != 0:
        raise RuntimeError(
            f"获取字幕信息失败（code={data.get('code')}）：{data.get('message', '')}"
        )
    sub = (data.get("data") or {}).get("subtitle") or {}
    return sub.get("subtitles") or []


# --- wbi 签名（B 站公开的风控签名算法） ---
_WBI_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
            27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
            37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
            22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]
_wbi_cache = {"ts": 0.0, "img": "", "sub": ""}


def _get_wbi_keys(cookie=""):
    if time.time() - _wbi_cache["ts"] < 3600 and _wbi_cache["img"]:
        return _wbi_cache["img"], _wbi_cache["sub"]
    data = http_get_json("https://api.bilibili.com/x/web-interface/nav", cookie)
    wbi = ((data.get("data") or {}).get("wbi_img")) or {}
    img = wbi.get("img_url", "").rsplit("/", 1)[-1].split(".")[0]
    sub = wbi.get("sub_url", "").rsplit("/", 1)[-1].split(".")[0]
    if not img or not sub:
        raise RuntimeError("无法获取 wbi 密钥")
    _wbi_cache.update(ts=time.time(), img=img, sub=sub)
    return img, sub


def _fetch_subtitle_wbi(bvid, cid, cookie=""):
    img, sub = _get_wbi_keys(cookie)
    mixin = "".join((img + sub)[i] for i in _WBI_TAB)[:32]
    params = {"bvid": bvid, "cid": cid, "wts": int(time.time())}
    query = urlencode(sorted(params.items()))
    params["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    url = "https://api.bilibili.com/x/player/wbi/v2?" + urlencode(params)
    data = http_get_json(url, cookie)
    if data.get("code") != 0:
        raise RuntimeError(f"wbi 接口错误 code={data.get('code')}")
    sub_data = (data.get("data") or {}).get("subtitle") or {}
    return sub_data.get("subtitles") or []


def download_subtitle(subtitle_url, cookie=""):
    """下载字幕 JSON，返回 [(from_s, to_s, text), ...]"""
    if subtitle_url.startswith("//"):
        subtitle_url = "https:" + subtitle_url
    data = http_get_json(subtitle_url, cookie)
    body = data.get("body")
    if not isinstance(body, list):
        raise RuntimeError("字幕文件格式异常，无法解析。")
    items = []
    for it in body:
        content = str(it.get("content", "")).strip()
        if content:
            items.append((float(it.get("from", 0)), float(it.get("to", 0)), content))
    items.sort(key=lambda x: x[0])
    if not items:
        raise RuntimeError("字幕文件为空。")
    return items


def fmt_ts(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
