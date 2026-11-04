"""Build a per-user Windows installer from a verified clean client package.

Uninstall removes only files shipped in that package. Runtime data and other
files created after installation stay in place; no data-directory migration is
performed. A companion PowerShell bootstrap pins the generated installer's hash.
This command neither signs updates nor publishes a release.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'build helper unavailable: {filename}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


clean = load('clean_installer_source', 'build-portable-clean.py')
release = load('installer_package_boundary', 'build-windows-release.py')
ARTIFACT = 'Zhiji-WeChat-Assistant-${version}-Setup-${arch}.exe'


def nsis_path(relative: str) -> str:
    """Accept an exact relative Windows filename, never NSIS code or globs."""
    if (not isinstance(relative, str) or not relative or '\\' in relative or
            any(ord(c) < 32 or c in '\":*?<>|' for c in relative)):
        raise ValueError('unsafe installer file path')
    path = PurePosixPath(relative)
    if (not path.parts or path.is_absolute() or path.as_posix() != relative or
            any(part in {'', '.', '..'} or part.endswith((' ', '.')) for part in path.parts) or
            any(part.casefold() == '.local' for part in path.parts)):
        raise ValueError('unsafe installer file path')
    # NSIS expands variables even inside quoted strings.
    return relative.replace('$', '$$').replace('/', '\\')


def render_uninstall_include(files: list[str]) -> str:
    if not files or len({name.casefold() for name in files}) != len(files):
        raise ValueError('installer file inventory must be nonempty and unique')
    directories: set[str] = set()
    lines = [
        '; Generated from the verified package. No recursive deletion.',
        '; Replace the builder default: never terminate a running application.',
        '!include "nsProcess.nsh"',
        '!macro customCheckAppRunning',
        '  Push $R0',
        '  ${nsProcess::FindProcess} "${APP_EXECUTABLE_FILENAME}" $R0',
        '  ${If} $R0 != 603',
        '    DetailPrint "Please close Zhiji WeChat Assistant before installing or uninstalling."',
        '    ${IfNot} ${Silent}',
        '      MessageBox MB_OK|MB_ICONEXCLAMATION "Please close Zhiji WeChat Assistant and try again."',
        '    ${EndIf}',
        '    Pop $R0',
        '    SetErrorLevel 2',
        '    Quit',
        '  ${EndIf}',
        '  Pop $R0',
        '!macroend',
        '',
        '!macro customRemoveFiles',
        '  SetOutPath "$TEMP"',
    ]
    for name in sorted(files, key=str.casefold):
        escaped = nsis_path(name)
        # Fail before removing registration if an installed program file is busy.
        lines.extend([
            f'  ${{If}} ${{FileExists}} "$INSTDIR\\{escaped}"',
            '    ClearErrors',
            f'    Delete "$INSTDIR\\{escaped}"',
            '    ${If} ${Errors}',
            '      SetErrorLevel 2',
            '      Abort "Please close Zhiji WeChat Assistant and try again."',
            '    ${EndIf}',
            '  ${EndIf}',
        ])
        parts = PurePosixPath(name).parts
        directories.update('/'.join(parts[:i]) for i in range(1, len(parts)))
    lines.extend([
        '  ClearErrors',
        '  Delete "$INSTDIR\\${UNINSTALL_FILENAME}"',
        '  ${If} ${Errors}',
        '    SetErrorLevel 2',
        '    Abort "Unable to remove the uninstaller. Please try again."',
        '  ${EndIf}',
    ])
    for name in sorted(directories, key=lambda v: (-len(PurePosixPath(v).parts), v.casefold())):
        lines.append(f'  RMDir "$INSTDIR\\{nsis_path(name)}"')
    lines.extend(['  RMDir "$INSTDIR"', '  ClearErrors', '!macroend', ''])
    return '\n'.join(lines)


def installer_config(generated: dict, output: Path, include: Path) -> dict:
    from msvc_runtime import specification
    license_path = ROOT / 'licenses/Microsoft-Visual-Cpp-Runtime-2015-2022.rtf'
    license_row = next(row for row in specification(ROOT)['files'] if row['name'] == license_path.name)
    if license_path.stat().st_size != license_row['bytes'] or clean.digest(license_path) != license_row['sha256']:
        raise ValueError('Microsoft runtime license differs from the pinned original RTF')
    config = json.loads(json.dumps(generated))
    if (config.get('appId') != 'com.local.knowyourfriends.real-client' or
            config.get('extraMetadata', {}).get('name') != 'know-your-friends' or
            config.get('win', {}).get('executableName') != 'KnowYourFriends'):
        raise ValueError('installer compatibility identity changed')
    if config['extraMetadata'].get('kyfUpdatesEnabled') is not False:
        raise ValueError('first installer must explicitly disable app updates')
    config['directories']['output'] = str(output)
    config['publish'] = None
    config['win']['target'] = ['nsis']
    config['nsis'] = {
        'oneClick': True,
        'perMachine': False,
        'packElevateHelper': False,
        'deleteAppDataOnUninstall': False,
        'runAfterFinish': False,
        # ZIP/Deflate avoids newer 7z executable filters that the bundled NSIS
        # decoder may silently skip (observed with setuptools ARM64 launchers).
        'useZip': True,
        'differentialPackage': False,
        'createDesktopShortcut': True,
        'createStartMenuShortcut': True,
        'shortcutName': '知己微信助手',
        'artifactName': ARTIFACT,
        'include': str(include),
        'license': str(ROOT / 'licenses/Microsoft-Visual-Cpp-Runtime-2015-2022.rtf'),
    }
    return config


def package_inventory(unpacked: Path, version: str, node: Path) -> dict[str, dict]:
    """Use the release ZIP's full-tree privacy/link/size boundary before NSIS."""
    rows = release._scan(unpacked)
    release._require_package(rows, version, node)
    return {relative.as_posix(): {'bytes': info.st_size, 'sha256': clean.digest(source)}
            for source, relative, directory, info in rows if not directory}


