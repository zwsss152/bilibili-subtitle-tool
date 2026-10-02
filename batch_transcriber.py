# -*- coding: utf-8 -*-
"""
批量视频/播客转文字稿工具（B 站 / 抖音 / 机核 · 本地版）
==========================================================
用途：写文章收集资料。粘贴链接 → 自动入队 → 自动逐条转写 →
文字稿自动存入「文字稿/<主题>/」文件夹，全程无确认、无弹窗。

特性：
  1. 队列式批量处理，随时粘贴随时加入（支持一次粘贴多条链接）
  2. 主题文件夹管理：按主题名自动建目录，稿子自动归档
  3. 全自动：B 站优先 CC 字幕（秒出），无字幕自动本地语音识别，无人工干预；
     机核播客直接取官方音频直链转写，无需额外解析
  4. 提速：批处理推理 + 贪心解码 + 下载/识别双线程流水线
  5. 输出规范：简体中文、带标点、按段落组织、每段带起始时间戳

合规提醒：仅本地运行、仅供个人学习整理使用，请遵守平台用户协议与版权法规，
勿批量抓取传播他人受版权保护的内容。

运行：双击 批量转写.bat
"""

import datetime
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from tkinter import ttk, scrolledtext

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import bilibili_subtitle_tool as core  # 复用：链接解析 / 视频信息 / CC字幕 / 音频下载 / 模型管理

OUTPUT_ROOT = os.path.join(BASE_DIR, "文字稿")
AUDIO_CACHE = core.ASR_CACHE_DIR
APP_TITLE = "视频列表 · 批量视频转文字稿"
APP_ICON = os.path.join(BASE_DIR, "assets", "视频列表.ico")
# ---------------- 设计令牌（Devtool 工作台模式 · 浅色） ----------------
C = {
    "bg": "#F4F5F9",          # 应用底色
    "surface": "#FFFFFF",     # 卡片
    "border": "#E4E7F0",      # 卡片描边
    "text": "#1F2430",        # 主文字
    "text2": "#6B7280",       # 次要文字
    "accent": "#4F46E5",      # 强调色（与图标呼应）
    "accent_hover": "#4338CA",
    "accent_active": "#3730A3",
    "success": "#16A34A",     # 完成
    "error": "#DC2626",       # 失败
    "running": "#2563EB",     # 进行中
    "wait": "#8A8F9E",        # 排队/等待
    "select": "#E7E6FC",      # 选中行
}
FONT = "Microsoft YaHei UI"

DOUYIN_RE = re.compile(r"https?://[^\s，。\"'）)】]*douyin\.com[^\s，。\"'）)】]*")
GCORES_RE = re.compile(r"https?://(?:www\.)?gcores\.com/radios/(\d+)", re.IGNORECASE)


# ----------------------------------------------------------------------
# 机核网（gcores.com）播客支持
# ----------------------------------------------------------------------
# 机核页面是 React 动态渲染，页面 HTML 里拿不到音频；但它自己的接口是开放的：
#   GET https://www.gcores.com/gapi/v1/radios/{id}?include=media
#     → data.attributes.title / duration
#     → included[].attributes.audio = "xxxx.mp3"（音频文件名）
#  音频真实地址 = https://alioss.gcores.com/uploads/audio/{audio}
#    （该规律来自其前端 JS：`${alioss_url}/uploads/audio/${e}`，alioss_url = https://alioss.gcores.com）
#  该直链为阿里云 OSS 公共读，无需鉴权、支持 Range 断点续传。
GCORES_OSS = "https://alioss.gcores.com/uploads/audio/"


def fetch_gcores_radio(radio_id):
    """读取机核电台节目标题与音频直链。"""
    api = f"https://www.gcores.com/gapi/v1/radios/{radio_id}?include=media"
    try:
        data = core.http_get_json(api)
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


def download_direct_audio(url, dest_path, progress_cb):
    """流式下载音频直链（支持 Range 断点续传 + 进度回调）。"""
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    tmp = dest_path + ".part"
    done = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    headers = dict(core.UA_HEADERS)
    if done:
        headers["Range"] = f"bytes={done}-"
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"音频下载失败：HTTP {e.code}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"音频下载失败：{e.reason}")
    with resp:
        if done and resp.status != 206:  # 服务端不支持断点续传 → 重下
            done = 0
        total = int(resp.headers.get("Content-Length") or 0) + done
        with open(tmp, "ab" if done else "wb") as f:
            while True:
                chunk = resp.read(1 << 18)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // total
                    progress_cb(
                        f"下载音频中…… {done >> 20}MB / {total >> 20}MB（{pct}%）", pct
                    )
    if os.path.getsize(tmp) < 10000:
        raise RuntimeError("音频文件异常（体积过小），可能下载被中断。")
    os.replace(tmp, dest_path)
    return dest_path



