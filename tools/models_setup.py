"""Install only the models used by the local acceptance benchmark."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.core.models import ensure_model, ensure_punctuation

last = ""
def progress(message, percentage=None):
    global last
    key = message.split("：")[0] + str((percentage or 0) // 10)
    if key != last:
        print(message, flush=True)
        last = key


if __name__ == "__main__":
    ensure_punctuation(progress)
    for name in (sys.argv[1:] or ["large-v3-turbo", "large-v3"]):
        print(f"Preparing {name}", flush=True)
        print(ensure_model(name, progress), flush=True)
