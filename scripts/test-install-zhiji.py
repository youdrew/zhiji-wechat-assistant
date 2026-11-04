"""install-zhiji.ps1 against synthetic installations and installer bytes.

The success path mocks the installer process and launches only a test CLI executable.
Registry and download tests use PowerShell mocks or disable system discovery.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SCRIPT = SCRIPTS / "install-zhiji.ps1"
PLACEHOLDER = "__ZHIJI_DOWNLOAD_URL__"
POWERSHELL = shutil.which("powershell")


def ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(sys.platform == "win32" and POWERSHELL, "Windows PowerShell required")
class InstallBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zhiji-bootstrap-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.temp = self.tmp / "temp"
        self.temp.mkdir()

    def fake_install(self):
        client = self.tmp / "安装 目录" / "resources" / "client"
        for relative in ("bridge/chat_server.py", "scripts/mcp-stdio-adapter.py", "scripts/zhiji-mcp-cli.py",
                         "runtime/python/python.exe"):
            (client / relative).parent.mkdir(parents=True, exist_ok=True)
            (client / relative).write_text("# synthetic\n", encoding="utf-8")
        return client.parent.parent

    def run_script(self, *args, script=SCRIPT, setup="", system_discovery=False):
        quoted = " ".join(ps_literal(arg) if not str(arg).startswith("-") or " " in str(arg)
                          else str(arg) for arg in args)
        command = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8\n"
                   f"{setup}\n& {ps_literal(script)} {quoted}\nexit $LASTEXITCODE")
        env = {**os.environ, "ZHIJI_CLI_NO_SYSTEM_DISCOVERY": "1", "TEMP": str(self.temp), "TMP": str(self.temp)}
        env.pop("ZHIJI_ROOT", None)
        if system_discovery:
            env.pop("ZHIJI_CLI_NO_SYSTEM_DISCOVERY", None)
        completed = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                    "-Command", command], capture_output=True, env=env, timeout=120,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        output = completed.stdout.decode("utf-8", "replace") + completed.stderr.decode("utf-8", "replace")
        return completed.returncode, output

    def test_installed_whatif_shows_connect_command_with_passthrough_args(self):
        install = self.fake_install()
        code, output = self.run_script("-InstallRoot", install, "-Client", "codex", "-WhatIf", "--name", "work")
        self.assertEqual(code, 0, output)
        self.assertIn("安装：就绪", output)
        self.assertIn(str(install / "resources" / "client"), output)
        self.assertIn("（演练）将运行：", output)
        self.assertIn("connect --client codex", output)
        self.assertIn("--name work", output)
        self.assertEqual(list(self.temp.iterdir()), [])

    def test_explicit_root_that_is_not_an_installation_fails(self):
        code, output = self.run_script("-InstallRoot", self.tmp, "-WhatIf")
        self.assertEqual(code, 1, output)
        self.assertIn("不是完整的知己安装目录", output)

    def test_not_installed_with_placeholder_url_prints_guidance(self):
        self.assertEqual(SCRIPT.read_text(encoding="utf-8-sig").count(f"$DownloadUrl = '{PLACEHOLDER}'"), 1)
        code, output = self.run_script("-Client", "claude-code", "-Yes")
        self.assertEqual(code, 2, output)
        self.assertIn("未安装", output)
        self.assertIn("安装包", output)
        self.assertIn("-InstallerUrl", output)
        self.assertEqual(list(self.temp.iterdir()), [])

    def test_build_filled_url_and_installer_url_stop_at_whatif(self):
        filled = self.tmp / "install-zhiji.ps1"
        filled.write_bytes(SCRIPT.read_bytes().replace(PLACEHOLDER.encode(), b"https://downloads.example/zhiji.exe")
                           .replace(b"__ZHIJI_INSTALLER_SHA256__", b"a" * 64))
        code, output = self.run_script("-WhatIf", "-Client", "codex", "--name", "工作用", script=filled)
        self.assertEqual(code, 0, output)
        self.assertIn("https://downloads.example/zhiji.exe", output)
        self.assertIn("（演练）不下载、不安装。", output)
        self.assertIn("connect --client codex --name 工作用", output)
        code, output = self.run_script("-InstallerUrl", "https://other.example/setup.exe", "-Sha256", "a" * 64, "-WhatIf")
        self.assertEqual(code, 0, output)
        self.assertIn("https://other.example/setup.exe", output)
        self.assertEqual(list(self.temp.iterdir()), [])

    def test_checksum_mismatch_refuses_to_run_and_removes_the_copy(self):
        installer = self.tmp / "setup.exe"
        installer.write_bytes(b"not an installer")
        code, output = self.run_script("-InstallerUrl", installer, "-Sha256", "0" * 64, "-Yes")
        self.assertEqual(code, 1, output)
        self.assertIn("校验失败", output)
        self.assertIn("未运行安装包", output)
        self.assertEqual(list(self.temp.iterdir()), [])

    def test_explicit_installer_without_trusted_hash_refuses_before_copy(self):
        code, output = self.run_script("-InstallerUrl", self.tmp / "missing.exe", "-Yes")
        self.assertEqual(code, 1, output)
        self.assertIn("64 位 -Sha256", output)
        self.assertEqual(list(self.temp.iterdir()), [])

    def test_plain_http_is_refused(self):
        code, output = self.run_script("-InstallerUrl", "http://insecure.example/setup.exe", "-Yes")
        self.assertEqual(code, 1, output)
        self.assertIn("HTTPS", output)

    def test_registry_discovery_skips_bad_quote_and_accepts_arguments(self):
        install = self.fake_install()
        uninstall = f'"{install / "Uninstall KnowYourFriends.exe"}" /S'
        setup = f"""