# ----------------------------------------------------------------------
# 文本规范化：繁转简 + 分段
# ----------------------------------------------------------------------
try:
    import opencc
    _T2S = opencc.OpenCC("t2s")
except Exception:
    _T2S = None


def to_simplified(text):
    """繁体 → 简体（opencc 不可用时原样返回）。"""
    return _T2S.convert(text) if _T2S else text


_CJK = r"\u4e00-\u9fff"


_LAUGH = "哈呵嘿嘻嚯噢啊呀吖哇哦嗯哎嘿"


def collapse_repeats(text):
    """清理 whisper 的重复幻觉。

    长音频（尤其播客）里 whisper 偶尔会卡在解码循环里「刷屏」：
      「……这座被称作岳阳观测站的七层废墟处处处处处处处处……」
    两档处理：
      1) 同一个字连续 ≥5 次 → 收敛（笑声/语气词保留 3 个，其余收敛为 1 个）
      2) 同一短语（2~10 字）连续 ≥4 次 → 收敛为 1 次（如「好的好的好的好的」）
    阈值取偏保守，避免误伤「非常非常」这类正常口语强调。
    """
    text = re.sub(r"(.)\1{4,}",
                  lambda m: m.group(1) * (3 if m.group(1) in _LAUGH else 1),
                  text)
    text = re.sub(r"(.{2,10}?)\1{3,}", r"\1", text)
    return text


def normalize_text(text):
    """简体化 + 中文语境标点规范化（whisper 常输出半角逗号/句号）+ 去重复幻觉。"""
    t = to_simplified(text)
    # 紧邻中文的半角标点 → 全角（不影响英文句子和数字小数点）
    t = re.sub(rf"(?<=[{_CJK}])\s*,\s*", "，", t)
    t = re.sub(rf",(?=[{_CJK}])", "，", t)
    t = re.sub(rf"(?<=[{_CJK}])\?", "？", t)
    t = re.sub(rf"(?<=[{_CJK}])!", "！", t)
    t = re.sub(rf"(?<=[{_CJK}]);", "；", t)
    t = re.sub(rf"(?<=[{_CJK}]):", "：", t)
    t = re.sub(rf"(?<=[{_CJK}])\.\s*", "。", t)
    t = collapse_repeats(t)
    return t.strip()


def build_paragraphs(items, gap_seconds=2.0, max_chars=220):
    """把带时间轴的字幕条目合并为便于阅读的段落。

    items: [(from_s, to_s, text)]，按时间有序。
    返回 [(start_s, paragraph_text)]：
      - 说话停顿超过 gap_seconds → 换段
      - 段落超过 max_chars 且落在句末标点 → 换段
      - 兜底：超过 2*max_chars 强制换段，防止超长段
    """
    paras = []
    buf = ""
    buf_start = 0.0
    prev_end = None
    for frm, to, text in items:
        t = normalize_text(text)
        if not t:
            continue
        if not buf:
            buf_start = frm
        else:
            long_pause = prev_end is not None and (frm - prev_end) > gap_seconds
            at_sentence_end = bool(re.search(r"[。！？!?…]$", buf))
            over_len = len(buf) >= max_chars and at_sentence_end
            too_long = len(buf) >= max_chars * 2
            if long_pause or over_len or too_long:
                paras.append((buf_start, buf))
                buf = ""
                buf_start = frm
        buf += t
        prev_end = to
    if buf:
        paras.append((buf_start, buf))
    return paras


def sanitize_filename(name):
    return re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(name)).strip().strip(".")[:80] or "未命名"


def save_transcript(topic_dir, title, url, platform, method, paras):
    """把段落写成规范的文字稿 txt，返回文件路径。"""
    os.makedirs(topic_dir, exist_ok=True)
    base = sanitize_filename(title)
    path = os.path.join(topic_dir, base + ".txt")
    n = 2
    while os.path.exists(path):
        path = os.path.join(topic_dir, f"{base}_{n}.txt")
        n += 1
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    out = [
        f"# {title}",
        f"来源：{url}",
        f"平台：{platform}    提取方式：{method}    生成时间：{now}",
        "",
    ]
    for start, para in paras:
        out.append(f"[{core.fmt_ts(start)}] {para}")
        out.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out).strip() + "\n")
    return path