def validate_installer_url(value: str, artifact_name: str) -> None:
    url = urlsplit(value)
    if (url.scheme != 'https' or not url.hostname or url.username is not None or
            url.password is not None or url.query or url.fragment or url.port == 0 or
            any(ord(char) <= 32 or ord(char) == 127 or char == '\\' for char in value) or
            url.path.rsplit('/', 1)[-1] != artifact_name):
        raise ValueError(f'installer URL must be an HTTPS URL for {artifact_name}, without credentials, query or fragment')


def write_bootstrap(artifact: Path, sha256: str, download_url: str | None) -> Path:
    template = (ROOT / 'scripts' / 'install-zhiji.ps1').read_text(encoding='utf-8-sig')
    replacements = {
        '__ZHIJI_INSTALLER_FILE_NAME__': artifact.name,
        '__ZHIJI_INSTALLER_SHA256__': sha256,
    }
    if download_url is not None:
        replacements['__ZHIJI_DOWNLOAD_URL__'] = download_url
    for marker, value in replacements.items():
        literal = f"'{marker}'"
        if template.count(literal) != 1:
            raise ValueError(f'bootstrap template must define exactly one {marker}')
        template = template.replace(literal, "'" + value.replace("'", "''") + "'", 1)
    bootstrap = artifact.with_name('install-zhiji.ps1')
    # Windows PowerShell 5.1 needs the UTF-8 BOM to read the Chinese script text.
    with bootstrap.open('x', encoding='utf-8-sig', newline='\r\n') as stream:
        stream.write(template)
    return bootstrap


