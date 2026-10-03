"""Choose the fastest quality-approved local profile, using reviewed references only."""
from pathlib import Path
import re
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.config import atomic_json, read_json
from src.paths import PROJECT_ROOT, PROFILE_PATH
from tools.benchmark import canonical, distance


def main():
    root = PROJECT_ROOT / 'benchmarks'
    review = read_json(root / 'quality-review.json', {})
    samples = [s['id'] for s in read_json(root / 'samples.json', []) if 'path' in s]
    if len(samples) != 6 or set(samples) != set(review.get('reviewed_samples', [])):
        raise SystemExit('尚无六段完整、已人工校对的参考稿；保持当前可靠配置。')
    references = {s:(root/'references'/f'{s}.txt').read_text(encoding='utf-8') for s in samples}
    baseline = 'medium-float16-batch16-beam3-legacy'
    measurements = {}
    for path in root.glob('*-beam3*.json'):
        record = read_json(path, {})
        if len(record.get('samples', [])) != 6:
            continue
        errors = total = number_errors = 0
        for name in samples:
            output = (root/'results'/path.stem/f'{name}.txt').read_text(encoding='utf-8')
            ref, pred = canonical(references[name]), canonical(output)
            errors += distance(ref, pred)
            total += len(ref)
            number_errors += distance(re.findall(r'\d+(?:\.\d+)?', references[name]), re.findall(r'\d+(?:\.\d+)?', output))
        measurements[path.stem] = dict(cer=errors/max(total,1), numeric_errors=number_errors,
            seconds=sum(s['elapsed_seconds'] for s in record['samples']), profile=record['profile'])
    base = measurements[baseline]
    candidates = {k:v for k,v in measurements.items() if k in review.get('accepted_profiles', [])
                  and v['cer'] <= base['cer'] and v['numeric_errors'] <= base['numeric_errors']}
    if not candidates:
        raise SystemExit('没有质量达到旧 medium 基线、且人工确认专有词与漏句合格的配置；保持当前配置。')
    name, best = min(candidates.items(), key=lambda pair:(pair[1]['seconds'],pair[1]['cer']))
    profile = best['profile'] | {'quality_verified':True, 'benchmark':name}
    if name.endswith('-legacy'):
        profile['legacy_settings'] = True
    improvement = 1-best['seconds']/base['seconds']
    if improvement < .20:
        profile = base['profile'] | {'legacy_settings':True, 'quality_verified':True, 'benchmark':baseline}
        name = baseline
    atomic_json(PROFILE_PATH, profile)
    atomic_json(root/'quality-results.json', dict(selected=name, speed_improvement=improvement, measurements=measurements))
    print(f'已应用自动档位：{name}；相对原 medium 提速 {improvement:.1%}。重新启动应用生效。')


if __name__ == '__main__':
    main()
