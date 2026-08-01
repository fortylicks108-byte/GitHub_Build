from pathlib import Path
import os
import shutil
import subprocess
import sys
from PIL import Image

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / 'build'
DIST = ROOT / 'dist'
DMG = ROOT / 'dist-dmg'
APP_NAME = 'Box Flip Automator'


def run(*args):
    print('+', ' '.join(map(str, args)))
    subprocess.run(list(map(str, args)), check=True)


def make_icns():
    iconset = ROOT / 'boxflip.iconset'
    shutil.rmtree(iconset, ignore_errors=True)
    iconset.mkdir()
    source = Image.open(ROOT / 'boxflip.png').convert('RGBA')
    sizes = [(16,1),(16,2),(32,1),(32,2),(128,1),(128,2),(256,1),(256,2),(512,1),(512,2)]
    for size, scale in sizes:
        px=size*scale
        img=source.resize((px,px), Image.Resampling.LANCZOS)
        suffix='' if scale==1 else '@2x'
        img.save(iconset / f'icon_{size}x{size}{suffix}.png')
    run('iconutil','-c','icns',iconset,'-o',ROOT/'boxflip.icns')
    shutil.rmtree(iconset)


def main():
    if sys.platform != 'darwin':
        raise SystemExit('This builder must run on macOS. Use the included GitHub Actions workflow from Windows.')
    shutil.rmtree(BUILD, ignore_errors=True)
    shutil.rmtree(DIST, ignore_errors=True)
    shutil.rmtree(DMG, ignore_errors=True)
    DMG.mkdir()
    make_icns()
    run(sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed', '--onedir',
        '--name', APP_NAME,
        '--osx-bundle-identifier', 'com.fortylix.boxflipautomator',
        '--icon', ROOT/'boxflip.icns',
        '--add-data', f'{ROOT / "boxflip.png"}:.',
        '--hidden-import', 'Quartz', '--hidden-import', 'Vision', '--hidden-import', 'Foundation', '--hidden-import', 'AppKit',
        ROOT/'app.py')
    app = DIST / f'{APP_NAME}.app'
    run('codesign','--force','--deep','--sign','-',app)
    stage = ROOT / 'dmg-stage'
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir()
    shutil.copytree(app, stage/app.name)
    os.symlink('/Applications', stage/'Applications')
    arch = subprocess.check_output(['uname','-m'], text=True).strip()
    out = DMG / f'BoxFlipAutomator-macOS-Tahoe-{arch}.dmg'
    run('hdiutil','create','-volname','Box Flip Automator','-srcfolder',stage,'-ov','-format','UDZO',out)
    shutil.rmtree(stage)
    print(out)

if __name__ == '__main__':
    main()
