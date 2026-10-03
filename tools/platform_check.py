"""Live metadata, short redirect and selected/all-page platform checks."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.core import bilibili
from src.core.links import parse_source, resolve_source
from src.services.scheduler import Scheduler
from src.config import atomic_json, load_config
from src.paths import PROJECT_ROOT


def main():
    results = {}
    for name, url in [('short', 'https://b23.tv/BV17x411w7KC'),
                      ('selected', 'https://www.bilibili.com/video/BV1sHU9BmEne?p=2'),
                      ('all_pages', 'https://www.bilibili.com/video/BV1sHU9BmEne'),
                      ('subtitles', 'https://www.bilibili.com/video/BV12N4y1M7rh')]:
        scheduler = Scheduler(state_path=PROJECT_ROOT/'benchmarks'/f'platform-{name}-queue.json', autostart=False)
        try:
            source = resolve_source(parse_source(url))
            scheduler.enqueue(source.url, '验收稿件', load_config()['cookie'])
            job = scheduler.jobs[0]
            scheduler._resolve(job)
            result = dict(url=url, resolved=source.url, title=job.title, pages=[p.number for p in job.pages])
            if name == 'subtitles':
                info = scheduler._ensure_info(job)
                subs = bilibili.fetch_subtitle_list(info['bvid'], info['pages'][0]['cid'], load_config()['cookie'])
                result['languages'] = [s.get('lan') for s in subs]
                chosen = next((s for s in subs if 'zh' in s.get('lan','')),None)
                if chosen:
                    scheduler._prepare(job,job.pages[0])
                    result['status'] = job.status
                    result['output'] = job.pages[0].txt
                else:
                    result['status'] = '没有取得可用中文字幕；未登录时可能需要 Cookie'
            results[name] = result
        except Exception as exc:
            results[name] = dict(url=url,error=str(exc))
        print(name,results[name],flush=True)
        atomic_json(PROJECT_ROOT/'benchmarks'/'platforms.json',results)


if __name__ == '__main__':
    main()
