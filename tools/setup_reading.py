"""Install the pinned official model/runtime entirely inside this checkout."""
from pathlib import Path
import sys
import time
import argparse
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.core.editor_model import ensure_resources

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--model',choices=('qwen3-8b',))
    args=parser.parse_args()
    last = [0]
    def progress(message, pct=None):
        if time.monotonic() - last[0] > 5:
            print(message, flush=True)
            last[0] = time.monotonic()
    ensure_resources(progress,model_id=args.model)
    print('READY', flush=True)