function global:Get-ChildItem {{
    [CmdletBinding()] param([string]$LiteralPath)
    if ($LiteralPath -like 'HKCU:*') {{
        [pscustomobject]@{{ PSPath = 'broken' }}
        [pscustomobject]@{{ PSPath = 'valid' }}
    }}
}}
function global:Get-ItemProperty {{
    [CmdletBinding()] param([string]$LiteralPath)
    $commandLine = if ($LiteralPath -eq 'broken') {{ '"C:\\broken.exe' }} else {{ {ps_literal(uninstall)} }}
    [pscustomobject]@{{ DisplayName = 'Zhiji WeChat Assistant'; UninstallString = $commandLine }}
}}
"""
        code, output = self.run_script("-WhatIf", setup=setup, system_discovery=True)
        self.assertEqual(code, 0, output)
        self.assertIn(str(install / "resources" / "client"), output)
        self.assertIn("（注册表）", output)

    def test_download_failure_removes_partial_file_without_printing_query(self):
        setup = """
function global:Invoke-WebRequest {
    param([uri]$Uri, [string]$OutFile, [switch]$UseBasicParsing, [int]$TimeoutSec)
    [IO.File]::WriteAllText($OutFile, 'synthetic partial response')
    throw 'synthetic network failure'
}
"""
        code, output = self.run_script("-InstallerUrl", "https://downloads.example/setup.exe?key=synthetic-query",
                                       "-Sha256", "a" * 64, "-Yes", setup=setup)
        self.assertEqual(code, 1, output)
        self.assertIn("无法从 https://downloads.example/setup.exe 下载", output)
        self.assertNotIn("synthetic-query", output)
        self.assertEqual(list(self.temp.iterdir()), [])

    def test_verified_installation_rediscovery_connects_and_preserves_cli_output_and_status(self):
        install = self.fake_install()
        client = install / "resources" / "client"
        python = client / "runtime" / "python" / "python.exe"
        python.unlink()
        installer = self.tmp / "setup.exe"
        installer.write_bytes(b"synthetic installer fixture")
        sha256 = hashlib.sha256(installer.read_bytes()).hexdigest()
        filled = self.tmp / "install-zhiji.ps1"
        filled.write_bytes(SCRIPT.read_bytes().replace(b"__ZHIJI_INSTALLER_FILE_NAME__", b"setup.exe")
                           .replace(b"__ZHIJI_INSTALLER_SHA256__", sha256.encode()))
        setup = f"""
$fixtureCode = @'
using System;
class FakeCli {{
    static int Main(string[] args) {{
        Console.OutputEncoding = new System.Text.UTF8Encoding(false);
        Console.WriteLine("SYNTHETIC-CONNECT:" + String.Join("|", args));
        return 17;
    }}
}}
'@
if (-not [IO.File]::Exists({ps_literal(python)})) {{
    Add-Type -TypeDefinition $fixtureCode -OutputType ConsoleApplication -OutputAssembly {ps_literal(python)}
}}
function global:Start-Process {{
    param([string]$FilePath, [switch]$Wait, [switch]$PassThru)
    if ([IO.File]::ReadAllText($FilePath) -ne 'synthetic installer fixture') {{ throw 'wrong installer bytes' }}
    if (-not $Wait -or -not $PassThru) {{ throw 'bootstrap must wait for installer' }}
    $env:ZHIJI_ROOT = {ps_literal(install)}
    [pscustomobject]@{{ ExitCode = 0 }}
}}
"""
        expected = (f"SYNTHETIC-CONNECT:{client / 'scripts/zhiji-mcp-cli.py'}|connect|--client|codex"
                    f"|--root|{client}|--name|work profile")
        for script, args in ((filled, ()), (SCRIPT, ("-InstallerUrl", installer.as_uri(), "-Sha256", sha256))):
            with self.subTest(script=script):
                code, output = self.run_script("-Client", "codex", "-Yes", *args, "--name", "work profile",
                                               setup=setup, script=script)
                self.assertEqual(code, 17, output)
                self.assertIn("校验：SHA256 一致。", output)
                self.assertIn(expected, output)
                self.assertEqual(list(self.temp.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
