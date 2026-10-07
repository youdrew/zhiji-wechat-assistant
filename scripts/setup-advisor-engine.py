"""Install the fixed Advisor runtime without consulting global OpenCode state."""
from pathlib import Path
import hashlib
import json
import shutil
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.18.34"
ARCHIVE = "opencode-windows-x64-baseline.zip"
ARCHIVE_SHA256 = "f89ab2720050780a450e3cf3e48ac3f0409235b46b6c548c69aa2b7051d716f4"
EXE_SHA256 = "184f196ec97c843a64b2e1a2b49165f25e73a5d6993e2f842c9958c2b1f7a5b2"
EXE_BYTES = 180599176
URL = f"https://github.com/anomalyco/opencode/releases/download/v{VERSION}/{ARCHIVE}"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def checked_engine(folder):
    if not folder.is_dir() or folder.is_symlink() or getattr(folder, "is_junction", lambda: False)():
        raise ValueError("Advisor runtime directory is unsafe")
    executable, manifest = folder / "opencode.exe", folder / "manifest.json"
    for file in (executable, manifest):
        if not file.is_file() or file.is_symlink():
            raise ValueError("Advisor runtime is incomplete")
    value = json.loads(manifest.read_text(encoding="utf-8"))
    if (value.get("schema") != 1 or value.get("version") != VERSION or
            value.get("file") != "opencode.exe" or value.get("sha256") != EXE_SHA256 or
            value.get("bytes") != EXE_BYTES or executable.stat().st_size != EXE_BYTES or
            digest(executable) != EXE_SHA256):
        raise ValueError("Advisor runtime checksum differs from the fixed release")
    return value


def main():
    destination = ROOT / ".local/advisor-engine"
    if destination.is_dir():
        checked_engine(destination)
        print("Advisor runtime is already installed and verified.")
        return
    parent = ROOT / ".local"
    parent.mkdir(exist_ok=True)
    if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
        raise ValueError("Advisor install root is unsafe")
    with tempfile.TemporaryDirectory(prefix="advisor-engine-install-", dir=parent) as temporary:
        work = Path(temporary)
        archive = work / ARCHIVE
        request = urllib.request.Request(URL, headers={"User-Agent": "WechatVibe-Advisor-setup"})
        with urllib.request.urlopen(request, timeout=120) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        if digest(archive) != ARCHIVE_SHA256:
            raise ValueError("Advisor download checksum is invalid")
        stage = work / "engine"
        stage.mkdir()
        with zipfile.ZipFile(archive) as content:
            entries = [item for item in content.infolist() if not item.is_dir()]
            if len(entries) != 1 or Path(entries[0].filename).name != "opencode.exe":
                raise ValueError("Advisor download layout is invalid")
            with content.open(entries[0]) as source, (stage / "opencode.exe").open("wb") as target:
                shutil.copyfileobj(source, target)
        value = {"schema": 1, "version": VERSION, "file": "opencode.exe", "sha256": EXE_SHA256,
                 "bytes": EXE_BYTES, "upstream": {"archiveName": ARCHIVE,
                 "archiveSha256": ARCHIVE_SHA256, "tag": f"v{VERSION}"}}
        (stage / "manifest.json").write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        checked_engine(stage)
        stage.rename(destination)
    print("Advisor runtime installed and verified.")


if __name__ == "__main__":
    main()
