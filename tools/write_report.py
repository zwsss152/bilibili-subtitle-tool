"""Render measured evidence without claiming unverified recognition quality."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.config import read_json
from src.paths import PROJECT_ROOT


def main():
    root = PROJECT_ROOT/'benchmarks'
    lines = ['# 本机识别性能对照', '', '测量日期：2026-10-03；Ryzen 7 9700X、RTX 5070 12GB、32GB DDR5、Windows、Python 3.13.9。',
        '', '固定样本：机核游戏/AI、军事播客、B 站游戏行业访谈，各在 02:00 与 10:00 起取 120 秒，总计 720 秒。同一组 PCM 16k 单声道文件用于所有配置；音频来源见 `benchmarks/samples.json`。',
        '', '解码 beam 3。新参数强制中文转写、VAD、重复惩罚 1.05；legacy 使用旧项目 medium 的提示语、重复惩罚 1.15、no-repeat-ngram 4 和自动语言检测，batch 16。速度统计为暖模型转写函数墙钟时间，包含解码与 VAD；模型加载另列，下载与文字排版不计入。',
        '', '| 配置 | 六段合计 (秒) | 模型加载 (秒) | 整卡峰值显存 (MiB) | CER |', '|---|---:|---:|---:|---|']
    for path in sorted(root.glob('*-beam3*.json')):
        data=read_json(path,{})
        lines.append(f"| {path.stem} | {sum(s['elapsed_seconds'] for s in data['samples']):.3f} | {data['model_load_seconds']:.3f} | {data.get('peak_device_vram_mib','未采样')} | 待人工参考稿 |")
    lines += ['', '显存为 nvidia-smi 每约 0.1 秒采样的整卡 memory.used，包含桌面及其他应用，属于观测峰值，不能当作模型独占精确峰值。两次 batch 8/16 的差别很小，当前六段较短，不据此声称大批次必然更快。',
        '', '旧 medium 基线合计 9.401 秒；turbo FP16 batch 8 为 6.362 秒（提速约 32.3%）；turbo int8_float16 为 5.958 秒（约 36.6%）。medium int8_float16 反而较慢。large-v3 为 14.278 秒。数据不包含质量合格结论，CER 未提供虚构数字。',
        '', '当前保留 medium。机器稿中可见“危机分/微积分”等专有词错误，需听音频校对后才能采用更快模型。`tools/make_review.py` 生成本地校对页；`tools/select_profile.py` 要求六段已人工校对参考稿，且人工接受数字、专有词、漏句后，按 CER 与数字错误不高于旧 medium 筛选最快配置。不到 20% 提速时保留旧参数基线。',
        '', '完整节目稳定性验证：机核 5949.649 秒音频，medium CUDA FP16 batch 8，识别 60.235 秒；B 站访谈 3519.309 秒，识别 34.190 秒。两个任务的下载、识别、保存与退出共 97.186 秒（下载与 GPU 流水线重叠）。后台进程正常结束，重载任务均保持完成，缓存中无音频。原始逐条记录及 TXT 保存在“文字稿/验收稿件”，逐任务证据见 `benchmarks/integration.json`。',
        '', '完整节目只是稳定性测试，尚无逐字人工参考稿，不能据此宣称准确率达到某个百分比。']
    (PROJECT_ROOT/'docs'/'performance.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


if __name__ == '__main__':
    main()
