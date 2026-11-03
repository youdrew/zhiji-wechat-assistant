"""Stage reviewed public client inputs into a new runtime stage."""
# Modified by Know Your Friends, 2026-10-10 (Apache-2.0): include scoped session and recovery modules.
# Modified by Know Your Friends, 2026-10-13 (Apache-2.0): no Laya model or retired analysis files.
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from community_package import COMMUNITY_PLUGINS, check_community_path, check_source_snapshot, source_revision
import msvc_runtime


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (
    "start-real-client.py", "start-real-client.cmd", "desktop-main.cjs",
    "real-client-shell.cjs", "real-client-preload.cjs", "real-client-recovery.cjs",
    "settings-window.cjs",
    "self-analysis-window.cjs", "self-analysis-preload.cjs",
    "real-client-update.cjs", "real-client-update-proxy.cjs",
    "real-client-update-controller.cjs",
    "real-client-update-helper.cjs", "real-client-update-extract.py",
    # model-files.json stays for the updater, which preserves a model an older install bundled.
    "update-signing.pub", "model-files.json",
    "model-probe.ts", "model-gateway.ts", "embedding-model.json", "embedding-runner.cjs",
    "judge-model.json", "judge-runtime.json", "judge-selftest.json",
    "msvc-runtime.json", "msvc_runtime.py", "pe_imports.py", "verify-msvc-runtime.py",
    "plugin-windows.cjs", "plugin-panel-preload.cjs",
    # Modified by Know Your Friends, 2026-10-27: the MCP stdio adapter for local AI clients.
    "mcp-stdio-adapter.py",
    "model-traces-host.cjs", "model-traces-window.cjs", "model-traces-preload.cjs",
    "image-preview.cjs", "chat-link-host.cjs",
    # Modified by Know Your Friends, 2026-10-10: independent 知己分析 windows.
    "friend-windows.cjs", "friend-preload.cjs",
    "history-windows.cjs", "history-preload.cjs",
    "external-import-windows.cjs", "external-import-preload.cjs",
    # Modified by Know Your Friends, 2026-10-27: the phase-5 high-impact deletion confirm window,
    # raised on demand by Core's presenter adapter (bridge/core_mcp_confirm_window.py).
    "confirm-window.cjs", "confirm-window-preload.cjs",
)
BRIDGE = (
    "account_api.py", "account_store.py", "conversation_selection.py",
    "cache_source.py", "chat_server.py", "data_root_source.py", "history_browser.py",
    "instance_identity.py", "live_source.py", "model_source.py",
    "backend_contracts.py", "backend_service.py", "message_input.py", "message_links.py",
    "wechat_source.py", "real_http.py", "snapshot_cache.py", "wechat_bridge.py",
    "windows_file_owners.py",
    # Modified by Know Your Friends, 2026-10-13: Core helpers of the plugin architecture.
    "core_runtime.py", "model_probe.py", "model_capabilities.py", "legacy_portrait.py",
    "core_plugins.py", "core_plugin_settings.py", "core_model_settings.py", "core_store.py", "core_jobs.py", "core_plugin_process.py", "core_supervisor.py",
    "core_tasks.py", "core_history.py", "core_human.py", "core_events.py", "core_services.py", "core_http.py",
    "plugin_api.py", "core_gateway.py", "core_embeddings.py", "core_model_assets.py", "core_judge.py", "usage_ledger.py", "usage_names.py",
    "core_external_store.py", "core_external_import.py", "core_external_media.py", "external_import_http.py",
    "core_contracts.py", "core_account_db.py", "core_projection.py", "core_accounts.py", "core_migration.py",
    # Modified 2026-10-27: MCP capability proxy, its store and the settings-channel admin.
    "core_mcp_proxy.py", "core_mcp_store.py", "core_mcp_admin.py",
    # Modified by Know Your Friends, 2026-10-27: the shared-Core leases and the saved-account offline scope.
    "core_leases.py", "core_mcp_offline.py",
    # Task, assistant and import capabilities share the Core proxy.
    "core_mcp_tasks.py", "core_mcp_assistant.py", "core_mcp_import.py",
    # Modified by Know Your Friends, 2026-10-27: phase-5 application management and the
    # high-impact deletion confirmation service, plus the Electron confirm window presenter.
    "core_mcp_manage.py", "core_mcp_confirm.py", "core_mcp_confirm_window.py",
    "model_trace_context.py", "model_traces.py", "model_traces_store.py", "model_traces_http.py",
    "friend_context.py",
    # Modified by Know Your Friends, 2026-10-10: local media and bounded vision context.
    "media_assets.py", "media_database.py", "media_keys.py", "sticker_download.py", "sticker_wxgf.py",
    # Modified by Know Your Friends, 2026-10-10: 知己分析 memories, jobs and trends.
    # Modified by Know Your Friends, 2026-10-13: the analysis itself ships in plugins/kyf.friend-analysis.
    "friend_store.py", "friend_service.py", "friend_http.py",

)
NATIVE_READER = (
    "__init__.py", "crypto.py", "database.py", "discovery.py", "errors.py",
    "fixture.py", "log.py", "png_encode.py", "protocol.py", "service.py",
    "snapshot.py", "wal.py", "window.py", "window_capture.py", "window_uia.py",
)
PUBLIC_FILES = (
    "LICENSE", "THIRD_PARTY_NOTICES.md", "README.md", "chatui/index.html",
    "licenses/Microsoft-Visual-Cpp-Runtime-2015-2022.rtf",
    "licenses/Microsoft-Visual-Cpp-Runtime-NOTICE.md",
    "licenses/Laya-Apache-2.0.txt", "licenses/Laya-NOTICE.txt",
    "chatui/app.js", "chatui/message-links.js", "chatui/conversation-menu.js", "chatui/message-menu.js", "chatui/session-resize.js", "chatui/composer-resize.js", "chatui/view-state.js", "chatui/style.css", "chatui/kaomoji.js",
    "chatui/composer-drafts.js", "chatui/chat-composer.js",
    "chatui/assets/app-icon.png",
    "chatui/assets/app-icon.ico",
    "chatui/settings-window.js", "chatui/settings-window.css",
    "chatui/self-analysis.html", "chatui/self-analysis-window.js", "chatui/self-analysis-window.css",
    "electron/model-connectors.ts",
    "electron/model-timeout.ts",
    "electron/model-traces.ts", "electron/model-trace-client.ts", "electron/model-trace-relay.ts",
    "electron/model-gateway.ts", "electron/model-gateway-queue.ts", "electron/model-gateway-meter.ts", "electron/model-gateway-local.ts",
    "chatui/model-traces.js", "chatui/model-traces.css", "chatui/model-settings.js",
    "chatui/model-traces-window.html", "chatui/model-traces-window.js", "chatui/model-usage.js",
    "chatui/plugin-settings.js", "chatui/plugin-configuration.js", "chatui/plugin-settings.css",
    "chatui/external-import.html", "chatui/external-import.js", "chatui/external-import.css", "chatui/external-import-host.js",
    "chatui/plugin-frame.js", "chatui/plugin-slots.js", "chatui/plugin-host.css",
    "chatui/message-analysis.js", "chatui/message-analysis.css",
    "licenses/OpenCode-MIT.txt", "licenses/BGE-MIT.txt",
    "licenses/llama.cpp-MIT.txt", "licenses/Julia-1-Apache-2.0.txt", "licenses/LLVM-OpenMP-Apache-2.0.txt",
    "chatui/media.js", "chatui/media.css", "chatui/media-settings.js",
    "chatui/image-viewer.html", "chatui/image-viewer.css", "chatui/image-viewer.js",
    "chatui/friend.html", "chatui/friend.css", "chatui/friend-core.js", "chatui/friend-memory.js",
    "chatui/friend-run.js", "chatui/friend-scheme.js", "chatui/friend.js",
    "chatui/history.html", "chatui/history-panel.js", "chatui/history-panel.css",
    "licenses/Skype-SILK-BSD.txt", "licenses/silk-v3-decoder-MIT.txt",
    "licenses/imageio-ffmpeg-BSD.txt", "licenses/FFmpeg-LGPL-2.1.txt",
    "licenses/wechat-exporter-MIT.txt",
    "chatui/plugin-panel.html", "chatui/plugin-panel.css", "chatui/plugin-panel.js",
    "shared/message-input.ts",
    "native-reader/THIRD_PARTY_NOTICES.md",
)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def inventory(root: Path) -> dict[str, Path]:
    if root.is_symlink() or getattr(root, "is_junction", lambda: False)():
        raise ValueError(f"linked staging root: {root}")
    found = {}
    if not root.exists():
        return found
    if not root.is_dir():
        raise ValueError(f"staging root is not a directory: {root}")
    for base, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            item = Path(base) / name
            if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
                raise ValueError(f"link in staging tree: {item}")
            if not item.is_dir() and not item.is_file():
                raise ValueError(f"unexpected staging entry: {item}")
            if item.is_file():
                relative = item.relative_to(root).as_posix()
                folded = relative.casefold()
                if folded in found:
                    raise ValueError(f"case-colliding staging file: {relative}")
                found[folded] = item
    return found


