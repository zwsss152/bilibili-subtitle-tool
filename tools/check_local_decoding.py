"""Compare a reported damaged passage without editing the user's transcripts."""
import json
from pathlib import Path
import re
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.paths import PROJECT_ROOT
from src.config import atomic_json, load_config
from src.core.download import download_audio, find_ffmpeg
from src.core.asr import get_asr_model


def main():
    directory = PROJECT_ROOT / 'benchmarks' / 'reported-damage'
    directory.mkdir(parents=True, exist_ok=True)
    record = next(json.loads(p.read_text(encoding='utf-8')) for p in (PROJECT_ROOT/'.data/transcripts').glob('*.json')
                  if '视角看过去' in json.loads(p.read_text(encoding='utf-8')).get('title',''))
    clips = [directory / 'age.wav', directory / 'missing.wav']
    if not all(p.exists() for p in clips):
        audio = download_audio(record['url'], 'source', directory, lambda *a: None, cookie=load_config()['cookie'])
        for path, start in zip(clips,[286,56]):
            subprocess.run([find_ffmpeg(),'-v','error','-y','-ss',str(start),'-i',audio,'-t','60','-ar','16000','-ac','1',str(path)],
                           capture_output=True, check=True, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        Path(audio).unlink()
    pipeline = get_asr_model('large-v3-turbo',device='cuda',compute_type='float16')
    results = []
    for path in clips:
        for mode, penalty in [('current',1.05),('standard',1.0),('sequential',1.0)]:
            options = dict(language='zh',beam_size=5,vad_filter=True,repetition_penalty=penalty)
            if mode=='current':
                options['initial_prompt']='以下是普通话访谈，请准确记录原话、数字和专有名词，并使用简体中文标点。'
            else:
                options['initial_prompt']=record['title']
            model = pipeline.model if mode == 'sequential' else pipeline
            if mode!='sequential':options['batch_size']=8
            start=time.perf_counter()
            segments,info=model.transcribe(str(path),**options)
            output=[dict(start=s.start,end=s.end,text=s.text) for s in segments]
            text=''.join(s['text'] for s in output)
            item=dict(clip=path.name,mode=mode,elapsed_seconds=time.perf_counter()-start,text=text,
                      suspicious=len(re.findall(r'[ＢＰ０７\ufffd]',text)),segments=output)
            results.append(item)
            atomic_json(directory/'comparison.json',results)
            print(json.dumps(item,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
