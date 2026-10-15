# Build on Windows using the isolated requirements-build.txt environment.
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, copy_metadata
from PyInstaller.utils.win32.versioninfo import VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable, StringStruct, VarFileInfo, VarStruct

root = Path(SPECPATH).parent
datas = [(str(root / 'assets'), 'assets'), (str(root / 'installer' / 'THIRD-PARTY-NOTICES.md'), '.')]
binaries, hiddenimports = [], []
for package in ('av', 'opencc', 'pypinyin', 'imageio_ffmpeg', 'webview', 'pythonnet', 'clr_loader', 'yt_dlp'):
    package_data, package_binaries, package_hidden = collect_all(package)
    datas += package_data
    binaries += package_binaries
    hiddenimports += package_hidden
for package in ('pywebview', 'pythonnet', 'yt-dlp', 'av', 'imageio-ffmpeg', 'opencc-python-reimplemented',
                'Pillow', 'pypinyin', 'cffi', 'clr_loader', 'proxy_tools', 'pycparser', 'typing_extensions'):
    datas += copy_metadata(package)
namespace = {}
exec((root / 'src' / 'version.py').read_text(encoding='utf-8'), namespace)
version = namespace['VERSION']
version_tuple = tuple(int(part) for part in version.split('.')) + (0,)
version_resource = VSVersionInfo(
    ffi=FixedFileInfo(filevers=version_tuple, prodvers=version_tuple, mask=0x3f,
                     flags=0, OS=0x40004, fileType=1, subtype=0, date=(0,0)),
    kids=[StringFileInfo([StringTable('080404b0', [
        StringStruct('FileDescription', '文字稿 · 视频转录与阅读'),
        StringStruct('FileVersion', version), StringStruct('ProductVersion', version),
        StringStruct('ProductName', '文字稿'), StringStruct('CompanyName', 'zwsss152'),
        StringStruct('OriginalFilename', 'BilibiliSubtitleTool.exe')])]),
        VarFileInfo([VarStruct('Translation', [2052,1200])])])

a = Analysis([str(root / 'app_launcher.py')], pathex=[str(root)], binaries=binaries,
    datas=datas, hiddenimports=hiddenimports,
    excludes=['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'gi', 'cefpython3',
              'webview.platforms.qt', 'webview.platforms.gtk', 'webview.platforms.cocoa', 'webview.platforms.android'],
    noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='BilibiliSubtitleTool',
    debug=False, strip=False, upx=False, console=False,
    icon=str(root / 'assets' / '视频列表.ico'), version=version_resource)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='BilibiliSubtitleTool')
