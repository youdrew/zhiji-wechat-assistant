"""Reviewed package boundary for the free, single-computer community edition."""
from pathlib import Path, PurePosixPath
import subprocess


COMMUNITY_PLUGINS = (
    "kyf.advisor", "kyf.external-chat-import", "kyf.friend-analysis",
    "kyf.mcp", "kyf.message-insights", "kyf.self-analysis",
)
WECHATAUTO_READ_FILES = frozenset({
    "__init__.py", "db.py", "media.py", "logger.py", "param.py", "KYF_READ_ONLY.md",
})
PRIVATE_PARTS = frozenset({
    ".local", ".git", ".venv", "__pycache__", "logs", "cache", "caches",
    "accounts", "account-data", "userdata", "user-data", "avatars", "avator",
})
PRIVATE_NAMES = frozenset({
    "auth.json", "credentials.json", "credential.json", "tokens.json", "token.json",
    "settings.json", "app.json", ".env", "id_rsa", "id_ed25519",
})
PRIVATE_SUFFIXES = frozenset({
    ".db", ".sqlite", ".sqlite3", ".log", ".key", ".pem", ".p12", ".pfx", ".lic", ".pdb",
    ".exe", ".dll", ".pyd", ".so", ".dylib",
})
PLUGIN_JSON_FILES = frozenset({"plugins/" + name + "/plugin.json" for name in COMMUNITY_PLUGINS} | {
    "plugins/kyf.self-analysis/backend/8values.json",
    "plugins/kyf.self-analysis/backend/ecr-rs-terms.json",
    "plugins/kyf.self-analysis/backend/ipip50-terms.json",
    "plugins/kyf.self-analysis/backend/worldview-terms.json",
})


def source_revision(root: Path, *, require_clean=False):
    """Keep the Git source identity separate from package.json's content hash."""
    if not (root / ".git").exists():
        if require_clean:
            raise ValueError("a formal build requires a clean Git checkout")
        return {"sourceGitSha": None, "sourceDirty": None}
    try:
        sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30).stdout.decode().strip()
        dirty = bool(subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
                                    check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=30).stdout.strip())
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise ValueError("cannot determine the Git source revision") from None
    if len(sha) != 40 or any(char not in "0123456789abcdef" for char in sha):
        raise ValueError("invalid Git source revision")
    if require_clean and dirty:
        raise ValueError("a formal build requires a clean Git checkout")
    return {"sourceGitSha": sha, "sourceDirty": dirty}


def check_source_snapshot(expected, current, *, require_clean=False):
    if require_clean and (not expected.get("sourceGitSha") or expected.get("sourceDirty") is not False):
        raise ValueError("a formal build cannot reuse a dirty or unbound source snapshot")
    if expected.get("sourceGitSha") is not None and current.get("sourceGitSha") != expected["sourceGitSha"]:
        raise ValueError("source Git revision changed during build")
    if require_clean and current.get("sourceDirty") is not False:
        raise ValueError("source checkout changed during the formal build")


def check_community_path(relative):
    """Reject edition leaks even when an earlier manifest mistakenly permits them."""
    path = PurePosixPath(str(relative).replace("\\", "/"))
    parts = tuple(part.casefold() for part in path.parts)
    if not parts:
        return
    if ("native-sender" in parts or "kyf.wechat-send" in parts or
            any("wechat-send" in part or "wechat-sdk" in part or "wechatauto.sdk" in part
                for part in parts)):
        raise ValueError(f"community package forbids WeChat sending resources: {path}")
    if parts[0] in {"plugins", "plugin-sdk"}:
        if parts == ("plugins",):
            return
        if parts[0] == "plugins" and parts[1] not in COMMUNITY_PLUGINS:
            raise ValueError(f"community package forbids unreviewed plugin: {path}")
        if (set(parts) & PRIVATE_PARTS or parts[-1] in PRIVATE_NAMES or
                any(part.startswith(".env.") for part in parts) or
                path.suffix.casefold() in PRIVATE_SUFFIXES or
                any(parts[-1].endswith(suffix) for suffix in ("-wal", "-shm", "-journal"))):
            raise ValueError(f"community package forbids plugin runtime data: {path}")
        if parts[0] == "plugins" and path.suffix.casefold() == ".json" and "/".join(parts) not in PLUGIN_JSON_FILES:
            raise ValueError(f"community package forbids unreviewed plugin JSON: {path}")
    marker = ("runtime", "python", "lib", "site-packages", "wechatauto")
    if parts[:len(marker)] == marker:
        if len(parts) != len(marker) and (len(parts) != len(marker) + 1 or path.name not in WECHATAUTO_READ_FILES):
            raise ValueError(f"community package forbids non-reader wechatauto module: {path}")
    imageio = ("runtime", "python", "lib", "site-packages", "imageio_ffmpeg", "binaries")
    if (parts[:len(imageio)] == imageio and path.suffix.casefold() == ".exe" and
            path.name != "ffmpeg-zhiji-lgpl.exe"):
        raise ValueError(f"community package forbids the unreviewed wheel FFmpeg executable: {path}")
