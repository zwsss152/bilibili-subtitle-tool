# -*- coding: utf-8 -*-
"""
B 站视频字幕提取工具（本地运行版）
====================================
功能：
  1. 输入 B 站视频链接 / BV 号 / av 号，抓取视频 CC 字幕
  2. 无 CC 字幕的视频：可选本地语音识别（yt-dlp 下载音频 + faster-whisper 离线转写）
  3. 展示完整字幕（带时间轴 / 纯文本两种视图）
  4. 一键复制、导出为 .txt

合规提醒：本工具仅供个人学习使用，请遵守 bilibili 用户协议与相关版权
法规，控制请求频率，勿批量抓取或传播他人受版权保护的内容。

运行：双击 启动.bat（自动使用项目内 .venv 环境）。
"""

import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog

# 模型统一存放到项目目录内，保持「全部本地、集中管理、可整体删除」
MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
# 模型文件直接地址（国内镜像站，不走 HF SDK，避免符号链接/缓存兼容问题）
HF_MIRROR = "https://hf-mirror.com"
MODEL_FILES = ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"]

try:
    import yt_dlp
    _HAS_YTDLP = True
except ImportError:
    _HAS_YTDLP = False

try:
    from faster_whisper import WhisperModel
    _HAS_WHISPER = True
except ImportError:
    _HAS_WHISPER = False

ASR_AVAILABLE = _HAS_YTDLP and _HAS_WHISPER
ASR_MODELS = ["tiny", "base", "small", "medium"]
ASR_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "asr_cache")

APP_TITLE = "视频字幕提取工具（B 站 / 抖音 · 本地版）"
UA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com",
}
REQUEST_GAP_SECONDS = 1.2  # 风控：两次请求之间的最小间隔
_last_request_ts = [0.0]


# ----------------------------------------------------------------------
# 网络层
# ----------------------------------------------------------------------
def _throttle():
    now = time.time()
    wait = REQUEST_GAP_SECONDS - (now - _last_request_ts[0])
    if wait > 0:
        time.sleep(wait)
    _last_request_ts[0] = time.time()


def http_get_json(url, cookie=""):
    _throttle()
    headers = dict(UA_HEADERS)
    if cookie.strip():
        headers["Cookie"] = cookie.strip()
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"网络请求失败：HTTP {e.code}（可能被风控拦截，请稍后重试）")
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络连接失败：{e.reason}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError("接口返回内容不是有效 JSON，可能已被风控拦截或接口变动。")


# ----------------------------------------------------------------------
# B 站接口层
# ----------------------------------------------------------------------
BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})", re.IGNORECASE)
AV_RE = re.compile(r"av(\d{1,12})", re.IGNORECASE)


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
    query = urllib.parse.urlencode({id_type: id_value})
    url = f"https://api.bilibili.com/x/web-interface/view?{query}"
    data = http_get_json(url, cookie)
    if data.get("code") != 0:
        raise RuntimeError(
            f"获取视频信息失败（code={data.get('code')}）：{data.get('message', '')}"
        )
    d = data["data"]
    pages = [
        {"cid": p["cid"], "part": p.get("part", ""), "page": p.get("page", 1)}
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
    """优先用 wbi 签名接口（未登录时命中率更高），失败回退旧接口。"""
    try:
        subs = _fetch_subtitle_wbi(bvid, cid, cookie)
        if subs:
            return subs
    except Exception:
        pass
    query = urllib.parse.urlencode({"bvid": bvid, "cid": cid})
    url = f"https://api.bilibili.com/x/player/v2?{query}"
    data = http_get_json(url, cookie)
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
    query = urllib.parse.urlencode(sorted(params.items()))
    params["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    url = "https://api.bilibili.com/x/player/wbi/v2?" + urllib.parse.urlencode(params)
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


# ----------------------------------------------------------------------
# 本地语音识别（yt-dlp 下载音频 + faster-whisper 离线转写）
# ----------------------------------------------------------------------
def find_ffmpeg():
    """优先用 imageio-ffmpeg 自带的静态 ffmpeg，其次找系统 PATH。"""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except ImportError:
        pass
    return shutil.which("ffmpeg")


def download_audio(url, tag, workdir, progress_cb):
    """用 yt-dlp 下载任意支持站点（B 站 / 抖音等）的音频并转为 m4a。

    progress_cb(msg, pct)：pct 为 0-100 的整数或 None（不确定进度）。
    """
    if not _HAS_YTDLP:
        raise RuntimeError("未安装 yt-dlp，无法下载音频。")
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg（imageio-ffmpeg 未安装且 PATH 中也没有）。")
    os.makedirs(workdir, exist_ok=True)
    outtmpl = os.path.join(workdir, f"{tag}.%(ext)s")

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            if total:
                pct = done * 100 // total
                progress_cb(f"下载音频中…… {done >> 20}MB / {total >> 20}MB（{pct}%）", pct)

    opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": ffmpeg,
        "http_headers": dict(UA_HEADERS),
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 30,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "m4a",
        }],
        "progress_hooks": [hook],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    target = os.path.join(workdir, f"{tag}.m4a")
    if not os.path.exists(target):
        raise RuntimeError("音频下载失败，未生成目标文件（可能触发风控，请稍后重试）。")
    return target


