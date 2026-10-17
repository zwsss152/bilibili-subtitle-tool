"""Spawn-safe cloud worker; network requests stay out of Tk."""
import traceback
from ..core.net import JobCancelled


def worker(inputs, outputs, cancelled):
    def log(message):
        outputs.put(("log", None, message))

    while True:
        request = inputs.get()
        if request is None:
            return
        identity = request["identity"]
        try:
            if request.get('engine', 'cloud') != 'cloud':
                raise ValueError('本地模型已移除，请重新加入在线任务')
            from ..core.cloud import transcribe, CloudClient
            result = transcribe(request['audio'], request['checkpoint'],
                context=request.get('title', ''), cancel=cancelled.is_set,
                client=CloudClient(models=request.get('cloud_models') or None,
                    diagnostic=lambda detail: outputs.put(('diagnostic', identity, detail))),
                progress=lambda pct, msg: outputs.put(('progress', identity, (pct, msg))))
            outputs.put(("result", identity, result))
        except JobCancelled:
            outputs.put(("cancelled", identity, None))
        except Exception as exc:
            from ..core.cloud import safe_diagnostic
            if request.get('engine', 'cloud') == 'cloud':
                outputs.put(('diagnostic', identity, dict(event='recognizer_exception',
                    exception=type(exc).__name__, traceback=safe_diagnostic(traceback.format_exc(), limit=8000))))
            outputs.put(("error", identity, f"识别失败：{type(exc).__name__}: {safe_diagnostic(exc)}"))
