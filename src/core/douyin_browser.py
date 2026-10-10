"""Task-cancellable WebView2 acquisition; a dedicated local Douyin profile.

Only the requested video's public playback record crosses the process pipe.
No general browser cookie database, debug port, or site-signature emulation.
"""
import json
import multiprocessing as mp
import threading
import time
import urllib.parse

from ..paths import PROJECT_ROOT
from .net import check_cancel

_SESSION_LOCK = threading.Lock()
_TIMEOUT = 300
_MAX_JSON = 4 * 1024 * 1024


def detail_response(url):
    parsed = urllib.parse.urlsplit(url)
    return (parsed.scheme == 'https' and parsed.hostname in ('www.douyin.com', 'www.iesdouyin.com')
            and parsed.path in ('/aweme/v1/web/aweme/detail/', '/web/api/v2/aweme/iteminfo/'))


def fetch_browser_video(video_id, cancel=None, progress=None):
    if not str(video_id).isdigit():
        raise ValueError('抖音视频编号无效')
    while not _SESSION_LOCK.acquire(timeout=.1):
        check_cancel(cancel)
    process = parent = child = stop = None
    try:
        check_cancel(cancel)
        if progress:
            progress('正在通过浏览器获取抖音播放信息')
        context = mp.get_context('spawn')
        stop = context.Event()
        parent, child = context.Pipe(duplex=False)
        process = context.Process(target=_browser_worker, args=(str(video_id), child, stop), daemon=True)
        process.start()
        child.close()
        deadline = time.monotonic() + _TIMEOUT + 30
        while time.monotonic() < deadline:
            check_cancel(cancel)
            if parent.poll(.1):
                try:
                    event, value = parent.recv()
                except EOFError:
                    break
                if event == 'result':
                    check_cancel(cancel)
                    return value
                if event == 'error':
                    raise RuntimeError(value)
                if event == 'progress' and progress:
                    progress(value)
            if not process.is_alive() and not parent.poll():
                break
        raise RuntimeError('抖音浏览器未取得播放信息；请完成窗口中的登录或验证后重试')
    finally:
        if process:
            stop.set()
            if process.pid is not None:
                process.join(timeout=3)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=3)
            process.close()
        for connection in (parent, child):
            if connection:
                connection.close()
        _SESSION_LOCK.release()


def _browser_worker(video_id, connection, stop):
    """WebView owns its main thread; Tk and task services never enter it."""
    import logging
    logging.getLogger('pywebview').setLevel(logging.CRITICAL)
    finished = threading.Event()
    send_lock = threading.Lock()

    def send(event, value):
        with send_lock:
            try:
                connection.send((event, value))
            except (BrokenPipeError, EOFError, OSError):
                finished.set()

    try:
        import webview
        webview.settings.update(ALLOW_FILE_URLS=False, OPEN_EXTERNAL_LINKS_IN_BROWSER=False,
                                IGNORE_SSL_ERRORS=False, REMOTE_DEBUGGING_PORT=None)
        window = webview.create_window('资料工具 · 抖音访问（需要时请登录或验证）',
                                      html='<html><body>正在加载抖音访问会话…</body></html>', hidden=True, width=1000, height=740)
        profile = PROJECT_ROOT / '.data' / 'douyin-browser'
        profile.mkdir(parents=True, exist_ok=True)
        ready = threading.Event()
        def initialized(sender, args):
            if args.IsSuccess:
                ready.set()
        def before_show():
            control = window.native.browser.webview
            control.CoreWebView2InitializationCompleted += initialized
            if control.CoreWebView2 is not None:
                ready.set()
        window.events.before_show += before_show

        def accept(data):
            from .douyin import find_video_record, playback_info
            record = find_video_record(data, video_id)
            if record and not finished.is_set():
                try:
                    result = playback_info(record, video_id)
                except (ValueError, TypeError):
                    return
                finished.set()
                send('result', result)

        def run():
            try:
                if not ready.wait(25):
                    send('error', 'WebView2 初始化超时；请运行环境诊断')
                    return
                from System import Action, Array, Byte
                from System.IO import Stream
                from System.Threading.Tasks import Task

                def response_received(sender, args):
                    if finished.is_set() or not detail_response(str(args.Request.Uri)) or args.Response.StatusCode != 200:
                        return

                    def body_ready(task):
                        if task.IsFaulted or task.IsCanceled or finished.is_set():
                            return
                        try:
                            stream = task.Result
                            buffer = Array.CreateInstance(Byte, 65536)
                            data = bytearray()
                            try:
                                while len(data) <= _MAX_JSON:
                                    count = stream.Read(buffer, 0, len(buffer))
                                    if not count:
                                        break
                                    data.extend(bytes(buffer)[:count])
                                if len(data) <= _MAX_JSON:
                                    accept(json.loads(data.decode('utf-8')))
                            finally:
                                stream.Dispose()
                        except Exception:
                            pass
                    args.Response.GetContentAsync().ContinueWith(Action[Task[Stream]](body_ready))

                # The pinned renderer exposes its WebView2 control here. Attach
                # on its UI thread before navigating to the actual video.
                control = window.native.browser.webview
                def attach():
                    control.CoreWebView2.WebResourceResponseReceived += response_received
                control.Invoke(Action(attach))
                window.load_url(f'https://www.douyin.com/video/{video_id}')
                start = time.monotonic()
                shown = False
                while not finished.wait(.5):
                    if stop.is_set():
                        finished.set()
                        break
                    elapsed = time.monotonic() - start
                    if elapsed >= 15 and not shown:
                        shown = True
                        send('progress', '抖音需要浏览器访问；如有登录或验证提示，请在弹出窗口完成')
                        window.show()
                    if elapsed >= _TIMEOUT:
                        send('error', '抖音访问等待超时；请完成登录或验证后重试，不支持私密、已删除视频或图集')
                        finished.set()
            except Exception as exc:
                # Do not log URLs with signatures, cookies, or exception bodies.
                send('error', f'抖音浏览器访问失败（{type(exc).__name__}）；请检查 WebView2 环境')
                finished.set()
            finally:
                window.destroy()

        def closed():
            if not finished.is_set():
                finished.set()
                send('error', '抖音访问窗口已关闭；任务保留，可重试')
        window.events.closed += closed
        webview.start(run, gui='edgechromium', private_mode=False, storage_path=str(profile))
    except Exception as exc:
        send('error', f'抖音浏览器无法启动（{type(exc).__name__}）；请运行环境诊断')
    finally:
        connection.close()