def build_installer(build_dir: Path, node: Path, *, require_clean=False, installer_url: str | None = None) -> dict:
    build_dir = build_dir.resolve()
    node = node.resolve()
    result = clean.verify_package(ROOT, build_dir, node, require_clean=require_clean)
    version = result['version']
    if not re.fullmatch(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)', version):
        raise ValueError('installer version must be stable X.Y.Z')
    artifact_name = f'Zhiji-WeChat-Assistant-{version}-Setup-x64.exe'
    if installer_url is not None:
        validate_installer_url(installer_url, artifact_name)
    unpacked = build_dir / 'release' / 'win-unpacked'
    before = package_inventory(unpacked, version, node)
    asar_policy = subprocess.check_output([
        str(node), '-e',
        "const a=require('@electron/asar');const p=JSON.parse(a.extractFile(process.argv[1],'package.json'));process.stdout.write(JSON.stringify(p.kyfUpdatesEnabled));",
        str(unpacked / 'resources/app.asar'),
    ], cwd=ROOT, text=True).strip()
    if asar_policy != 'false':
        raise ValueError('packaged first installer app updates must be disabled')
    working = Path(tempfile.mkdtemp(prefix='installer-', dir=build_dir))
    include = working / 'preserve-runtime-data.nsh'
    include.write_text(render_uninstall_include(list(before)), encoding='utf-8')
    generated = json.loads((build_dir / 'electron-builder.generated.json').read_text(encoding='utf-8'))
    config = installer_config(generated, working / 'release', include)
    config_file = working / 'electron-builder.installer.json'
    config_file.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (working / 'installed-files.json').write_text(json.dumps(before, indent=2) + '\n', encoding='utf-8')
    subprocess.run([
        str(node), str(ROOT / 'node_modules/electron-builder/cli.js'),
        '--prepackaged', str(unpacked), '--win', 'nsis', '--x64',
        '--config', str(config_file), '--publish', 'never',
    ], cwd=ROOT, check=True)
    if package_inventory(unpacked, version, node) != before:
        raise ValueError('verified package changed during installer build')
    if require_clean:
        clean.check_source_snapshot(result, clean.source_revision(ROOT, require_clean=True), require_clean=True)
    artifact = working / 'release' / artifact_name
    if not artifact.is_file() or artifact.stat().st_size == 0:
        raise ValueError('Windows installer output is missing')
    with artifact.open('rb') as stream:
        if stream.read(2) != b'MZ':
            raise ValueError('Windows installer header is invalid')
    sha256 = clean.digest(artifact)
    bootstrap = write_bootstrap(artifact, sha256, installer_url)
    installer = {
        **result, 'installer': str(artifact), 'installerBytes': artifact.stat().st_size,
        'installerSha256': sha256, 'installedFileCount': len(before),
        'bootstrap': str(bootstrap), 'bootstrapSha256': clean.digest(bootstrap),
        'bootstrapDownloadUrl': installer_url,
        'installerBuildDirectory': str(working), 'appUpdatesEnabled': False,
        'uninstallPolicy': 'remove shipped files only; retain runtime data and unshipped files',
        'published': False,
    }
    (working / 'installer-verification.json').write_text(
        json.dumps(installer, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return installer


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path, help='reuse a verified clean build directory')
    parser.add_argument('--electron-dist', type=Path)
    parser.add_argument('--python-exe', type=Path)
    parser.add_argument('--node-exe', type=Path)
    parser.add_argument('--node-license', type=Path)
    parser.add_argument('--require-clean', action='store_true', help='require committed, clean source for a formal installer')
    parser.add_argument('--installer-url', help='optional final HTTPS URL of the generated installer for the bootstrap')
    args = parser.parse_args(argv)
    node = args.node_exe or os.environ.get('KYF_BUILD_NODE') or shutil.which('node')
    if not node:
        parser.exit(1, 'Node 24.11.1 is required\n')
    try:
        clean.source_revision(ROOT, require_clean=args.require_clean)
        build_dir = args.build_dir or Path(clean.build(args)['buildDirectory'])
        result = build_installer(build_dir, Path(node), require_clean=args.require_clean,
                                 installer_url=args.installer_url)
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Windows installer build rejected: {error}\n')
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