def verify_directories(root: Path, files: set[str]) -> None:
    expected = set()
    for filename in files:
        parts = Path(filename).parts
        expected.update(Path(*parts[:index]).as_posix().casefold()
                        for index in range(1, len(parts)))
    actual = set()
    for base, directories, _ in os.walk(root, followlinks=False):
        for name in directories:
            actual.add(((Path(base) / name).relative_to(root)).as_posix().casefold())
    if actual != expected:
        raise ValueError("stage has unexpected or missing directories")


def verify_runtime_stage(output: Path) -> set[str]:
    manifest = output.parent / "runtime-manifest.json"
    existing = inventory(output)
    if not manifest.exists():
        if existing:
            raise ValueError("nonempty stage lacks runtime manifest")
        if output.exists():
            verify_directories(output, set())
        return set()
    rows = json.loads(manifest.read_text(encoding="utf-8")).get("files")
    if not isinstance(rows, list):
        raise ValueError("runtime manifest lacks exact file inventory")
    expected = {}
    for row in rows:
        relative = row["file"]
        path = Path(relative)
        if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
            raise ValueError("unsafe runtime inventory path")
        folded = relative.casefold()
        if folded in expected or not (relative.startswith("runtime/") or relative.startswith("node_modules/")):
            raise ValueError(f"unexpected runtime inventory path: {relative}")
        check_community_path(relative)
        expected[folded] = row
    if set(existing) != set(expected):
        raise ValueError("runtime stage has unexpected or missing content")
    verify_directories(output, set(expected))
    for folded, row in expected.items():
        path = existing[folded]
        if path.stat().st_size != row["bytes"] or digest(path) != row["sha256"]:
            raise ValueError(f"runtime stage hash differs: {row['file']}")
    return set(expected)


