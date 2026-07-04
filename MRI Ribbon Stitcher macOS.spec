# -*- mode: python ; coding: utf-8 -*-
# macOS .app bundle spec for MRI Ribbon Stitcher.
# Run on a Mac:
#   pip install pyinstaller
#   pyinstaller "MRI Ribbon Stitcher macOS.spec"
# The finished app lands in dist/MRI Ribbon Stitcher.app
# Gatekeeper blocks unsigned apps — right-click the app, choose Open, confirm.

a = Analysis(
    ['mri_ribbon_stitcher.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,   # binaries go into COLLECT so the .app can find them
    name='MRI Ribbon Stitcher',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=True,     # lets the app open files dropped on its Dock icon
    target_arch=None,        # build for the current arch; set to 'universal2' for fat binary
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='MRI Ribbon Stitcher',
)

app = BUNDLE(
    coll,
    name='MRI Ribbon Stitcher.app',
    icon=None,               # replace with 'MRI Ribbon Stitcher.icns' if you add an icon
    bundle_identifier='com.mristitcher.ribbonstitcher',
    version='1.0.0',
    info_plist={
        'CFBundleName': 'MRI Ribbon Stitcher',
        'CFBundleDisplayName': 'MRI Ribbon Stitcher',
        'CFBundleShortVersionString': '1.0.0',
        'CFBundleVersion': '1.0.0',
        'NSHighResolutionCapable': True,
        'NSRequiresAquaSystemAppearance': False,  # allow Dark Mode
        'CFBundleDocumentTypes': [
            {
                'CFBundleTypeName': 'Image File',
                'CFBundleTypeRole': 'Viewer',
                'LSItemContentTypes': [
                    'public.jpeg',
                    'public.png',
                    'public.tiff',
                    'com.microsoft.bmp',
                    'org.webmproject.webp',
                ],
            }
        ],
        # Show in macOS Open-With menus for common image types
        'LSApplicationCategoryType': 'public.app-category.medical',
        'NSHumanReadableCopyright': 'Copyright © 2024 MRI Ribbon Stitcher contributors',
    },
)
