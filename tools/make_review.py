"""Prepare a local audio and transcript comparison page. Drafts are never gold labels."""
import html
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.config import read_json, atomic_json
from src.paths import PROJECT_ROOT


def main():
    root = PROJECT_ROOT / 'benchmarks'
    samples = read_json(root / 'samples.json', [])
    keys = [p.stem for p in sorted(root.glob('*-beam3*.json'))]
    sections = []
    for sample in samples:
        if 'path' not in sample:
            continue
        drafts = []
        for key in keys:
            path = root / 'results' / key / (sample['id']+'.txt')
            if path.exists():
                drafts.append(f'<details><summary>{html.escape(key)}</summary><p>{html.escape(path.read_text(encoding="utf-8"))}</p></details>')
        sections.append(f'''<section><h2>{sample['id']} · {html.escape(sample['title'])}</h2>
<p>原节目 {sample['start']//60}:00 起，共 2 分钟。先听音频，逐字写原话；模型稿仅供比较。</p>
<audio controls preload="none" src="clips/{sample['id']}.wav"></audio>
{''.join(drafts)}<p>人工参考稿（空白起步）：</p><textarea id="{sample['id']}" rows="10"></textarea>
<button onclick="save('{sample['id']}')">导出 {sample['id']}.txt</button></section>''')
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>转写质量校对</title>
<style>body{font:16px/1.7 system-ui;background:#f5f5f7;max-width:1050px;margin:30px auto;color:#1d1d1f}section{background:white;padding:24px;border:1px solid #ddd;border-radius:14px;margin:24px 0}textarea{box-sizing:border-box;width:100%;font:16px/1.6 system-ui}button{background:#007aff;color:white;border:0;border-radius:8px;padding:10px 16px}p{white-space:pre-wrap}audio{width:100%}</style>
<h1>六段固定音频的人工校对</h1><p>以下所有模型稿均为机器输出，尚未验证质量。请保留口语、数字、英文和专有词，勿改写；听不清处标注后反馈，避免将机器稿直接当参考答案。</p>
<p>导出的 TXT 放入 benchmarks/references。全部校对完成后，更新 quality-review.json 的 reviewed_samples，并逐配置核对数字、专有词及漏句。工具只采用确认质量合格的配置。</p>''' + ''.join(sections) + '''<script>
function save(id){const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([document.getElementById(id).value],{type:'text/plain;charset=utf-8'}));a.download=id+'.txt';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)}
</script></html>'''
    (root / '校对.html').write_text(page, encoding='utf-8')
    review = root / 'quality-review.json'
    if not review.exists():
        atomic_json(review, dict(reviewed_samples=[], accepted_profiles=[], note='需人工听音频建立参考稿，并确认数字、专有词与漏句不退步'))


if __name__ == '__main__':
    main()