# ----------------------------------------------------------------------
# 提速版语音识别：批处理推理 + 贪心解码 + 模型常驻缓存
# ----------------------------------------------------------------------
_model_cache = {}
_model_lock = threading.Lock()


def get_asr_model(model_size):
    """模型只加载一次，常驻内存（批量处理时省去重复加载开销）。"""
    with _model_lock:
        if model_size not in _model_cache:
            from faster_whisper import WhisperModel
            model_dir = core.ensure_model(model_size, lambda m, p=None: None)
            wm = WhisperModel(
                model_dir, device="cpu", compute_type="int8",
                cpu_threads=os.cpu_count() or 4,
            )
            try:
                from faster_whisper import BatchedInferencePipeline
                wm = BatchedInferencePipeline(wm)  # CPU 上数倍的吞吐提升
            except ImportError:
                pass
            _model_cache[model_size] = wm
    return _model_cache[model_size]


def transcribe_fast(audio_path, model_size, progress_cb, beam_size=3):
    """离线转写，返回 [(from, to, text)]。

    相对旧实现（beam5 逐条解码）的提速点：
      - BatchedInferencePipeline(batch_size=16)：批量并行解码
      - beam_size 默认 3（实测均衡档 2.1x 提速且更准；beam1 极速档 3.6x）
      - condition_on_previous_text=False：省去长上下文注意力，且避免重复幻觉
      - vad_filter=True：跳过静音/纯音乐段，少做无效解码
    """
    model = get_asr_model(model_size)
    kwargs = dict(
        beam_size=beam_size,
        vad_filter=True,
        condition_on_previous_text=False,
        initial_prompt="以下是带有正确标点符号的简体中文语音转写。",
    )

    def run(vad):
        kw = dict(kwargs, vad_filter=vad)
        try:
            segments, info = model.transcribe(audio_path, batch_size=16, **kw)
        except TypeError:
            segments, info = model.transcribe(audio_path, **kw)  # 无批处理接口的回退
        duration = max(getattr(info, "duration", 0.0) or 1.0, 1.0)
        out = []
        for seg in segments:
            out.append((seg.start, seg.end, seg.text.strip()))
            progress_cb(min(int(seg.end * 100 / duration), 100))
        return out

    items = run(vad=True)
    if not items:
        items = run(vad=False)  # VAD 误杀弱人声时兜底
    if not items:
        raise RuntimeError("识别完成但未产生任何文字（音频可能无人声）。")
    return items


# ----------------------------------------------------------------------
# 任务与流水线
# ----------------------------------------------------------------------
class Job:
    def __init__(self, jid, kind, ref, raw):
        self.jid = jid
        self.kind = kind            # 'bilibili' | 'douyin' | 'gcores'
        self.ref = ref              # bilibili: (id_type, id_value)；douyin: 完整 URL；gcores: 电台 id
        self.raw = raw
        self.title = raw[:42]
        self.status = "排队中"      # 排队中/解析中/下载中/等待识别/识别中/完成/失败
        self.progress = 0
        self.detail = ""
        self.pages_total = 0
        self.pages_done = 0
        self.saved = []
        self.error = ""


