"""Read-only runtime checks, available even if GUI bootstrap fails."""
import importlib.metadata
import sys
import tkinter
from .paths import PROJECT_ROOT, LOGS_DIR
from .core.models import model_is_ready, PUNCTUATION_PATH
from .core.asr import pick_device, selected_profile


def main():
    print("项目：", PROJECT_ROOT)
    print("Python：", sys.version)
    print("Tk：", tkinter.TkVersion)
    for package in ("yt-dlp", "faster-whisper", "ctranslate2", "sherpa-onnx", "opencc-python-reimplemented", "av"):
        try:
            print(package, importlib.metadata.version(package))
        except importlib.metadata.PackageNotFoundError:
            print(package, "未安装")
    print("设备：", pick_device())
    print("自动配置：", selected_profile())
    print("模型：", [(name, model_is_ready(name)) for name in ("medium", "small", "large-v3-turbo", "large-v3")])
    print("标点模型：", PUNCTUATION_PATH.exists())
    print("日志：", LOGS_DIR / "app.log")


if __name__ == "__main__":
    main()
