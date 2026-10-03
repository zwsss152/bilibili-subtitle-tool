# 本机识别性能对照

测量日期：2026-10-03；Ryzen 7 9700X、RTX 5070 12GB、32GB DDR5、Windows、Python 3.13.9。

固定样本：机核游戏/AI、军事播客、B 站游戏行业访谈，各在 02:00 与 10:00 起取 120 秒，总计 720 秒。同一组 PCM 16k 单声道文件用于所有配置；音频来源见 `benchmarks/samples.json`。

解码 beam 3。新参数强制中文转写、VAD、重复惩罚 1.05；legacy 使用旧项目 medium 的提示语、重复惩罚 1.15、no-repeat-ngram 4 和自动语言检测，batch 16。速度统计为暖模型转写函数墙钟时间，包含解码与 VAD；模型加载另列，下载与文字排版不计入。

| 配置 | 六段合计 (秒) | 模型加载 (秒) | 整卡峰值显存 (MiB) | CER |
|---|---:|---:|---:|---|
| large-v3-float16-batch8-beam3 | 14.278 | 2.012 | 6937 | 待人工参考稿 |
| large-v3-turbo-float16-batch16-beam3 | 6.572 | 1.204 | 4271 | 待人工参考稿 |
| large-v3-turbo-float16-batch8-beam3 | 6.362 | 1.064 | 4208 | 待人工参考稿 |
| large-v3-turbo-int8_float16-batch8-beam3 | 5.958 | 1.916 | 3203 | 待人工参考稿 |
| medium-float16-batch16-beam3-legacy | 9.401 | 1.225 | 4487 | 待人工参考稿 |
| medium-float16-batch16-beam3 | 9.956 | 1.244 | 4327 | 待人工参考稿 |
| medium-float16-batch8-beam3 | 9.442 | 1.203 | 4265 | 待人工参考稿 |
| medium-int8_float16-batch8-beam3 | 10.639 | 1.860 | 3369 | 待人工参考稿 |

显存为 nvidia-smi 每约 0.1 秒采样的整卡 memory.used，包含桌面及其他应用，属于观测峰值，不能当作模型独占精确峰值。两次 batch 8/16 的差别很小，当前六段较短，不据此声称大批次必然更快。

旧 medium 基线合计 9.401 秒；turbo FP16 batch 8 为 6.362 秒（提速约 32.3%）；turbo int8_float16 为 5.958 秒（约 36.6%）。medium int8_float16 反而较慢。large-v3 为 14.278 秒。数据不包含质量合格结论，CER 未提供虚构数字。

上表属于早期单模型对照。用户后续要求准确度优先且无需设置，当前默认改为 large-v3-turbo beam 5 加自动中文复核，不把这项调整说成完成了六段人工 CER 验收。`tools/make_review.py` 与 `tools/select_profile.py` 是开发验收工具，不是日常使用步骤。

完整节目稳定性验证：机核 5949.649 秒音频，medium CUDA FP16 batch 8，识别 60.235 秒；B 站访谈 3519.309 秒，识别 34.190 秒。两个任务的下载、识别、保存与退出共 97.186 秒（下载与 GPU 流水线重叠）。后台进程正常结束，重载任务均保持完成，缓存中无音频。原始逐条记录及 TXT 保存在“文字稿/验收稿件”，逐任务证据见 `benchmarks/integration.json`。

完整节目只是稳定性测试，尚无逐字人工参考稿，不能据此宣称准确率达到某个百分比。

新版自动流程在用户报告错误的完整 B 站视频（1338.050 秒）上耗时 29.952 秒，含首次中文模型初始化，不含下载。原 medium 稿记录识别耗时 12.381 秒；新版增加复核耗时，优先解决实际同音字错误。报告的“捋了个油”已改善为“旅了个游”，两轮原始文字保留。此处仅比较该节目，不能直接外推为全部内容的速度或准确率。结果在 `benchmarks/reported-error/full-production.json`。