def fetch_media_title(url):
    """用 yt-dlp 读取任意支持站点的视频标题（不下载）。"""
    if not _HAS_YTDLP:
        return ""
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                               "http_headers": dict(UA_HEADERS)}) as ydl:
            info = ydl.extract_info(url, download=False)
            return (info or {}).get("title", "")
    except Exception:
        return ""


def ensure_model(model_size, progress_cb):
    """确保模型文件已在本地下载好，返回模型目录路径。"""
    if model_size not in ASR_MODELS:
        raise ValueError(f"未知模型：{model_size}")
    model_dir = os.path.join(MODELS_DIR, f"faster-whisper-{model_size}")
    os.makedirs(model_dir, exist_ok=True)
    repo = f"Systran/faster-whisper-{model_size}"
    for name in MODEL_FILES:
        dst = os.path.join(model_dir, name)
        # 已存在且不是错误页（过小文件视为损坏）则跳过
        if os.path.exists(dst) and os.path.getsize(dst) > 10000:
            continue
        if name in ("config.json", "vocabulary.txt") and os.path.exists(dst) \
                and os.path.getsize(dst) > 500:
            with open(dst, "rb") as f:
                if not f.read(15).startswith(b"<!DOCTYPE"):
                    continue
        url = f"{HF_MIRROR}/{repo}/resolve/main/{name}"
        tmp = dst + ".part"
        last_err = None
        for attempt in range(3):
            try:
                # 断点续传：.part 已存在则从未完成的字节继续
                done = os.path.getsize(tmp) if os.path.exists(tmp) else 0
                # 注意：镜像站对浏览器 UA / Referer 会返回反爬 HTML 页，这里用朴素请求头
                headers = {"User-Agent": "python-urllib/3"}
                if done:
                    headers["Range"] = f"bytes={done}-"
                    progress_cb(f"继续下载模型 {name}（已下载 {done >> 20}MB）……", None)
                else:
                    progress_cb(f"下载模型文件 {name}（仅此一次）……", None)
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=60) as resp:
                    if done and resp.status != 206:
                        # 服务端不支持断点续传，重新下载
                        done = 0
                    total = int(resp.headers.get("Content-Length") or 0) + done
                    with open(tmp, "ab" if done else "wb") as f:
                        while True:
                            chunk = resp.read(1 << 20)
                            if not chunk:
                                break
                            f.write(chunk)
                            done += len(chunk)
                            if total:
                                pct = done * 100 // total
                                progress_cb(
                                    f"下载模型 {name}：{done >> 20}MB / "
                                    f"{total >> 20}MB（{pct}%）", pct,
                                )
                os.replace(tmp, dst)
                last_err = None
                break
            except Exception as e:
                last_err = e
                time.sleep(2 * (attempt + 1))
        if last_err is not None:
            raise RuntimeError(
                f"模型文件 {name} 下载失败（已重试 3 次，已下载部分已保留，"
                f"重试会自动续传）：{last_err}"
            )
    return model_dir