# Community builds select plugin ids explicitly; adding a folder cannot add a Pro feature.
PLUGIN_TREES = ("plugin-sdk",) + tuple("plugins/" + name for name in COMMUNITY_PLUGINS)
TREE_EXCLUDED = frozenset({"__pycache__", "node_modules", "tests"})


def tree_files(source: Path, name: str) -> list[Path]:
    base = source / name
    if not base.is_dir():
        return []
    return sorted(path.relative_to(source) for path in base.rglob("*")
                  if path.is_file() and not TREE_EXCLUDED & set(path.relative_to(base).parts) and
                  path.suffix != ".pyc")


def public_mappings(source: Path) -> list[tuple[Path, Path, Path]]:
    project_files = [Path(name) for name in PUBLIC_FILES]
    for tree in PLUGIN_TREES:
        project_files += tree_files(source, tree)
    project_files += [Path("scripts") / name for name in SCRIPTS]
    project_files += [Path("bridge") / name for name in BRIDGE]
    project_files += [Path("native-reader/wr") / name for name in NATIVE_READER]
    for relative in project_files:
        check_community_path(relative)
    return [(source / relative, relative, source) for relative in project_files]


def judge_runtime_mappings(source: Path, *, include_app_local=False) -> list[tuple[Path, Path, Path]]:
    """Ship the reviewed exe/DLL/license closure; never copy a tools directory wholesale."""
    if not (source / "scripts/judge-runtime.json").is_file():
        return []  # Older source snapshots and synthetic fixtures without this feature.
    loader = importlib.util.spec_from_file_location("judge_runtime_setup", Path(__file__).with_name("setup-judge-runtime.py"))
    runtime_setup = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(runtime_setup)
    spec = runtime_setup.specification(source)
    packaged = source / "runtime/judge"
    base = packaged if packaged.exists() else source / ".local/tools/judge-runtime" / spec["version"]
    for directory in (base, *base.parents):
        if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
            raise ValueError("linked judge runtime directory")
        if directory == source:
            break
    try:
        additional = []
        if include_app_local and (source / "scripts/msvc-runtime.json").is_file():
            additional = [row for row in msvc_runtime.specification(source)["files"] if row["name"] in msvc_runtime.CRT_FILES]
        runtime_setup.checked_runtime(base, spec, additional_files=additional)
    except (OSError, ValueError, KeyError) as error:
        raise ValueError("judge runtime missing or altered; run scripts/setup-judge-runtime.py before staging") from error
    return [(base / name, Path("runtime/judge") / name, base.resolve())
            for name in [row["name"] for row in spec["files"]] + ["manifest.json"]]


