"""Explicit offline checks run by the release pipeline, never on normal startup."""
import json
import multiprocessing as mp
import os
from pathlib import Path
import sys
import tempfile
import threading
import traceback
import wave

from .config import atomic_json
from .paths import BUNDLE_ROOT, DATA_ROOT, APP_ICON
from .version import VERSION


def _browser_check(output):
    try:
        import webview
        window = webview.create_window('文字稿 · 浏览器组件检查', html='<html><body>offline check</body></html>', hidden=True)
        def loaded():
            try:
                if window.evaluate_js('1 + 1') != 2:
                    raise RuntimeError('JavaScript check failed')
                output.put(dict(ok=True, renderer='edgechromium'))
            except Exception:
                output.put(dict(ok=False, error=traceback.format_exc()))
            finally:
                window.destroy()
        window.events.loaded += loaded
        def timeout():
            output.put(dict(ok=False, error='WebView2 initialization timed out'))
            window.destroy()
        timer = threading.Timer(45, timeout)
        timer.daemon = True
        timer.start()
        try:
            webview.start(gui='edgechromium', private_mode=True)
        finally:
            timer.cancel()
    except Exception:
        output.put(dict(ok=False, error=traceback.format_exc()))


def _self_test():
    import av
    from PIL import Image
    from .core.download import find_ffmpeg
    from .core.cloud import _audio_chunk, _crypt
    from .core.local_media import inspect_media
    from .core.textout import to_simplified
    from .services.recognizer import worker
    if not APP_ICON.is_file():
        raise RuntimeError('Bundled icon missing')
    if to_simplified('臺灣') != '台湾':
        raise RuntimeError('OpenCC dictionary missing')
    secret = b'offline-dpapi-check-not-a-key'
    if _crypt(_crypt(secret), decrypt=True) != secret:
        raise RuntimeError('DPAPI check failed')
    with tempfile.TemporaryDirectory() as folder:
        audio = Path(folder) / '中文 media.wav'
        with wave.open(str(audio), 'wb') as stream:
            stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            stream.writeframes(b'\0\0' * 16000)
        info = inspect_media(audio)
        if abs(info['duration'] - 1) > .01:
            raise RuntimeError('Media decoding check failed')
        ffmpeg = find_ffmpeg()
        if not ffmpeg or not Path(ffmpeg).is_file():
            raise RuntimeError('Bundled FFmpeg missing')
        if len(_audio_chunk(audio, 0, 1, ffmpeg)) < 32000:
            raise RuntimeError('FFmpeg conversion check failed')
        context = mp.get_context('spawn')
        inputs, outputs, cancelled = context.Queue(), context.Queue(), context.Event()
        cancelled.set()  # Exercises the real cloud worker without any HTTP/key access.
        process = context.Process(target=worker, args=(inputs, outputs, cancelled))
        process.start()
        try:
            inputs.put(dict(identity=('offline-check', 1), engine='cloud', audio=str(audio), checkpoint=str(Path(folder) / 'checkpoint.json')))
            event, identity, value = outputs.get(timeout=45)
            if event != 'cancelled':
                raise RuntimeError(f'Worker dispatch check failed: {event}')
            inputs.put(None)
            process.join(timeout=10)
            if process.is_alive() or process.exitcode != 0:
                raise RuntimeError('Worker shutdown failed')
        finally:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            process.close()
            for channel in (inputs, outputs):
                channel.cancel_join_thread()
                channel.close()
    Image.new('RGB', (2, 2))
    return dict(media=True, ffmpeg=True, opencc=True, pillow=True, dpapi=True,
                spawn_worker=True, cancel=True, worker_exit=True, av=av.__version__)


def _smoke_test():
    from .ui.batch_app import BatchApp
    app = BatchApp(autostart=False)
    app.withdraw()
    failures = []
    app.report_callback_exception = lambda *args: failures.append(str(args[1]))
    try:
        app.update_idletasks()
        app.settings()
        app.update_idletasks()
        app._save_settings()
        title = app.title()
    finally:
        app.close()
    if failures:
        raise RuntimeError('; '.join(failures))
    return dict(tk_window=True, settings=True, title=title, shutdown=True)


def run_check(args):
    destination = args.self_test or args.browser_self_test or args.smoke_test
    report = dict(version=VERSION, frozen=bool(getattr(sys, 'frozen', False)),
                  python=sys.version.split()[0], bundle=str(BUNDLE_ROOT), data=str(DATA_ROOT))
    try:
        if args.browser_self_test:
            context = mp.get_context('spawn')
            output = context.Queue()
            process = context.Process(target=_browser_check, args=(output,))
            process.start()
            try:
                result = output.get(timeout=60)
                if not result.get('ok'):
                    raise RuntimeError(result.get('error', 'Browser check failed'))
                report.update(result)
            finally:
                process.join(timeout=8)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
                process.close()
                output.cancel_join_thread()
                output.close()
        else:
            report.update(_self_test() if args.self_test else _smoke_test())
        report['ok'] = True
    except Exception:
        report.update(ok=False, error=traceback.format_exc())
    atomic_json(Path(destination).resolve(), report)
    return 0 if report['ok'] else 1
