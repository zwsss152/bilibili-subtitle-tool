"""Real platform queue, resident process, saved output and restart verification."""
from pathlib import Path
import json
import queue
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.services.scheduler import Scheduler
from src.paths import PROJECT_ROOT, ASR_CACHE_DIR
from src.config import load_config, atomic_json


def main():
    state = PROJECT_ROOT / 'benchmarks' / 'integration-queue.json'
    scheduler = Scheduler(state_path=state, autostart=False)
    if not scheduler.jobs:
        scheduler.enqueue('https://www.gcores.com/radios/196305', '验收稿件', load_config()['cookie'])
        scheduler.enqueue('https://www.bilibili.com/video/BV1wfLUzwEUv', '验收稿件', load_config()['cookie'])
    started = time.monotonic()
    scheduler.start()
    try:
        while time.monotonic()-started < 900:
            try:
                event, value = scheduler.events.get(timeout=0.5)
                print(value, flush=True)
            except queue.Empty:
                pass
            if all(j.status in ('完成','失败','部分失败') for j in scheduler.jobs):
                break
        snapshot = scheduler.snapshot()
    finally:
        scheduler.shutdown()
    restored = Scheduler(state_path=state, autostart=False)
    evidence = dict(elapsed_seconds=time.monotonic()-started, tasks=snapshot,
                    worker_stopped=scheduler.process is None,
                    restored_statuses=[j.status for j in restored.jobs],
                    remaining_audio=list(str(p.relative_to(PROJECT_ROOT)) for p in ASR_CACHE_DIR.rglob('*') if p.is_file()))
    atomic_json(PROJECT_ROOT / 'benchmarks' / 'integration.json', evidence)
    print(json.dumps(evidence, ensure_ascii=False), flush=True)
    if any(j.status != '完成' for j in restored.jobs):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
