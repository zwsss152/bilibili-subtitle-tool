"""Single-instance application bootstrap and visible startup diagnostics."""
import logging
from logging.handlers import RotatingFileHandler
import multiprocessing
import os
import sys
import traceback
from .paths import PROJECT_ROOT, LOGS_DIR, QUEUE_STATE_PATH, initialize

_INSTANCE = None


def acquire_instance():
    global _INSTANCE
    import msvcrt
    _INSTANCE = (PROJECT_ROOT / ".instance.lock").open("a+b")
    if os.fstat(_INSTANCE.fileno()).st_size == 0:
        _INSTANCE.write(b"0")
        _INSTANCE.flush()
    _INSTANCE.seek(0)
    try:
        msvcrt.locking(_INSTANCE.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        _INSTANCE.close()
        _INSTANCE = None
        return False


def setup_logging():
    handler = RotatingFileHandler(LOGS_DIR / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger("transcripts")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)


def main():
    multiprocessing.freeze_support()
    import argparse
    parser = argparse.ArgumentParser(description='文字稿')
    parser.add_argument('--diagnostics', action='store_true')
    parser.add_argument('--self-test', metavar='REPORT')
    parser.add_argument('--browser-self-test', metavar='REPORT')
    parser.add_argument('--smoke-test', metavar='REPORT')
    args = parser.parse_args()
    if args.self_test or args.browser_self_test or args.smoke_test:
        from .packaging_checks import run_check
        raise SystemExit(run_check(args))
    if args.diagnostics:
        from .diagnostics import show_diagnostics
        show_diagnostics()
        return
    try:
        initialize()
        setup_logging()
        if not acquire_instance():
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, "文字稿工具已经在运行，请使用已打开的窗口。", "文字稿", 0x40)
            return
        from .core.documents import migrate_records
        from .core.rename import recover
        # A pending transaction can still own a legacy sidecar; recover it first.
        recover(QUEUE_STATE_PATH, logging.getLogger('transcripts').info)
        migrate_records(logging.getLogger('transcripts').info)
        from .core.library_migration import migrate_library
        migrate_library(QUEUE_STATE_PATH, logging.getLogger('transcripts').info)
        from .ui.batch_app import BatchApp
        app = BatchApp()
        app.mainloop()
    except Exception:
        message = traceback.format_exc()
        logging.getLogger("transcripts").error(message)
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, f"启动失败。可从开始菜单打开“文字稿 · 环境诊断”。\n日志：{LOGS_DIR / 'app.log'}", "文字稿 · 启动诊断", 0x10)
        if sys.stderr:
            print(message, file=sys.stderr)
        raise SystemExit(1)
    finally:
        if _INSTANCE:
            _INSTANCE.close()


if __name__ == "__main__":
    main()
