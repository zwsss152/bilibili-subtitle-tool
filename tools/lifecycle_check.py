"""Check actual spawned worker, pause, exit and unfinished queue restoration."""
from pathlib import Path
import queue
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.services.scheduler import Scheduler
from src.paths import PROJECT_ROOT
from src.config import atomic_json


def main():
    state = PROJECT_ROOT / 'tests' / '.artifacts' / 'lifecycle.json'
    state.unlink(missing_ok=True)
    scheduler = Scheduler(state_path=state, autostart=False)
    scheduler.paused.set()
    scheduler.enqueue('BV17x411w7KC', '生命周期验收', '')
    scheduler.start()
    process = scheduler.process
    deadline = time.monotonic()+30
    warmed = False
    try:
        while time.monotonic() < deadline:
            try:
                _, message = scheduler.events.get(timeout=.5)
                print(message, flush=True)
                if '已就绪' in message:
                    warmed = True
                    break
            except queue.Empty:
                pass
        paused = scheduler.jobs[0].status == '排队中' and not scheduler.jobs[0].pages
    finally:
        scheduler.shutdown()
    restored = Scheduler(state_path=state, autostart=False)
    evidence = dict(prewarm_local_only=warmed, pause_prevented_dispatch=paused,
                    worker_closed=scheduler.process is None,
                    unfinished_restored=restored.jobs[0].status == '排队中')
    atomic_json(PROJECT_ROOT/'benchmarks'/'lifecycle.json',evidence)
    assert all(evidence.values()), evidence
    print(evidence)


if __name__ == '__main__':
    main()
