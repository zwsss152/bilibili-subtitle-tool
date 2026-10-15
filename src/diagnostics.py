"""Read-only diagnostics for the cloud desktop runtime."""
import importlib.metadata
import sys
import tkinter
from .paths import PROJECT_ROOT, LOGS_DIR
from .core.cloud import configured, ASR_MODEL, EDITOR_MODEL, REVIEW_MODEL
from .core.download import find_ffmpeg

def main():
    print('项目：', PROJECT_ROOT)
    print('Python：', sys.version)
    print('Tk：', tkinter.TkVersion)
    print('识别与整理：阿里云百炼（北京）')
    print('在线密钥已配置：', configured())
    print('云端模型：', ASR_MODEL, EDITOR_MODEL, REVIEW_MODEL)
    for package in ('yt-dlp', 'pywebview', 'pythonnet', 'opencc-python-reimplemented', 'av', 'imageio-ffmpeg'):
        try:
            print(package, importlib.metadata.version(package))
        except importlib.metadata.PackageNotFoundError:
            print(package, '未安装')
    print('音频转换程序：', find_ffmpeg() or '未安装')
    print('日志：', LOGS_DIR / 'app.log')

if __name__ == '__main__':
    main()
