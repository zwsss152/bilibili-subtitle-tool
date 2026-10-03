"""Spawn-safe worker: GPU libraries and the resident model stay out of Tk."""
import queue
from ..core.net import JobCancelled


def worker(inputs, outputs, cancelled):
    from ..core.asr import transcribe, prewarm

    def log(message):
        outputs.put(("log", None, message))

    prewarm(log)
    while True:
        request = inputs.get()
        if request is None:
            return
        identity = request["identity"]
        try:
            result = transcribe(request["audio"], hotwords=request.get("title", ""),
                                cancel=cancelled.is_set, log=log,
                                progress=lambda pct, message: outputs.put(("progress", identity, (pct, message))))
            outputs.put(("result", identity, result))
        except JobCancelled:
            outputs.put(("cancelled", identity, None))
        except Exception as exc:
            outputs.put(("error", identity, f"识别失败：{type(exc).__name__}: {exc}"))
