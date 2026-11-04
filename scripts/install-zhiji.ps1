<#
.SYNOPSIS
  知己微信助手 · 一键接入 AI 客户端（未安装时先安装）。

.DESCRIPTION
  Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

  独立脚本，只依赖 Windows PowerShell 5.1，放在下载站与安装包并列提供：
    1. 按与 zhiji-mcp-cli.py 相同的规则查找已安装的知己
       （-InstallRoot / ZHIJI_ROOT → 注册表卸载项 → 默认安装目录）；
    2. 未安装时，从 -InstallerUrl、同目录的配套安装包或构建时指定的 HTTPS 地址获取安装包。
       使用发布构建写入的 SHA256 或经可信渠道获取的 -Sha256 校验，通过后才运行；
    3. 用安装自带的 Python 运行
       zhiji-mcp-cli.py connect --client <Client> [其余参数原样传递]。
  -WhatIf 只显示将要做的事，不下载、不安装、不运行 connect。

  源码脚本首次安装时须提供 -InstallerUrl 和 -Sha256。配套脚本使用构建生成的安装包名称
  和哈希。请从信任的发布渠道获取脚本或哈希。-Yes 跳过本脚本的安装确认。

  退出码：0 成功；1 下载、校验或安装失败；2 未安装且没有可用的下载地址；
  其余为 zhiji-mcp-cli.py connect 的退出码。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install-zhiji.ps1 -Client claude-code --install-hook
#>
[CmdletBinding(SupportsShouldProcess = $true, PositionalBinding = $false)]
param(
    [ValidateSet('claude-code', 'codex', 'cursor', 'deepseek-harness', 'generic')]
    [string]$Client = 'claude-code',
    [string]$InstallerUrl,
    [string]$Sha256,
    [string]$InstallRoot,
    [switch]$Yes,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ConnectArgs
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

# 发布构建替换下列值；保持单引号、各占一行。
$DownloadUrl = '__ZHIJI_DOWNLOAD_URL__'
$InstallerFileName = '__ZHIJI_INSTALLER_FILE_NAME__'
$InstallerSha256 = '__ZHIJI_INSTALLER_SHA256__'
$ProductNames = @('知己微信助手', 'Zhiji WeChat Assistant')
$ExitNotInstalled = 2

function Say([string]$Message) { Write-Host $Message }

function Get-ClientRoot([string]$Path) {
    $nested = Join-Path $Path 'resources\client'
    if (Test-Path -LiteralPath $nested -PathType Container) { return $nested }
    return $Path
}

function Test-Installation([string]$Root) {
    foreach ($relative in @('bridge\chat_server.py', 'scripts\mcp-stdio-adapter.py', 'scripts\zhiji-mcp-cli.py',
                            'runtime\python\python.exe')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Root $relative) -PathType Leaf)) { return $false }
    }
    return $true
}

function Get-UninstallDirectory([string]$Command) {
    $commandLine = $Command.Trim()
    if ($commandLine -match '^"([^"]+\.exe)"(?:\s|$)') { $executable = $Matches[1] }
    elseif ($commandLine -match '^([^"].*?\.exe)(?:\s|$)') { $executable = $Matches[1] }
    else { return }
    if ([IO.Path]::IsPathRooted($executable)) { Split-Path -Parent $executable }
}

function Get-RegistryString($Values, [string]$Name) {
    $property = $Values.PSObject.Properties[$Name]
    if ($null -eq $property) { return '' }
    return [string]$property.Value
}

function Get-RegistryLocations {
    $found = @()
    $bases = @('HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
               'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
               'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall')
    foreach ($base in $bases) {
        try { $keys = Get-ChildItem -LiteralPath $base -ErrorAction Stop } catch { continue }
        foreach ($key in $keys) {
            try {
                $values = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction Stop
                $display = Get-RegistryString $values 'DisplayName'
                if (-not ($ProductNames | Where-Object { $display.StartsWith($_) })) { continue }
                $location = Get-RegistryString $values 'InstallLocation'
                if ($location) { $found += $location; continue }
                $uninstall = Get-RegistryString $values 'UninstallString'
                $location = Get-UninstallDirectory $uninstall
                if ($location) { $found += $location }
            } catch { continue }
        }
    }
    return $found
}

