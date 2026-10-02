from pathlib import Path

root = Path.cwd()
a = Analysis(
    ['launcher.py'], pathex=[], binaries=[],
    datas=[(str(root / 'fb_collector/templates'), 'fb_collector/templates'),
           (str(root / 'fb_collector/static'), 'fb_collector/static')],
    hiddenimports=['pystray._darwin', 'PIL.Image', 'PIL.ImageDraw', 'keyring.backends.macOS', 'cryptography.fernet'],
    excludes=['tkinter', 'numpy', 'pandas', 'torch', 'scipy', 'matplotlib', 'cv2'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='FBPostCollector',
          console=False, upx=False, argv_emulation=False)
coll = COLLECT(exe, a.binaries, a.datas, name='FBPostCollector', upx=False)
app = BUNDLE(coll, name='FBPostCollector.app', bundle_identifier='com.fbpostcollector.desktop',
             info_plist={'CFBundleShortVersionString': '1.4.18', 'CFBundleVersion': '1.4.18',
                         'LSUIElement': True, 'LSMinimumSystemVersion': '15.0'})