def transcribe_audio(audio_path, model_size, progress_cb):
    """faster-whisper 离线转写，返回 [(from, to, text), ...]。"""
    if not _HAS_WHISPER:
        raise RuntimeError("未安装 faster-whisper，无法本地识别。")
    model_dir = ensure_model(model_size, progress_cb)
    progress_cb(f"加载识别模型 {model_size}……", None)
    model = WhisperModel(model_dir, device="cpu", compute_type="int8")

    def run(vad):
        segments, info = model.transcribe(audio_path, vad_filter=vad, beam_size=5)
        duration = max(info.duration, 1.0)
        t0 = time.time()
        out = []
        for seg in segments:
            out.append((seg.start, seg.end, seg.text.strip()))
            frac = min(seg.end / duration, 1.0)
            pct = int(frac * 100)
            eta = ""
            elapsed = time.time() - t0
            if frac > 0.03 and elapsed > 2:
                remain = int(elapsed * (1 - frac) / frac)
                eta = f"，预计剩余 {remain // 60}分{remain % 60}秒" if remain >= 60 \
                    else f"，预计剩余约 {remain} 秒"
            progress_cb(f"语音识别中…… {pct}%{eta}", pct)
        return out

    items = run(vad=True)
    if not items:
        # VAD 可能把弱人声/音乐全过滤掉，关闭 VAD 重试一次
        progress_cb("VAD 未检出语音，关闭过滤重试……", None)
        items = run(vad=False)
    if not items:
        raise RuntimeError("识别完成但未产生任何文字（音频可能无人声）。")
    return items


def render_with_timestamps(items):
    return "\n".join(f"[{fmt_ts(f)}] {t}" for f, _, t in items)