class Pipeline:
    """双线程流水线：网络线程（解析/CC字幕/下载音频）+ 识别线程（CPU 转写）。

    两条线程并行：识别第 N 个视频时，网络线程已在下载第 N+1 个，
    批量总耗时 ≈ 转写耗时之和，不再叠加下载等待。
    """

    def __init__(self, get_topic_dir, get_model_size, get_cookie, log, get_beam_size=None):
        self.get_topic_dir = get_topic_dir
        self.get_model_size = get_model_size
        self.get_beam_size = get_beam_size or (lambda: 3)
        self.get_cookie = get_cookie
        self.log = log
        self.jobs = []
        self.lock = threading.Lock()
        self.asr_queue = queue.Queue()
        self.paused = threading.Event()     # 置位 = 暂停
        self.stop_flag = False
        self._jid = 0
        threading.Thread(target=self._net_loop, daemon=True).start()
        threading.Thread(target=self._asr_loop, daemon=True).start()

    # ---------------- 入队 ----------------
    def add_links(self, raw_text):
        """解析一段可能含多条链接的文本，返回 (added, skipped)。"""
        added, skipped = 0, 0
        for token in re.split(r"[\s，；;、]+", raw_text.strip()):
            if not token:
                continue
            m = DOUYIN_RE.search(token)
            g = GCORES_RE.search(token)
            if m:
                kind, ref, raw = "douyin", m.group(0), m.group(0)
            elif g:
                kind, ref, raw = "gcores", g.group(1), g.group(0)
            else:
                try:
                    kind, ref = "bilibili", core.parse_video_id(token)
                    raw = token
                except ValueError:
                    skipped += 1
                    continue
            with self.lock:
                dup = any(j.ref == ref and j.status not in ("失败",) for j in self.jobs)
                if dup:
                    skipped += 1
                    continue
                self._jid += 1
                self.jobs.append(Job(self._jid, kind, ref, raw))
            added += 1
        return added, skipped

    def snapshot(self):
        with self.lock:
            return list(self.jobs)

    def remove_jobs(self, jids):
        with self.lock:
            self.jobs = [j for j in self.jobs
                         if j.jid not in jids or j.status in ("下载中", "识别中", "解析中")]

    def clear_finished(self):
        with self.lock:
            self.jobs = [j for j in self.jobs if j.status not in ("完成", "失败")]

    # ---------------- 状态推进 ----------------
    def _prog(self, job, pct, stage_msg=""):
        if pct is not None:
            if job.pages_total <= 1:
                job.progress = pct
            else:
                frac = (job.pages_done + pct / 100.0) / max(job.pages_total, 1)
                job.progress = min(int(frac * 100), 100)
        if stage_msg:
            job.detail = stage_msg

    def _next_queued(self):
        with self.lock:
            for j in self.jobs:
                if j.status == "排队中":
                    j.status = "解析中"
                    return j
        return None

    def _page_finished(self, job, path, note):
        job.pages_done += 1
        job.saved.append(path)
        self._prog(job, 100, note)
        self.log(f"[#{job.jid}] {note} → {os.path.basename(path)}")
        if job.pages_done >= max(job.pages_total, 1):
            job.status = "完成"
            job.progress = 100
            # 有失败页时在界面上明确标出，避免被误当成全部成功
            job.detail = (f"部分失败（{job.error}）" if job.error
                          else os.path.dirname(path))

    def _page_failed(self, job, page_no, err):
        label = f"P{page_no}" if job.pages_total > 1 else "任务"
        job.error = f"{label}: {err}"
        self.log(f"[#{job.jid}] {label} 失败：{err}")
        # 单页失败不阻塞后续页；全部结束后若零产出则标失败
        job.pages_done += 1
        if job.pages_done >= max(job.pages_total, 1):
            job.status = "失败" if not job.saved else "完成"
            job.detail = job.error if not job.saved else f"部分失败：{job.error}"

    def _fail(self, job, err):
        job.status = "失败"
        job.error = str(err)
        job.detail = job.error
        self.log(f"[#{job.jid}] 失败：{err}")

    def _is_paused(self):
        return self.paused.is_set() and not self.stop_flag

    # ---------------- 网络线程 ----------------
    def _net_loop(self):
        while not self.stop_flag:
            if self._is_paused():
                time.sleep(0.3)
                continue
            job = self._next_queued()
            if job is None:
                time.sleep(0.3)
                continue
            try:
                if job.kind == "douyin":
                    self._handle_douyin(job)
                elif job.kind == "gcores":
                    self._handle_gcores(job)
                else:
                    self._handle_bilibili(job)
            except Exception as e:
                self._fail(job, e)

    def _handle_bilibili(self, job):
        id_type, id_value = job.ref
        cookie = self.get_cookie()
        job.detail = "获取视频信息"
        info = core.fetch_video_info(id_type, id_value, cookie)
        job.title = info["title"]
        pages = info["pages"]
        job.pages_total = len(pages)
        for p in pages:
            if self.stop_flag:
                return
            while self._is_paused():
                time.sleep(0.3)
            try:
                self._process_bilibili_page(job, info, p["page"], p["cid"], len(pages))
            except Exception as e:
                # 单页失败（如网络抖动/风控）只记该页，继续处理后续页
                self._page_failed(job, p["page"], e)

    def _process_bilibili_page(self, job, info, page_no, cid, total_pages):
        page_title = info["title"] if total_pages == 1 else f"{info['title']}_P{page_no}"
        url = f"https://www.bilibili.com/video/{info['bvid']}"
        if page_no > 1:
            url += f"?p={page_no}"
        self._prog(job, None, f"P{page_no}/{total_pages} 查字幕")

        # 1) CC 字幕：秒出，优先
        try:
            subs = core.fetch_subtitle_list(info["bvid"], cid, self.get_cookie())
        except Exception:
            subs = []
        chosen = None
        for s in subs:
            if "zh" in (s.get("lan") or ""):
                chosen = s
                break
        if chosen is None and subs:
            chosen = subs[0]
        if chosen:
            try:
                items = core.download_subtitle(chosen["subtitle_url"], self.get_cookie())
                paras = build_paragraphs(items)
                path = save_transcript(
                    self.get_topic_dir(), page_title, url, "B站",
                    f"CC字幕({chosen.get('lan', '')})", paras,
                )
                self._page_finished(job, path, f"P{page_no} CC字幕")
                return
            except Exception:
                pass  # 字幕下载失败则降级到语音识别

        # 2) 无 CC 字幕：下载音频（失败自动重试一次）→ 交给识别线程
        tag = f"{info['bvid']}_p{page_no}"
        audio = os.path.join(AUDIO_CACHE, tag + ".m4a")
        if not (os.path.exists(audio) and os.path.getsize(audio) > 10000):
            job.status = "下载中"
            last_err = None
            for attempt in range(2):
                try:
                    audio = core.download_audio(
                        url, tag, AUDIO_CACHE,
                        lambda m, pct=None: self._prog(job, pct, f"P{page_no} 下载音频"),
                    )
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(5)  # 网络抖动/风控后退一步再试
            if last_err is not None:
                raise last_err
        job.status = "等待识别"
        job.detail = f"P{page_no} 排队识别"
        self.asr_queue.put((job, page_no, page_title, url, audio, False))

    def _handle_gcores(self, job):
        """机核播客：接口取音频直链 → 直接流式下载（mp3）→ 交给识别线程。

        与 B 站/抖音不同，机核音频是 OSS 公共直链，不需要 yt-dlp 解析，
        也不需要转码成 m4a（faster-whisper 可直接解码 mp3），少一道工序。
        """
        radio_id = job.ref
        url = f"https://www.gcores.com/radios/{radio_id}"
        job.detail = "读取节目信息"
        info = fetch_gcores_radio(radio_id)
        job.title = info["title"]
        job.pages_total = 1
        # 文件名只由电台 id 决定 → 天然可缓存，重跑同一期无需重新下载
        audio = os.path.join(AUDIO_CACHE, f"gc_{radio_id}.mp3")
        if os.path.exists(audio) and os.path.getsize(audio) > 10000:
            self._prog(job, 100, "使用本地已缓存音频")
        else:
            job.status = "下载中"
            last_err = None
            for attempt in range(2):
                try:
                    download_direct_audio(
                        info["audio_url"], audio,
                        lambda m, pct=None: self._prog(job, pct, m),
                    )
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(5)  # 断点续传已保留，退一步再试
            if last_err is not None:
                raise last_err
        job.status = "等待识别"
        job.detail = "排队识别"
        self.asr_queue.put((job, 1, job.title, url, audio, True))

    def _handle_douyin(self, job):
        url = job.ref
        job.detail = "读取视频信息"
        title = core.fetch_media_title(url) or "抖音视频"
        job.title = title
        job.pages_total = 1
        tag = f"dy_{job.jid}_{int(time.time())}"
        job.status = "下载中"
        last_err = None
        for attempt in range(2):
            try:
                audio = core.download_audio(
                    url, tag, AUDIO_CACHE,
                    lambda m, pct=None: self._prog(job, pct, "下载音频"),
                )
                last_err = None
                break
            except Exception as e:
                last_err = e
                time.sleep(5)
        if last_err is not None:
            raise last_err
        job.status = "等待识别"
        job.detail = "排队识别"
        self.asr_queue.put((job, 1, title, url, audio, False))

    # ---------------- 识别线程 ----------------
    def _asr_loop(self):
        while not self.stop_flag:
            try:
                task = self.asr_queue.get(timeout=0.3)
            except queue.Empty:
                continue
            if self._is_paused():
                self.asr_queue.put(task)
                time.sleep(0.5)
                continue
            job, page_no, page_title, url, audio, keep = task
            pfx = f"P{page_no} " if job.pages_total > 1 else ""
            try:
                job.status = "识别中"
                model_size = self.get_model_size()
                items = transcribe_fast(
                    audio, model_size,
                    lambda pct: self._prog(job, pct, f"{pfx}识别中"),
                    beam_size=self.get_beam_size(),
                )
                paras = build_paragraphs(items)
                platform = {"douyin": "抖音", "gcores": "机核"}.get(job.kind, "B站")
                path = save_transcript(
                    self.get_topic_dir(), page_title, url, platform,
                    f"语音识别({model_size})", paras,
                )
                self._page_finished(job, path, f"{pfx}识别完成")
            except Exception as e:
                self._page_failed(job, page_no, e)
            finally:
                if not keep:  # 机核音频保留做缓存，其余用完即删
                    try:
                        os.remove(audio)
                    except OSError:
                        pass


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------
class BatchApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1000x720")
        self.minsize(880, 620)
        self.configure(bg=C["bg"])
        if os.path.exists(APP_ICON):
            try:
                self.iconbitmap(APP_ICON)
            except tk.TclError:
                pass

        self._setup_style()
        self.pipe = Pipeline(
            get_topic_dir=self.topic_dir,
            get_model_size=lambda: self.model_var.get() or "small",
            get_beam_size=lambda: 1 if self.beam_var.get().startswith("极速") else 3,
            get_cookie=lambda: self.cookie_var.get(),
            log=self._log,
        )
        self._build_ui()
        self._refresh_loop()

    # ---------------- 样式系统 ----------------
    def _setup_style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")  # 可定制性最好的内置主题
        except tk.TclError:
            pass
        st.configure(".", background=C["bg"], foreground=C["text"],
                     font=(FONT, 10), borderwidth=0)
        st.configure("TFrame", background=C["bg"])
        st.configure("Card.TFrame", background=C["surface"], relief="solid",
                     borderwidth=1, bordercolor=C["border"])
        # 卡片内部的普通行：只继承白底，无边框（避免嵌套灰线造成「假输入框」）
        st.configure("Row.TFrame", background=C["surface"], relief="flat",
                     borderwidth=0)
        st.configure("TLabel", background=C["bg"], foreground=C["text"])
        st.configure("Card.TLabel", background=C["surface"], foreground=C["text"])
        st.configure("CardTitle.TLabel", background=C["surface"], foreground=C["text"],
                     font=(FONT, 10, "bold"))
        st.configure("Hint.TLabel", background=C["surface"], foreground=C["text2"],
                     font=(FONT, 9))
        st.configure("Header.TLabel", background=C["bg"], foreground=C["text"],
                     font=(FONT, 15, "bold"))
        st.configure("Sub.TLabel", background=C["bg"], foreground=C["text2"],
                     font=(FONT, 9))
        st.configure("Status.TLabel", background="#EDEFF5", foreground=C["text2"],
                     font=(FONT, 9), padding=(10, 5))
        st.configure("TButton", padding=(14, 8), font=(FONT, 10),
                     background=C["surface"], bordercolor=C["border"])
        st.map("TButton", background=[("active", "#EEF0F7")])
        st.configure("Accent.TButton", padding=(18, 9), font=(FONT, 10, "bold"),
                     background=C["accent"], foreground="#FFFFFF", borderwidth=0)
        st.map("Accent.TButton",
               background=[("pressed", C["accent_active"]), ("active", C["accent_hover"])],
               foreground=[("disabled", "#FFFFFF")])
        st.configure("TEntry", padding=6, fieldbackground=C["surface"],
                     bordercolor=C["border"])
        st.configure("TCombobox", padding=4, fieldbackground=C["surface"])
        st.configure("TCheckbutton", background=C["surface"])
        st.configure("Treeview", background=C["surface"], fieldbackground=C["surface"],
                     foreground=C["text"], rowheight=30, font=(FONT, 10),
                     borderwidth=1, relief="solid")
        st.configure("Treeview.Heading", background=C["bg"], foreground=C["text2"],
                     font=(FONT, 9, "bold"), padding=(8, 6), relief="flat")
        st.map("Treeview",
               background=[("selected", C["select"])],
               foreground=[("selected", C["text"])])

    def _card(self, title, expand=False, side="top"):
        card = ttk.Frame(self, style="Card.TFrame", padding=12)
        card.pack(fill="both" if expand else "x", expand=expand,
                  padx=14, pady=(0, 8), side=side)
        if title:
            ttk.Label(card, text=title, style="CardTitle.TLabel").pack(anchor="w", pady=(0, 8))
        return card

    # ---------------- UI ----------------
    def _build_ui(self):
        # 状态栏先 pack（side=bottom 先占位，避免被上方扩展组件挤掉）
        self.status_var = tk.StringVar(value="就绪 · 先填主题，再粘贴链接加入队列，全自动处理")
        ttk.Label(self, textvariable=self.status_var, style="Status.TLabel",
                  anchor="w").pack(fill="x", side="bottom")

        # 运行日志也先占住底部（否则会被上方 expand 的工作区把高度吃光）
        card3 = self._card("运行日志", side="bottom")
        self.log_text = scrolledtext.ScrolledText(
            card3, height=5, wrap="word", font=("Consolas", 9),
            bg="#FAFBFD", fg=C["text2"], relief="solid", bd=1,
        )
        self.log_text.pack(fill="x")
        self.log_text.configure(state="disabled")

        # 头部：产品名 + 识别选项（右置）
        header = ttk.Frame(self)
        header.pack(fill="x", padx=16, pady=(14, 10))
        ttk.Label(header, text="视频列表", style="Header.TLabel").pack(side="left")
        ttk.Label(header, text="批量视频转文字稿 · B 站 / 抖音 / 机核",
                  style="Sub.TLabel").pack(side="left", padx=(10, 0), pady=(6, 0))
        opts = ttk.Frame(header)
        opts.pack(side="right")
        ttk.Label(opts, text="模型").pack(side="left")
        self.model_var = tk.StringVar(value="small")
        ttk.Combobox(opts, textvariable=self.model_var, width=7,
                     values=core.ASR_MODELS, state="readonly").pack(side="left", padx=(4, 12))
        ttk.Label(opts, text="模式").pack(side="left")
        self.beam_var = tk.StringVar(value="均衡(快2倍·更准)")
        ttk.Combobox(opts, textvariable=self.beam_var, width=17,
                     values=["均衡(快2倍·更准)", "极速(快3.6倍·略糙)"],
                     state="readonly").pack(side="left", padx=(4, 0))

        # 卡片 1：新建任务
        card1 = self._card("新建任务")
        row1 = ttk.Frame(card1, style="Row.TFrame")
        row1.pack(fill="x", pady=(0, 8))
        ttk.Label(row1, text="主题", style="Card.TLabel", width=5, anchor="e").pack(side="left")
        self.topic_var = tk.StringVar()
        topic_entry = ttk.Entry(row1, textvariable=self.topic_var, width=22)
        topic_entry.pack(side="left", padx=(6, 6))
        topic_entry.bind("<Return>", lambda e: self._confirm_topic())
        ttk.Button(row1, text="创建/使用该主题", command=self._confirm_topic).pack(side="left")
        self.outdir_var = tk.StringVar(value="输出到：文字稿\\未分类")
        ttk.Label(row1, textvariable=self.outdir_var, style="Hint.TLabel").pack(side="left", padx=12)
        self.show_cookie = tk.BooleanVar(value=False)
        ttk.Checkbutton(row1, text="B 站 Cookie（可选）",
                        variable=self.show_cookie, command=self._toggle_cookie).pack(side="right")

        row2 = ttk.Frame(card1, style="Row.TFrame")
        row2.pack(fill="x")
        ttk.Label(row2, text="链接", style="Card.TLabel", width=5, anchor="e").pack(side="left")
        self.link_var = tk.StringVar()
        link_entry = ttk.Entry(row2, textvariable=self.link_var)
        link_entry.pack(side="left", fill="x", expand=True, padx=(6, 6), ipady=3)
        link_entry.bind("<Return>", lambda e: self.add_links())
        ttk.Button(row2, text="加入队列", style="Accent.TButton",
                   command=self.add_links).pack(side="left")
        self.cookie_var = tk.StringVar()
        self.cookie_entry = ttk.Entry(card1, textvariable=self.cookie_var)

        # 卡片 2：处理队列（主工作区，占据剩余空间）
        card2 = self._card("处理队列", expand=True)
        ctl = ttk.Frame(card2, style="Row.TFrame")
        ctl.pack(fill="x", pady=(0, 8))
        self.pause_btn = ttk.Button(ctl, text="暂停", command=self.toggle_pause)
        self.pause_btn.pack(side="left")
        ttk.Button(ctl, text="移除选中", command=self.remove_selected).pack(side="left", padx=6)
        ttk.Button(ctl, text="清空已完成", command=self.clear_finished).pack(side="left")
        ttk.Button(ctl, text="打开输出文件夹", command=self.open_output).pack(side="right")

        tree_frame = ttk.Frame(card2, style="Row.TFrame")
        tree_frame.pack(fill="both", expand=True)
        cols = ("status", "title", "progress", "detail")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings", height=9)
        self.tree.heading("status", text="状态")
        self.tree.heading("title", text="标题 / 链接")
        self.tree.heading("progress", text="进度")
        self.tree.heading("detail", text="说明")
        self.tree.column("status", width=90, anchor="center")
        self.tree.column("title", width=380)
        self.tree.column("progress", width=70, anchor="center")
        self.tree.column("detail", width=320)
        sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        # 状态语义色：成功绿 / 失败红 / 进行中蓝 / 等待灰
        self.tree.tag_configure("done", foreground=C["success"])
        self.tree.tag_configure("fail", foreground=C["error"])
        self.tree.tag_configure("run", foreground=C["running"])
        self.tree.tag_configure("wait", foreground=C["wait"])

    def _toggle_cookie(self):
        if self.show_cookie.get():
            self.cookie_entry.pack(fill="x", pady=(8, 0))
        else:
            self.cookie_entry.pack_forget()

    # ---------------- 行为 ----------------
    def topic_dir(self):
        name = sanitize_filename(self.topic_var.get().strip() or "未分类")
        d = os.path.join(OUTPUT_ROOT, name)
        os.makedirs(d, exist_ok=True)
        return d

    def _confirm_topic(self):
        d = self.topic_dir()
        name = sanitize_filename(self.topic_var.get().strip() or "未分类")
        # 只显示短路径（完整路径写入日志，避免长路径挤压布局）
        self.outdir_var.set(f"输出到：文字稿\\{name}")
        self._log(f"主题文件夹已就绪：{d}")

    def add_links(self):
        raw = self.link_var.get().strip()
        if not raw:
            return
        added, skipped = self.pipe.add_links(raw)
        if added:
            self.link_var.set("")
            self._log(f"加入队列 {added} 条" + (f"，跳过 {skipped} 条（重复或无法识别）" if skipped else ""))
        else:
            self.status_var.set(
                "未识别到有效链接（支持 B 站链接/BV 号/av 号/抖音分享链接/机核 radios 链接）"
            )

    def toggle_pause(self):
        if self.pipe.paused.is_set():
            self.pipe.paused.clear()
            self.pause_btn.configure(text="暂停")
            self.status_var.set("已继续")
        else:
            self.pipe.paused.set()
            self.pause_btn.configure(text="继续")
            self.status_var.set("已暂停（进行中的任务会完成当前步骤后停下）")

    def remove_selected(self):
        jids = {int(iid) for iid in self.tree.selection()}
        if jids:
            self.pipe.remove_jobs(jids)
            for iid in self.tree.selection():
                self.tree.delete(iid)

    def clear_finished(self):
        self.pipe.clear_finished()

    def open_output(self):
        try:
            os.startfile(self.topic_dir())  # Windows
        except OSError as e:
            self.status_var.set(f"打开文件夹失败：{e}")

    def _log(self, msg):
        def append():
            self.log_text.configure(state="normal")
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            self.log_text.insert("end", f"[{ts}] {msg}\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.after(0, append)

    # ---------------- 刷新 ----------------
    def _refresh_loop(self):
        jobs = self.pipe.snapshot()
        seen = set(self.tree.get_children())
        done = fail = 0
        for j in jobs:
            iid = str(j.jid)
            seen.discard(iid)
            values = (j.status, j.title, f"{j.progress}%", j.detail or j.error)
            if j.status == "完成":
                tag = "done"
            elif j.status == "失败":
                tag = "fail"
            elif j.status in ("解析中", "下载中", "识别中"):
                tag = "run"
            else:
                tag = "wait"
            if self.tree.exists(iid):
                self.tree.item(iid, values=values, tags=(tag,))
            else:
                self.tree.insert("", "end", iid=iid, values=values, tags=(tag,))
            if j.status == "完成":
                done += 1
            elif j.status == "失败":
                fail += 1
        for iid in seen:
            self.tree.delete(iid)
        total = len(jobs)
        if total:
            self.status_var.set(
                f"共 {total} 个任务 · 完成 {done} · 失败 {fail} · 处理中/排队 {total - done - fail}"
            )
        self.after(300, self._refresh_loop)


if __name__ == "__main__":
    BatchApp().mainloop()
