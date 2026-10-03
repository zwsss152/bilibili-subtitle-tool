"""One cached recognition model per worker, with explicit device and OOM fallback."""
import gc
import glob
import os
import site
import time
from ..config import read_json
from ..paths import PROFILE_PATH
from .models import model_is_ready, model_path, ensure_model
from .net import JobCancelled, check_cancel

ASR_MODELS = ("tiny", "small", "medium", "large-v3-turbo", "large-v3")
ASR_AVAILABLE = True
_DLL_HANDLES = []
_DEVICE = None
_MODEL = None
_KEY = None
DEFAULT_PROFILE = dict(model="large-v3-turbo", device="cuda", compute_type="float16", batch_size=8, beam_size=5,
                       review_chinese=True)


def _add_nvidia_dll_dirs():
    if os.name != "nt" or _DLL_HANDLES:
        return
    for root in site.getsitepackages():
        for directory in glob.glob(os.path.join(root, "nvidia", "*", "bin")):
            _DLL_HANDLES.append(os.add_dll_directory(directory))
            os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")


def pick_device():
    global _DEVICE
    if _DEVICE is None:
        _add_nvidia_dll_dirs()
        try:
            import ctranslate2
            _DEVICE = ("cuda", "float16") if ctranslate2.get_cuda_device_count() else ("cpu", "int8")
        except Exception:
            _DEVICE = ("cpu", "int8")
    return _DEVICE


def is_gpu():
    return pick_device()[0] == "cuda"


def selected_profile():
    candidate = read_json(PROFILE_PATH, DEFAULT_PROFILE)
    result = {**DEFAULT_PROFILE, **(candidate if isinstance(candidate, dict) else {})}
    if not model_is_ready(result["model"]):
        result["model"] = next((name for name in ("medium", "small", "tiny") if model_is_ready(name)), "small")
    if not is_gpu():
        result.update(device="cpu", compute_type="int8", batch_size=4)
    return result


def get_asr_model(model_size, log=None, device=None, compute_type=None):
    global _MODEL, _KEY, _DEVICE
    _add_nvidia_dll_dirs()
    device = device or pick_device()[0]
    compute_type = compute_type or ("float16" if device == "cuda" else "int8")
    key = model_size, device, compute_type
    if _KEY == key and _MODEL is not None:
        return _MODEL
    _MODEL, _KEY = None, None
    gc.collect()
    from faster_whisper import WhisperModel, BatchedInferencePipeline
    model = WhisperModel(str(model_path(model_size)), device=device, compute_type=compute_type,
                         cpu_threads=8, num_workers=1, local_files_only=True)
    _MODEL = BatchedInferencePipeline(model)
    _KEY = key
    _DEVICE = device, compute_type
    return _MODEL


def _out_of_memory(error):
    message = str(error).lower()
    return any(word in message for word in ("out of memory", "cuda_error_out_of_memory", "failed to allocate"))


def transcribe(audio_path, profile=None, progress=lambda *a: None, cancel=None, hotwords="", log=lambda *a: None):
    profile = dict(profile or selected_profile())
    check_cancel(cancel)
    if not model_is_ready(profile["model"]):
        try:
            ensure_model(profile["model"], lambda msg, pct=None: progress(pct or 0, msg), cancel)
        except JobCancelled:
            raise
        except Exception as exc:
            fallback = next((name for name in ('medium', 'small', 'tiny') if model_is_ready(name)), None)
            if not fallback:
                raise
            log(f"模型下载失败（{exc}），使用已就绪的 {fallback}")
            profile['model'] = fallback
    started = time.perf_counter()
    while True:
        check_cancel(cancel)
        try:
            model = get_asr_model(profile["model"], device=profile["device"], compute_type=profile["compute_type"])
            options = dict(beam_size=profile.get("beam_size", 3), vad_filter=True,
                           repetition_penalty=1.05, no_repeat_ngram_size=0,
                           initial_prompt="以下是普通话访谈，请准确记录原话、数字和专有名词，并使用简体中文标点。",
                           language="zh", task="transcribe")
            if hotwords:
                options["hotwords"] = hotwords[:120]
            if profile.get('legacy_settings'):
                options.update(repetition_penalty=1.15, no_repeat_ngram_size=4,
                    initial_prompt='以下是一段普通话对话的转写，话题可能涉及人工智能、游戏、科技等，请输出带正确标点符号的简体中文。')
                options.pop('language')
                options['language_detection_segments'] = 3

            def run(vad):
                options["vad_filter"] = vad
                segments, information = model.transcribe(str(audio_path), batch_size=profile["batch_size"], **options)
                output = []
                for segment in segments:
                    check_cancel(cancel)
                    output.append(dict(start=float(segment.start), end=float(segment.end), text=segment.text.strip(),
                                       avg_logprob=float(segment.avg_logprob), no_speech_prob=float(segment.no_speech_prob)))
                    limit = 79 if profile.get('review_chinese') else 99
                    progress(min(limit, int(segment.end * limit / max(information.duration, 1))), "识别中")
                return output, information

            output, information = run(True)
            if not output:
                output, information = run(False)
            if not output:
                raise RuntimeError("识别没有产生文字，音频可能无人声")
            check_cancel(cancel)
            review_info = None
            if profile.get('review_chinese'):
                from .chinese_review import review
                output, review_info = review(audio_path, output, progress, cancel, log)
            return dict(segments=output, recognition={**profile, "duration": information.duration,
                         "language": information.language, "chinese_review": review_info,
                         "elapsed_seconds": time.perf_counter() - started})
        except JobCancelled:
            raise
        except Exception as exc:
            if profile["device"] != "cuda":
                raise
            gpu_failure = _out_of_memory(exc) or any(term in str(exc).lower() for term in ('cuda', 'gpu', 'cublas', 'cudnn'))
            if not gpu_failure:
                raise
            if _out_of_memory(exc) and profile["batch_size"] > 1:
                profile["batch_size"] = max(1, profile["batch_size"] // 2)
                log(f"显存不足，减小识别批次为 {profile['batch_size']}")
                continue
            log(f"GPU 识别不可用（{type(exc).__name__}: {exc}），回退 CPU")
            profile.update(device="cpu", compute_type="int8", batch_size=4)


def prewarm(log=lambda *args: None):
    profile = selected_profile()
    if model_is_ready(profile["model"]):
        try:
            get_asr_model(profile["model"], device=profile["device"], compute_type=profile["compute_type"])
            log(f"识别模型已就绪：{profile['model']} · {profile['device'].upper()}")
        except Exception as exc:
            log(f"模型预热失败，处理任务时将重试（{type(exc).__name__}）")
    if profile.get('review_chinese'):
        from .models import sense_voice_is_ready
        if sense_voice_is_ready():
            try:
                from .chinese_review import engine
                engine()
                log('本地中文语音复核模型已就绪')
            except Exception as exc:
                log(f'中文复核预热失败，处理任务时将重试（{type(exc).__name__}）')
