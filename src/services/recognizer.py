"""Spawn-safe worker: GPU libraries and the resident model stay out of Tk."""
import queue
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
            if request.get('engine') == 'cloud':
                from ..core.cloud import transcribe as cloud_transcribe
                result = cloud_transcribe(request['audio'], request['checkpoint'],
                    context=request.get('title',''), progress=lambda pct,msg: outputs.put(('progress',identity,(pct,msg))),
                    cancel=cancelled.is_set)
            else:
                from ..core.asr import transcribe
                result = transcribe(request["audio"], hotwords=request.get("title", ""),
                                cancel=cancelled.is_set, log=log,
                                progress=lambda pct, message: outputs.put(("progress", identity, (pct, message))))
            outputs.put(("result", identity, result))
        except JobCancelled:
            outputs.put(("cancelled", identity, None))
        except Exception as exc:
            outputs.put(("error", identity, f"识别失败：{type(exc).__name__}: {exc}"))
