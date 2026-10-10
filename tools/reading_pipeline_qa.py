"""Real ASR -> local editor lifecycle, using a copied local two-minute clip."""
from pathlib import Path
import json
import shutil
import sys
import tempfile
import time
import argparse
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.paths import PROJECT_ROOT
from src.core import documents, library, reading, rename
from src.core.links import parse_source
from src.services import reading_service
from src.services.scheduler import Scheduler
from src.services.tasks import Task,Page
import src.services.scheduler as scheduler_module
from src.core.cloud import discard_checkpoints


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--engine',choices=('local','cloud'),default='local')
    parser.add_argument('--run',default='reading-final')
    args=parser.parse_args()
    artifacts=PROJECT_ROOT/'tests'/'.artifacts'
    artifacts.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='reading-pipeline-',dir=artifacts) as temporary:
        root=Path(temporary).resolve()
        assert root.is_relative_to(artifacts.resolve())
        for module in (documents,library,rename,reading_service):module.OUTPUT_ROOT=root/'output'
        documents.RECORDS_DIR=rename.RECORDS_DIR=root/'records'
        reading.PROFILE=root/'profile.json'
        reading.PROFILE.write_text(json.dumps(dict(version=reading.VERSION,validated=True)),encoding='utf-8')
        audio=root/'audio.wav'
        shutil.copyfile(PROJECT_ROOT/'benchmarks'/'clips'/'interview_1.wav',audio)
        scheduler=Scheduler(state_path=root/'queue.json',autostart=False)
        scheduler_module.cleanup_stale_cache=lambda *a:None
        # The copied fixture audio is owned by TemporaryDirectory, not ASR_CACHE.
        scheduler._cleanup=lambda path:None
        job=Task.create(parse_source('BV17x411w7KC'),'串行验收',args.engine)
        job.title='转写流水线验收'
        job.output_dir=str((root/'output'/library.ORIGINAL/job.topic).relative_to(PROJECT_ROOT))
        page=Page(1,job.title,job.source_url,status='等待识别')
        job.pages=[page]
        job.status='等待识别'
        scheduler.jobs=[job]
        scheduler.start()
        scheduler.slots.acquire()
        scheduler.ready.put((job,page,str(audio),root))
        phases=set()
        started=time.monotonic()
        try:
            while time.monotonic()-started<240:
                if scheduler.process and scheduler.editor.process:
                    raise AssertionError('ASR与整理模型同时驻留')
                if scheduler.process:phases.add('ASR')
                if scheduler.editor.process:phases.add('editor')
                if page.reading_status in ('完成','原文排版'):
                    break
                if job.status=='失败' or page.reading_status=='整理失败':
                    raise AssertionError(job.detail or page.reading_error)
                time.sleep(.1)
            assert page.reading_status in ('完成','原文排版'),page.reading_status
            assert job.status=='完成'
            assert phases==({'ASR','editor'} if args.engine=='local' else {'ASR'}),phases
            raw=json.loads((PROJECT_ROOT/page.raw).read_text(encoding='utf-8'))
            result=dict(seconds=round(time.monotonic()-started,3),phases=sorted(phases),
                        original_device=raw['recognition']['device'],editor_device=raw['reading']['device'],
                        reading_status=page.reading_status,blocks=len(raw['reading']['blocks']),
                        fallback_reasons=[b['reason'] for b in raw['reading']['blocks'] if b['reason']],
                        model_error=raw['reading'].get('model_error',''),
                        original_exists=(PROJECT_ROOT/page.txt).exists(),reading_exists=(PROJECT_ROOT/page.reading).exists())
            assert raw['recognition']['engine']==args.engine
            assert raw['reading']['strategy']==('local' if args.engine=='local' else 'cloud')
            scheduler.rename_transcript(job.jid,'流水线新名称')
            assert (PROJECT_ROOT/page.txt).name=='流水线新名称.txt'
            assert (PROJECT_ROOT/page.reading).name=='流水线新名称.txt'
            assert (PROJECT_ROOT/page.reading).read_text(encoding='utf-8').startswith('# 流水线新名称\n')
            result['rename_verified']=True
            assert scheduler.remove({job.jid})==1
            assert not scheduler.jobs
            assert not list((root/'output').rglob('*.txt'))
            result['delete_verified']=True
            report=PROJECT_ROOT/'benchmarks'/'results'/args.run/'pipeline.json'
            report.parent.mkdir(parents=True,exist_ok=True)
            report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(result,ensure_ascii=False),flush=True)
        finally:
            scheduler.shutdown()
            discard_checkpoints(job.jid)
        assert scheduler.process is None and scheduler.editor.process is None


if __name__=='__main__':main()