function Get-KnownLocations {
    $found = @()
    if ($env:LOCALAPPDATA) {
        foreach ($name in @('知己微信助手', 'know-your-friends', 'KnowYourFriends')) {
            $found += (Join-Path $env:LOCALAPPDATA "Programs\$name")
        }
    }
    if ($env:ProgramFiles) { $found += (Join-Path $env:ProgramFiles '知己微信助手') }
    return $found
}

function Find-Installation {
    $explicit = if ($InstallRoot) { $InstallRoot } elseif ($env:ZHIJI_ROOT) { $env:ZHIJI_ROOT } else { $null }
    if ($explicit) {
        $root = Get-ClientRoot $explicit
        if (-not (Test-Installation $root)) {
            throw "不是完整的知己安装目录：$explicit（应包含 resources\client 下的 bridge、scripts 与 runtime\python）"
        }
        return @{ Root = (Resolve-Path -LiteralPath $root).ProviderPath; Source = '指定目录' }
    }
    if ($env:ZHIJI_CLI_NO_SYSTEM_DISCOVERY) { return $null }
    foreach ($pair in @(@('注册表', (Get-RegistryLocations)), @('默认安装目录', (Get-KnownLocations)))) {
        foreach ($location in @($pair[1])) {
            if (-not $location) { continue }
            try {
                $root = Get-ClientRoot $location
                if (Test-Installation $root) {
                    return @{ Root = (Resolve-Path -LiteralPath $root).ProviderPath; Source = $pair[0] }
                }
            } catch { continue }
        }
    }
    return $null
}

function Get-InstallerSource([string]$Location) {
    if ($Location -match '^https://') {
        $uri = $null
        if (-not [uri]::TryCreate($Location, [UriKind]::Absolute, [ref]$uri) -or
            -not $uri.Host -or $uri.UserInfo -or $uri.Fragment) { throw '安装包 HTTPS 地址不正确。' }
        return @{ Uri = $uri; Path = $null; Display = $uri.GetLeftPart([UriPartial]::Path) }
    }
    if ($Location -match '^file:') {
        $uri = $null
        if (-not [uri]::TryCreate($Location, [UriKind]::Absolute, [ref]$uri) -or -not $uri.IsFile -or $uri.IsUnc) {
            throw '安装包文件地址必须指向本机文件。'
        }
        $Location = $uri.LocalPath
    } elseif ($Location -match '^[a-z][a-z0-9+.-]*:' -and $Location -notmatch '^[a-z]:[\\/]') {
        throw '安装包地址必须是 HTTPS 或本机文件路径。'
    }
    if ($Location -match '^[\\/]{2}') { throw '安装包文件地址必须指向本机文件。' }
    return @{ Uri = $null; Path = $Location; Display = $Location }
}

