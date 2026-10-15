"""Read-only diagnostics for the cloud desktop runtime."""
import importlib.metadata
import sys
import tkinter
from .paths import PROJECT_ROOT, LOGS_DIR
from .core.cloud import configured, model_settings
from .core.download import find_ffmpeg
from .version import VERSION

def main():
    print('版本：', VERSION)
    print('项目：', PROJECT_ROOT)
    print('Python：', sys.version)
    print('Tk：', tkinter.TkVersion)
    print('识别与整理：阿里云百炼（北京）')
    print('在线密钥已配置：', configured())
    print('所选云端模型：', model_settings())
    for package in ('yt-dlp', 'pywebview', 'pythonnet', 'opencc-python-reimplemented', 'av', 'imageio-ffmpeg'):
        try:
            print(package, importlib.metadata.version(package))
        except importlib.metadata.PackageNotFoundError:
            print(package, '未安装')
    print('音频转换程序：', find_ffmpeg() or '未安装')
    print('日志：', LOGS_DIR / 'app.log')


def show_diagnostics():
    import contextlib
    import io
    import tkinter as tk
    from tkinter.scrolledtext import ScrolledText
    import os
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        main()
    content = stream.getvalue()
    (LOGS_DIR / 'diagnostics.txt').write_text(content, encoding='utf-8')
    root = tk.Tk()
    root.title(f'文字稿 · 环境诊断 · {VERSION}')
    root.geometry('760x500')
    text = ScrolledText(root, wrap='word', font=('Microsoft YaHei UI', 10))
    text.pack(fill='both', expand=True, padx=12, pady=12)
    text.insert('1.0', content)
    text.configure(state='disabled')
    tk.Button(root, text='打开日志文件夹', command=lambda: os.startfile(LOGS_DIR)).pack(pady=(0, 12))
    root.mainloop()

if __name__ == '__main__':
    main()
