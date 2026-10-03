# -*- coding: utf-8 -*-
"""机核网（gcores.com）播客接口。

机核页面是 React 动态渲染，页面 HTML 里拿不到音频；但官方 gapi 是开放的：
  GET https://www.gcores.com/gapi/v1/radios/{id}?include=media
    → data.attributes.title / duration
    → included[].attributes.audio = "xxxx.mp3"（音频文件名，不是完整 URL）
音频真实地址 = https://alioss.gcores.com/uploads/audio/{audio}
（规律来自其前端 JS：`${alioss_url}/uploads/audio/${e}`）
该直链为阿里云 OSS 公共读：无需鉴权、支持 Range 断点续传。
注意 include=media 必须带，否则 attributes 里没有 audio 字段。
"""
import re

from .net import http_get_json

GCORES_RE = re.compile(r"https?://(?:www\.)?gcores\.com/radios/(\d+)", re.IGNORECASE)
GCORES_OSS = "https://alioss.gcores.com/uploads/audio/"


def fetch_gcores_radio(radio_id):
    """读取机核电台节目标题、时长（秒）与音频直链。"""
    api = f"https://www.gcores.com/gapi/v1/radios/{radio_id}?include=media"
    try:
        data = http_get_json(api)
    except Exception as e:
        raise RuntimeError(f"读取机核节目信息失败：{e}")
    attrs = (data.get("data") or {}).get("attributes") or {}
    title = (attrs.get("title") or "").strip() or f"机核电台_{radio_id}"
    audio = ""
    for inc in data.get("included") or []:
        a = (inc.get("attributes") or {}).get("audio")
        if a:
            audio = a
            break
    if not audio:
        raise RuntimeError("未找到音频文件（该期可能不是标准播客节目）。")
    if not re.match(r"^https?://", audio):
        audio = GCORES_OSS + audio.lstrip("/")
    return {
        "title": title,
        "duration": int(attrs.get("duration") or 0),
        "audio_url": audio,
    }