function Copy-Installer($Source, [string]$Target) {
    if ($Source.Path) {
        Copy-Item -LiteralPath $Source.Path -Destination $Target
        return
    }
    $protocols = [Net.ServicePointManager]::SecurityProtocol
    $progress = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'
    try {
        [Net.ServicePointManager]::SecurityProtocol = $protocols -bor [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $Source.Uri -OutFile $Target -UseBasicParsing -TimeoutSec 600
    } catch {
        throw "无法从 $($Source.Display) 下载知己安装包。"
    } finally {
        [Net.ServicePointManager]::SecurityProtocol = $protocols
        $ProgressPreference = $progress
    }
}

function Get-InstallerHash([string]$Path) {
    $hash = [Security.Cryptography.SHA256]::Create()
    try {
        $stream = [IO.File]::OpenRead($Path)
        try { return [BitConverter]::ToString($hash.ComputeHash($stream)).Replace('-', '') }
        finally { $stream.Dispose() }
    } finally { $hash.Dispose() }
}

function Install-Zhiji {
    $location = $InstallerUrl
    if (-not $location -and -not $InstallerFileName.StartsWith('__')) {
        $sibling = Join-Path $PSScriptRoot $InstallerFileName
        if (Test-Path -LiteralPath $sibling -PathType Leaf) { $location = $sibling }
    }
    if (-not $location -and -not $DownloadUrl.StartsWith('__')) { $location = $DownloadUrl }
    if (-not $location) {
        Say '安装：未安装，未找到配套安装包或内置下载地址。'
        Say '请从信任的发布渠道获取 Windows 安装包，或用 -InstallerUrl <地址> -Sha256 <哈希> 指定来源。'
        Say '已安装在其他位置时可加 -InstallRoot <安装目录>。'
        return $ExitNotInstalled
    }
    $source = Get-InstallerSource $location
    $expected = if ($Sha256) { $Sha256.Trim() } else { $InstallerSha256 }
    if ($expected -notmatch '^[0-9a-fA-F]{64}$') {
        throw '请提供发布者通过可信渠道提供的 64 位 -Sha256，或使用发布构建生成的配套脚本。'
    }
    Say "安装：未安装，将从 $($source.Display) 获取知己微信助手安装包并校验 SHA256。"
    if (-not $PSCmdlet.ShouldProcess($source.Display, '校验并运行知己微信助手安装包')) {
        if ($WhatIfPreference) {
            Say '（演练）不下载、不安装。'
            return 0
        }
        Say '已取消安装。'
        return 1
    }
    if (-not $Yes) {
        $answer = Read-Host '下载并安装知己微信助手？[y/N]'
        if ($answer -notmatch '^(y|yes|是)$') { Say '已取消，未做任何修改。'; return 1 }
    }
    $installer = Join-Path ([IO.Path]::GetTempPath()) ("Zhiji-Setup-" + [guid]::NewGuid().ToString('N') + '.exe')
    try {
        Copy-Installer $source $installer
        $actual = Get-InstallerHash $installer
        if ($actual -ne $expected) {
            Say "校验失败：安装包 SHA256 为 $actual，与预期哈希不一致；未运行安装包。"
            return 1
        }
        Say '校验：SHA256 一致。'
        Say '正在运行安装包，请按安装程序提示完成…'
        $process = Start-Process -FilePath $installer -Wait -PassThru
        if ($process.ExitCode -ne 0) { Say "安装包退出码 $($process.ExitCode)，安装未完成。"; return 1 }
    } finally {
        Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    }
    return 0
}

try {
    $forwarded = @($ConnectArgs | Where-Object { $_ -ne $null })
    if ($forwarded.Count -and $forwarded[0] -eq '--') { $forwarded = @($forwarded | Select-Object -Skip 1) }
    $inst = Find-Installation
    if (-not $inst) {
        $code = Install-Zhiji
        if ($code -ne 0) { exit $code }
        if ($WhatIfPreference) {
            $plan = (@('connect', '--client', $Client) + $forwarded) -join ' '
            Say "（演练）将运行：用安装自带的 Python 执行 zhiji-mcp-cli.py $plan"
            exit 0
        }
        $inst = Find-Installation
        if (-not $inst) { Say '安装后仍未找到知己安装目录；请加 -InstallRoot <安装目录> 重试接入。'; exit 1 }
    }
    Say "安装：就绪 —— $($inst.Root)（$($inst.Source)）"
    $python = Join-Path $inst.Root 'runtime\python\python.exe'
    $cli = Join-Path $inst.Root 'scripts\zhiji-mcp-cli.py'
    $arguments = @($cli, 'connect', '--client', $Client, '--root', $inst.Root) + $forwarded
    $display = (@($python) + $arguments | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join ' '
    if (-not $PSCmdlet.ShouldProcess($display, '运行 zhiji-mcp-cli.py connect')) {
        if ($WhatIfPreference) {
            Say "（演练）将运行：$display"
            exit 0
        }
        Say '已取消接入。'
        exit 1
    }
    & $python @arguments
    exit $LASTEXITCODE
} catch {
    Say "失败：$($_.Exception.Message)"
    exit 1
}