def stage_public(source: Path, output: Path, *, require_clean=False) -> dict:
    source, output = source.resolve(), output.absolute()
    revision = source_revision(source, require_clean=require_clean)
    if output == source or source.is_relative_to(output):
        raise ValueError("staging output must not replace a source directory")
    if (output.parent / "client-files.json").exists():
        raise ValueError("client stage already has a manifest; create a new build directory")
    runtime_files = verify_runtime_stage(output)
    mappings = public_mappings(source) + judge_runtime_mappings(source)
    names = [relative.as_posix().casefold() for _, relative, _ in mappings]
    if len(names) != len(set(names)) or set(names) & runtime_files:
        raise ValueError("duplicate or colliding client staging path")
    version = json.loads((source / "package.json").read_text(encoding="utf-8"))["version"]
    if not isinstance(version, str) or not version:
        raise ValueError("source package version is invalid")
    prepared = []
    for original, relative, allowed_root in mappings:
        check_community_path(relative)
        if (original.is_symlink() or getattr(original, "is_junction", lambda: False)() or
                not original.is_file() or not original.resolve().is_relative_to(allowed_root)):
            raise ValueError(f"missing or unsafe public input: {relative.as_posix()}")
        length, checksum = original.stat().st_size, digest(original)
        prepared.append((original, relative, length, checksum))
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    for original, relative, length, checksum in prepared:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)
        if target.stat().st_size != length or digest(target) != checksum or digest(original) != checksum:
            raise ValueError(f"client input changed while staging: {relative.as_posix()}")
        rows.append({"file": relative.as_posix(), "bytes": length, "sha256": checksum})
    metadata = output / "package.json"
    metadata.write_text(json.dumps({"name": "know-your-friends-runtime", "version": version,
                                    "private": True, "type": "module"}, indent=2) + "\n",
                        encoding="utf-8")
    rows.append({"file": "package.json", "bytes": metadata.stat().st_size,
                 "sha256": digest(metadata)})
    expected = runtime_files | {row["file"].casefold() for row in rows}
    if set(inventory(output)) != expected:
        raise ValueError("client stage has unexpected or missing content")
    verify_directories(output, expected)
    # Check the copied manifest against the copied bytes again: a publish racing
    # staging must not mix one build's manifest with another build's binaries.
    judge_runtime_mappings(output, include_app_local=True)
    if (output / "scripts/msvc-runtime.json").is_file():
        msvc_runtime.verify_deployment(output, msvc_runtime.specification(output))
    check_source_snapshot(revision, source_revision(source, require_clean=require_clean), require_clean=require_clean)
    result = {"sourceVersion": version, **revision, "sourcePackageSha256": digest(source / "package.json"),
              "files": rows}
    (output.parent / "client-files.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--require-clean", action="store_true", help="require an exact committed source tree for a formal package")
    parser.add_argument("--output", type=Path, required=True,
                        help="new stage created by the clean build driver")
    args = parser.parse_args(argv)
    source = args.source_root.resolve()
    try:
        result = stage_public(source, args.output, require_clean=args.require_clean)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        parser.exit(1, f"Client stage rejected: {error}\n")
    print(json.dumps({"publicFiles": len(result["files"]),
                      "bytes": sum(row["bytes"] for row in result["files"]),
                      "sourceVersion": result["sourceVersion"],
                      "userDataCopied": False, "output": str(args.output.absolute())}, ensure_ascii=True))


if __name__ == "__main__":
    main()
