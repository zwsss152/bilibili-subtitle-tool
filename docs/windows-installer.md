# Windows 安装版

## 1.0.2 错误诊断

云端调用失败会保留 HTTP 状态、服务错误码、脱敏错误说明、请求 ID、接口、模型、重试次数；识别失败还记录任务 ID、分 P、音频分块序号和时间范围、WAV 字节数及处理阶段。识别子进程通过队列把诊断和异常堆栈交给主程序写入，避免子进程没有日志处理器而丢失错误。界面失败提示也会保留服务错误码、说明、请求 ID 和分块位置。

设置中点击“打开日志文件夹”，查看 `%LOCALAPPDATA%\BilibiliSubtitleTool\logs\app.log`。日志以 UTF-8 保存，单文件约 5 MB，另保留 5 个轮转备份；启动时记录版本与进程 ID。记录不包含请求体、音频 Base64、密钥或 Cookie；鉴权失败的服务原文隐藏，其他错误说明经过脱敏及长度限制，截断会标记。非 JSON 错误也保留经过脱敏的限长说明。已有断点不变，更新不会自动重试或调用付费接口；历史已丢失的服务错误无法补回。

某些 Windows 环境会把普通队列文件的解析名称报告为应用虚拟目录，但父目录仍是原 AppData 目录。队列事务目录的安全检查以解析后的父目录及普通文件名为准；真实符号链接仍校验最终目标，项目外路径继续拒绝。数据位置不变，不复制或删除原数据。

## 用户体验与配置

下载 Setup EXE，安装后从桌面快捷方式启动。无需 Python、pip、显卡或本地模型。首次引导打开设置，三个页签分别为：

- **云端服务**：自己的北京地域百炼 API Key、可选业务空间 ID，以及费用与上传说明。
- **模型选择**：语音识别、阅读稿整理、内容核对三个模型；可以手动填写兼容 ID，或点击“获取可用模型”。该按钮只查询列表，不进行付费转录。列表可见不保证账户已开通权限。
- **平台与应用**：B 站 Cookie、版本号、打开日志文件夹。

密钥本机 DPAPI 加密，不进入队列、日志、Git 或安装包。设置空密钥表示保留原密钥。模型参数随转录任务保存；更改设置不改变已入队任务。整理请求单独记录模型，未完成分块变更模型时不会混用结果。无效模型明确显示错误，不自动切换和产生额外模型费用。

默认模型经过本次短音频调用：`qwen-audio-3.0-asr-flash`、`qwen3.8-flash`、`qwen3.8-max`。原来默认的 `qwen3.5-plus` 返回了 HTTP 404，因此不再作为新安装的默认整理模型。账户权限、地域和模型供应情况仍需用户自行确认。

## 安装与数据

- 支持 Windows 10 22H2 / Windows 11 x64；当前用户安装，无需管理员权限。
- 程序：`%LOCALAPPDATA%\Programs\BilibiliSubtitleTool`。
- 稿件、队列、设置、加密密钥、日志：`%LOCALAPPDATA%\BilibiliSubtitleTool`。
- 安装包包含微软签名的 **x64 WebView2 Evergreen Standalone Installer**，缺少运行库时按当前用户安装，不使用需要联网下载运行库的 bootstrapper。
- 更新通过新版 Setup 覆盖安装；固定 AppId，个人数据位于安装目录之外，卸载不删除个人数据。
- 旧源码目录保持原样，不扫描硬盘、不自动导入旧稿；源码版与安装版有各自的数据目录。
- 第一版不加入自动更新、付费代码签名或服务器登录服务。

## 开发构建

在 Windows x64 / Python 3.13.16 中执行：

```powershell
python -m venv .build-venv
.build-venv\Scripts\python.exe -m pip install -r requirements-build.txt
pwsh -File tools/build_release.ps1 -Python .build-venv\Scripts\python.exe
pwsh -File tools/test_installer.ps1 -Setup release/BilibiliSubtitleTool-Setup-1.0.0-x64.exe
```

PyInstaller 使用目录构建，Inno Setup 将整个目录制成单个安装包。图标、字典、Tk、FFmpeg、PyAV 原生库、pythonnet/WebView2 DLL 和第三方许可信息一起收集。程序入口在导入界面前调用 `freeze_support()`，让冻结后的识别和浏览器工作进程正确分流。普通启动无控制台；诊断通过独立 GUI 展示。

构建脚本下载官方 Inno Setup 6.7.3 与 WebView2 x64 离线安装程序，检查数字签名、发布者和最小文件体积，记录 SHA-256。安装包不包含 `.venv`、源码测试数据、用户密钥、Cookie 或稿件。

测试工具会拒绝覆盖已有安装或同名桌面快捷方式；安装生命周期测试应在专用 Windows 用户或虚拟机中运行。构建输出位于 `release/`，包含安装 EXE、SHA-256 校验文件、依赖下载清单与离线检查报告。

## 本次验证与限制

- Python 3.13.16 的完整回归测试通过，包含安装目录与数据目录分离、模型查询过滤、模型角色校验、任务快照和切换模型后断点失效。
- 冻结 EXE 在移除系统 Python 的 PATH、清除 Python 环境变量后，通过媒体读取、FFmpeg 转换、OpenCC、DPAPI、实际识别工作进程启动/取消/退出、Tk 设置窗口以及独立 WebView2 离线页面检查。
- 当前 Windows 主机实际完成安装、桌面快捷方式检查、同版本覆盖安装、卸载、用户数据哨兵文件保留检查。
- 用户授权后，用 **11.97 秒合成中文语音**进行真实识别，得到“预算是 1000 元，不是 100 元”等完整内容；使用 `qwen3.8-flash` 整理、`qwen3.8-max` 核对成功，无原文回退。这只是短音频功能验收，不是长视频质量、费用或字错率评测。
- 本机没有 Windows Sandbox，因此**没有完成无 Python 的全新 Windows 虚拟机验收**；清除 PATH 的测试不等于干净系统测试。缺少 WebView2 的机器上首次离线安装，以及跨版本升级也仍需专机验收。

## GitHub 发布

`.github/workflows/windows-release.yml` 在 PR、手动运行和 `v*` 标签时构建。版本标签必须与 `src/version.py` 一致。构建和安装生命周期检查通过后上传 artifact；版本标签任务创建带 EXE 与 SHA-256 的 **draft release**。

正式发布前，必须在无 Python、无开发环境、未装 WebView2 的干净 Windows 10/11 x64 机器完成安装、快捷方式、媒体、浏览器、升级与卸载验收，并更新记录。未完成前保持草稿，不将当前候选宣称为正式通过验收的版本。