def render_plain_text(items):
    """合并为通顺的纯文本（按时间断行，便于通读）。"""
    lines = []
    buf = ""
    last_end = None
    for frm, to, text in items:
        if last_end is not None and frm - last_end > 8 and buf:
            lines.append(buf)
            buf = ""
        buf += text if buf == "" else (" " + text if re.match(r"[A-Za-z0-9]", text) else text)
        last_end = to
    if buf:
        lines.append(buf)
    return "\n\n".join(lines)


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("920x680")
        self.minsize(760, 560)

        self.video_info = None   # dict from fetch_video_info
        self.subtitle_items = None
        self.subtitle_lang = ""
        self._build_ui()

    # ---------------- UI 搭建 ----------------
    def _build_ui(self):
        # 顶部输入区
        top = ttk.LabelFrame(self, text="视频地址")
        top.pack(fill="x", padx=10, pady=(10, 4))

        ttk.Label(top, text="链接/BV号/抖音：").grid(row=0, column=0, padx=(8, 2), pady=8, sticky="e")
        self.url_var = tk.StringVar()
        url_entry = ttk.Entry(top, textvariable=self.url_var)
        url_entry.grid(row=0, column=1, padx=2, pady=8, sticky="ew")
        url_entry.bind("<Return>", lambda e: self.start_fetch())

        self.page_var = tk.StringVar()
        self.page_box = ttk.Combobox(top, textvariable=self.page_var, width=14, state="disabled")
        self.page_box.grid(row=0, column=2, padx=4, pady=8)
        self.page_box.bind("<<ComboboxSelected>>",
                           lambda e: self._load_subtitle_for_page(self.cookie_var.get()))

        self.fetch_btn = ttk.Button(top, text="提取字幕", command=self.start_fetch)
        self.fetch_btn.grid(row=0, column=3, padx=(4, 8), pady=8)
        top.columnconfigure(1, weight=1)

        # Cookie（可选，折叠展示）
        cookie_frame = ttk.Frame(self)
        cookie_frame.pack(fill="x", padx=10)
        self.show_cookie = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            cookie_frame, text="字幕接口需要登录时填写 Cookie（可选）",
            variable=self.show_cookie, command=self._toggle_cookie,
        ).pack(side="left")
        self.cookie_var = tk.StringVar()
        self.cookie_entry = ttk.Entry(cookie_frame, textvariable=self.cookie_var)

        # 语音识别选项
        asr_frame = ttk.Frame(self)
        asr_frame.pack(fill="x", padx=10, pady=(2, 0))
        self.asr_enabled = tk.BooleanVar(value=ASR_AVAILABLE)
        self.asr_check = ttk.Checkbutton(
            asr_frame, text="无字幕时使用本地语音识别（下载音频 + 离线转写）",
            variable=self.asr_enabled,
        )
        self.asr_check.pack(side="left")
        ttk.Label(asr_frame, text="模型：").pack(side="left", padx=(12, 2))
        self.model_var = tk.StringVar(value="small")
        self.model_box = ttk.Combobox(
            asr_frame, textvariable=self.model_var, width=8,
            values=ASR_MODELS, state="readonly",
        )
        self.model_box.pack(side="left")
        hint = ("（越大越准但越慢，首次使用需联网下载模型）" if ASR_AVAILABLE
                else "（未检测到 yt-dlp / faster-whisper，语音识别不可用）")
        ttk.Label(asr_frame, text=hint, foreground="#777").pack(side="left", padx=6)
        if not ASR_AVAILABLE:
            self.asr_check.configure(state="disabled")
            self.model_box.configure(state="disabled")

        # 视频信息栏
        self.info_var = tk.StringVar(value="等待输入视频链接……")
        ttk.Label(self, textvariable=self.info_var, foreground="#444").pack(
            fill="x", padx=12, pady=(4, 0)
        )

        # 进度条（常驻可见，工作时实时填充）
        prog_frame = ttk.Frame(self)
        prog_frame.pack(fill="x", padx=12, pady=(4, 0))
        ttk.Label(prog_frame, text="进度：").pack(side="left")
        self.progress = ttk.Progressbar(prog_frame, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True, padx=(4, 0))

        # 视图切换 + 操作按钮
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=(4, 0))
        ttk.Label(bar, text="显示方式：").pack(side="left")
        self.view_var = tk.StringVar(value="ts")
        ttk.Radiobutton(bar, text="带时间轴", value="ts",
                        variable=self.view_var, command=self.refresh_view).pack(side="left")
        ttk.Radiobutton(bar, text="纯文本（通读）", value="plain",
                        variable=self.view_var, command=self.refresh_view).pack(side="left")
        ttk.Button(bar, text="复制全部", command=self.copy_all).pack(side="right", padx=4)
        ttk.Button(bar, text="导出 TXT", command=self.export_txt).pack(side="right")

        # 字幕展示区
        self.text = scrolledtext.ScrolledText(self, wrap="word", font=("Microsoft YaHei UI", 11))
        self.text.pack(fill="both", expand=True, padx=10, pady=6)
        self.text.insert("1.0", "粘贴 B 站链接 / BV 号 / 抖音分享链接，点击「提取字幕」。\n\n"
                                "说明：\n"
                                "· B 站：优先提取 CC 字幕（UP 主上传或平台 AI 生成），最准最快；\n"
                                "· B 站无 CC 字幕或抖音视频：自动转本地语音识别（下载音频 + 离线转写）；\n"
                                "· 底部有进度条和预计剩余时间；\n"
                                "· 你在 B 站播放器看到的字幕若是「烧在画面里的硬字幕」，接口取不到，"
                                "只能语音识别；填写自己的 Cookie 可提高 CC 字幕命中率。")
        self.text.configure(state="disabled")

        # 底部风控提示
        notice = ("合规与风控提醒：本工具仅本地运行、仅供个人学习使用；"
                  "已内置请求限速（间隔 %.1f 秒），请勿频繁/批量抓取，"
                  "请遵守 bilibili / 抖音的用户协议并尊重视频版权。") % REQUEST_GAP_SECONDS
        ttk.Label(self, text=notice, foreground="#a33", wraplength=880,
                  justify="left").pack(fill="x", padx=10, pady=(0, 6))

        # 状态栏
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self, textvariable=self.status_var, relief="sunken",
                  anchor="w").pack(fill="x", side="bottom")

    def _on_progress(self, msg, pct=None):
        """工作线程的进度汇报：更新状态栏与进度条。"""
        self.status_var.set(msg)
        if pct is None:
            if str(self.progress.cget("mode")) != "indeterminate":
                self.progress.configure(mode="indeterminate")
                self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.progress["value"] = pct

    def _toggle_cookie(self):
        if self.show_cookie.get():
            self.cookie_entry.pack(side="left", fill="x", expand=True, padx=(8, 0))
        else:
            self.cookie_entry.pack_forget()

    # ---------------- 抓取流程 ----------------
    def set_busy(self, busy, msg=""):
        self.fetch_btn.configure(state="disabled" if busy else "normal")
        if not busy:
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.progress["value"] = 0
        if msg:
            self.status_var.set(msg)
        self.update_idletasks()

    def start_fetch(self):
        raw = self.url_var.get()
        # 抖音链接：无 CC 字幕概念，直接走本地语音识别
        m = re.search(r"https?://[^\s，。\"'）)】]*douyin\.com[^\s，。\"'）)】]*", raw)
        if m:
            self.start_douyin(m.group(0))
            return
        try:
            id_type, id_value = parse_video_id(raw)
        except ValueError as e:
            messagebox.showwarning(
                "输入有误",
                str(e) + "\n\n支持：B 站链接 / BV 号 / av 号 / 抖音分享链接。",
            )
            return
        cookie = self.cookie_var.get()
        self.set_busy(True, "正在获取视频信息……")
        threading.Thread(target=self._worker_info,
                         args=(id_type, id_value, cookie), daemon=True).start()

    # ---------------- 抖音流程 ----------------
    def start_douyin(self, url):
        if not ASR_AVAILABLE:
            messagebox.showwarning("不可用", "未检测到 yt-dlp / faster-whisper，无法处理抖音视频。")
            return
        ok = messagebox.askyesno(
            "抖音视频",
            "抖音没有官方字幕，将直接使用本地语音识别：\n"
            "下载音频 → 离线转写。\n\n是否继续？",
        )
        if not ok:
            return
        model_size = self.model_var.get() or "small"
        self.video_info = {"bvid": "", "title": "抖音视频", "owner": "",
                           "duration": 0, "pages": [{"page": 1, "cid": 0, "part": ""}]}
        self.set_busy(True, "正在解析抖音链接……")
        threading.Thread(target=self._worker_douyin,
                         args=(url, model_size), daemon=True).start()

    def _worker_douyin(self, url, model_size):
        def cb(msg, pct=None):
            self.after(0, self._on_progress, msg, pct)
        audio_path = None
        try:
            title = fetch_media_title(url)
            if title:
                self.after(0, self.info_var.set, f"标题：{title}（抖音）")
                self.after(0, lambda: self.video_info.update(title=title))
            audio_path = download_audio(url, "douyin_audio", ASR_CACHE_DIR, cb)
            items = transcribe_audio(audio_path, model_size, cb)
        except Exception as e:
            self.after(0, self._on_error, "抖音视频处理失败", e)
            return
        finally:
            if audio_path and os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except OSError:
                    pass
        self.after(0, self._on_subtitle_ok, items,
                   f"本地语音识别（{model_size}，结果可能有错字）")

    def _worker_info(self, id_type, id_value, cookie):
        try:
            info = fetch_video_info(id_type, id_value, cookie)
        except Exception as e:
            self.after(0, self._on_error, "获取视频信息失败", e)
            return
        self.after(0, self._on_info_ok, info, cookie)

    def _on_info_ok(self, info, cookie):
        self.video_info = info
        pages = info["pages"]
        self.info_var.set(
            f"标题：{info['title']}    UP主：{info['owner']}    "
            f"时长：{fmt_ts(info['duration'])}    分P数：{len(pages)}"
        )
        if len(pages) > 1:
            self.page_box.configure(
                state="readonly",
                values=[f"P{p['page']} {p['part'][:18]}" for p in pages],
            )
            self.page_box.current(0)
        else:
            self.page_box.configure(state="disabled", values=[])
            self.page_var.set("")
        self._load_subtitle_for_page(cookie)

    def _load_subtitle_for_page(self, cookie):
        info = self.video_info
        pages = info["pages"]
        idx = 0
        if len(pages) > 1:
            sel = self.page_box.current()
            idx = sel if sel >= 0 else 0
        page = pages[idx]
        self.set_busy(True, f"正在获取 P{page['page']} 的字幕……")
        threading.Thread(target=self._worker_subtitle,
                         args=(info["bvid"], page["cid"], cookie), daemon=True).start()

    def _worker_subtitle(self, bvid, cid, cookie):
        try:
            subs = fetch_subtitle_list(bvid, cid, cookie)
            if not subs:
                raise NoSubtitleError
            # 优先中文字幕
            chosen = None
            for s in subs:
                if "zh" in (s.get("lan") or ""):
                    chosen = s
                    break
            if chosen is None:
                chosen = subs[0]
            items = download_subtitle(chosen["subtitle_url"], cookie)
            lang = chosen.get("lan_doc") or chosen.get("lan") or ""
        except NoSubtitleError:
            self.after(0, self._on_no_subtitle)
            return
        except Exception as e:
            self.after(0, self._on_error, "获取字幕失败", e)
            return
        self.after(0, self._on_subtitle_ok, items, lang)

    # ---------------- 结果处理 ----------------
    def _on_no_subtitle(self):
        self.subtitle_items = None
        self._set_text("")
        if self.asr_enabled.get() and ASR_AVAILABLE:
            ok = messagebox.askyesno(
                "无 CC 字幕",
                "该视频没有可提取的 CC 字幕。\n\n"
                "是否改用本地语音识别？流程：下载音频 → 离线转写，"
                "10 分钟视频约需几分钟。\n\n"
                "注意：首次使用需先下载识别模型（small 约 480MB），"
                "期间窗口看似卡住实属正常，状态栏有进度，"
                "请勿关闭窗口；中断后再试会自动续传。",
            )
            if ok:
                self.start_asr()
                return
        self.set_busy(False, "该视频没有 CC 字幕")
        messagebox.showinfo(
            "无字幕",
            "该视频没有可提取的 CC 字幕（既无 UP 主上传字幕，也无平台 AI 字幕）。\n\n"
            "提示：可勾选「无字幕时使用本地语音识别」后重新提取；"
            "部分视频需登录后才返回字幕列表，也可填写 Cookie 后重试。",
        )

    # ---------------- 本地语音识别流程 ----------------
    def start_asr(self):
        info = self.video_info
        if not info:
            return
        idx = self.page_box.current() if len(info["pages"]) > 1 else 0
        if idx < 0:
            idx = 0
        page = info["pages"][idx]["page"]
        model_size = self.model_var.get() or "small"
        self.set_busy(True, "准备语音识别……")
        threading.Thread(target=self._worker_asr,
                         args=(info["bvid"], page, model_size), daemon=True).start()

    def _worker_asr(self, bvid, page, model_size):
        def cb(msg, pct=None):
            self.after(0, self._on_progress, msg, pct)
        audio_path = None
        try:
            url = f"https://www.bilibili.com/video/{bvid}"
            if page > 1:
                url += f"?p={page}"
            audio_path = download_audio(url, f"{bvid}_p{page}", ASR_CACHE_DIR, cb)
            items = transcribe_audio(audio_path, model_size, cb)
        except Exception as e:
            self.after(0, self._on_error, "语音识别失败", e)
            return
        finally:
            if audio_path and os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except OSError:
                    pass
        self.after(0, self._on_subtitle_ok, items,
                   f"本地语音识别（{model_size}，结果可能有错字）")

    def _on_error(self, title, err):
        self.set_busy(False, "出错了")
        messagebox.showerror(title, str(err))

    def _on_subtitle_ok(self, items, lang):
        self.subtitle_items = items
        self.subtitle_lang = lang
        self.refresh_view()
        self.set_busy(False, f"完成：共 {len(items)} 条字幕（{lang or '未知语言'}）")

    # ---------------- 展示 / 导出 ----------------
    def _set_text(self, content):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        self.text.configure(state="disabled")

    def current_render(self):
        if not self.subtitle_items:
            return ""
        if self.view_var.get() == "ts":
            return render_with_timestamps(self.subtitle_items)
        return render_plain_text(self.subtitle_items)

    def refresh_view(self):
        if self.subtitle_items:
            self._set_text(self.current_render())

    def copy_all(self):
        content = self.current_render()
        if not content:
            messagebox.showinfo("提示", "当前没有可复制的字幕内容。")
            return
        self.clipboard_clear()
        self.clipboard_append(content)
        self.status_var.set("已复制到剪贴板")

    def export_txt(self):
        content = self.current_render()
        if not content:
            messagebox.showinfo("提示", "当前没有可导出的字幕内容。")
            return
        title = re.sub(r'[\\/:*?"<>|]', "_",
                       (self.video_info or {}).get("title", "subtitle"))[:60]
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            initialfile=f"{title}.txt",
            filetypes=[("文本文件", "*.txt")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as e:
            messagebox.showerror("导出失败", str(e))
            return
        self.status_var.set(f"已导出：{path}")


class NoSubtitleError(Exception):
    pass


if __name__ == "__main__":
    App().mainloop()
