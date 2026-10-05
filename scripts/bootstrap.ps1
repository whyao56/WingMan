#Requires -Version 5.1
<#
    WingMan 一键启动器 · 真正逻辑
    由仓库根目录的 wingman.cmd 调用（wingman.cmd 才是面向用户的唯一入口）。

    直接运行方式（排障用）：
        powershell -NoProfile -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 -RawArgs "--doctor"

    职责：
      1. 前置自检：仓库完整性、Python >= 3.11、端口可用 —— 任何失败都在创建 venv 之前拒绝
      2. 准备依赖：backend\.venv + backend\requirements.lock.txt（幂等、可自愈）
      3. 启动服务并轮询 /api/health 真实就绪，然后打印访问地址、数据目录、停止方法
      4. --setup-only / --doctor 只安装或只体检，然后退出

    退出码契约（D3，与 wingman.cmd 一致）：
      0 = 成功
      2 = 前置自检未通过（零副作用：不创建、不修改任何文件）
      3 = 依赖准备失败（venv 创建 / pip 安装 / 依赖校验）
      1 = 其他错误（启动失败、健康检查超时、意外异常）

    编码契约（D6）：
      - 本文件必须以 UTF-8 with BOM 保存。Windows PowerShell 5.1 在没有 BOM 时会按
        ANSI(936) 解析源文件，脚本里的中文字面量会直接变成乱码。
      - 启动时切 chcp 65001，并在所有退出路径（正常/异常/提前退出/Ctrl+C）恢复原始代码页；
        wingman.cmd 在 PowerShell 进程退出后还会兜底恢复一次。
      - 终端无法切到 UTF-8（无控制台、代码页被策略锁定）或设了 WINGMAN_ASCII=1 时，
        输出自动降级为纯 ASCII（绝不输出乱码）。

    路径契约（D2）：
      虚拟环境只使用 backend\.venv\Scripts\python.exe 这一个路径，不存在第二候选。
#>

param(
    [string]$RawArgs = ''
)

# ============================================================ 0. 常量与全局状态

$script:ExitOk        = 0
$script:ExitOther     = 1
$script:ExitPreflight = 2
$script:ExitDeps      = 3

$script:DefaultPort    = 8787
$script:MinPythonMinor = 11
$script:MaxTestedMinor = 13
$script:HealthTimeout  = 90

$script:ScriptRoot  = Split-Path -Parent $PSCommandPath
$script:RepoRoot    = Split-Path -Parent $script:ScriptRoot
$script:BackendDir  = Join-Path $script:RepoRoot 'backend'
$script:FrontendDir = Join-Path $script:RepoRoot 'frontend'
$script:DataDir     = Join-Path $script:BackendDir 'data'
$script:VenvDir     = Join-Path $script:BackendDir '.venv'               # D2
$script:VenvPython  = Join-Path $script:VenvDir 'Scripts\python.exe'     # D2
$script:LockFile    = Join-Path $script:BackendDir 'requirements.lock.txt'
$script:StampFile   = Join-Path $script:VenvDir '.wingman-deps.json'
$script:InstallLog  = Join-Path $script:VenvDir 'wingman-last-install.log'
$script:AppTarget   = 'app.main:app'

$script:AsciiMode       = $false
$script:OriginalCodePage = 0
$script:ConsoleRestored = $false
$script:PauseOnExit     = $false
$script:PauseChecked    = $false
$script:Sym             = @{}
$script:Opt             = @{}
$script:Python          = $null
$script:Port            = $script:DefaultPort
$script:PortFromEnv     = $false

# ============================================================ 1. 控制台编码与输出

function Get-ActiveCodePage {
    # chcp.com 的输出是本地化的（中文系统： "活动代码页: 936"），只取其中的数字。
    $text = ''
    try { $text = (& chcp.com 2>$null | Out-String) } catch { $text = '' }
    if ($text -match '(\d{2,5})') { return [int]$Matches[1] }
    return 0
}

function ConvertTo-AsciiText {
    param([string]$Text)
    if (-not $Text) { return '' }
    $sb = New-Object System.Text.StringBuilder
    foreach ($ch in $Text.ToCharArray()) {
        if ([int][char]$ch -lt 128) { [void]$sb.Append($ch) }
    }
    return (($sb.ToString() -replace '\s{2,}', ' ').Trim())
}

function Initialize-Console {
    $script:OriginalCodePage = Get-ActiveCodePage

    if ($env:WINGMAN_ASCII -and $env:WINGMAN_ASCII -notin @('0', 'false', 'no', 'off')) {
        $script:AsciiMode = $true
    }
    else {
        $utf8Ok = $false
        try {
            $null = & chcp.com 65001 2>&1
            [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
            $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
            $utf8Ok = ([Console]::OutputEncoding.CodePage -eq 65001)
        }
        catch {
            $utf8Ok = $false
        }
        if (-not $utf8Ok) {
            $script:AsciiMode = $true
            if ($script:OriginalCodePage -gt 0) { $null = & chcp.com $script:OriginalCodePage 2>&1 }
        }
    }

    $ProgressPreference = 'SilentlyContinue'

    # 子进程（pip / 服务）的输出编码跟随当前模式：ASCII 降级时让 Python 主动把
    # 非 ASCII 字符替换成 ?，而不是吐出一堆乱码。
    if ($script:AsciiMode) {
        $env:PYTHONIOENCODING = 'ascii:replace'
        $env:PYTHONUTF8 = '0'
    }
    else {
        $env:PYTHONIOENCODING = 'utf-8'
        $env:PYTHONUTF8 = '1'
    }
    $env:PYTHONUNBUFFERED = '1'
    if ($script:AsciiMode) {
        $script:Sym = @{
            Ok = '[ OK ]'; Bad = '[FAIL]'; Warn = '[WARN]'; Info = '[ .. ]'
            Arrow = '->'; Bullet = '-'; Line = ('=' * 64); Thin = ('-' * 64)
        }
    }
    else {
        $script:Sym = @{
            Ok = '✓'; Bad = '✗'; Warn = '!'; Info = '·'
            Arrow = '→'; Bullet = '·'; Line = ('═' * 64); Thin = ('─' * 64)
        }
    }
}

function Restore-Console {
    if ($script:ConsoleRestored) { return }
    $script:ConsoleRestored = $true
    if ($script:OriginalCodePage -le 0) { return }
    if ($script:OriginalCodePage -eq 65001) { return }
    try { $null = & chcp.com $script:OriginalCodePage 2>&1 } catch { }
}

function Emit {
    param([string]$Text = '', [string]$Color = '')
    if ([Console]::IsOutputRedirected) {
        [Console]::Out.WriteLine($Text)
    }
    elseif ($Color) {
        Write-Host $Text -ForegroundColor $Color
    }
    else {
        Write-Host $Text
    }
}

function Resolve-Text {
    param([string]$Zh, [string]$En)
    if (-not $script:AsciiMode) { return $Zh }
    if ($En) { return $En }
    return (ConvertTo-AsciiText $Zh)
}

function Say {
    param([string]$Zh = '', [string]$En = '', [string]$Color = '')
    # 防线：如果调用方把颜色名误当成英文文案传进来（Say "..." 'Green'），
    # 这里纠正成颜色参数，避免 ASCII 降级模式下把 "Green" 当正文打印出去。
    if (-not $Color -and $En -in @('Green', 'Red', 'Yellow', 'Cyan', 'White', 'DarkGray', 'Gray')) {
        $Color = $En
        $En = ''
    }
    Emit (Resolve-Text $Zh $En) $Color
}
function SayOk   { param([string]$Zh = '', [string]$En = ''); Say $Zh $En 'Green' }
function SayWarn { param([string]$Zh = '', [string]$En = ''); Say $Zh $En 'Yellow' }
function SayErr  { param([string]$Zh = '', [string]$En = ''); Say $Zh $En 'Red' }
function SayInfo { param([string]$Zh = '', [string]$En = ''); Say $Zh $En 'Cyan' }
function SayDim  { param([string]$Zh = '', [string]$En = ''); Say $Zh $En 'DarkGray' }

function Get-DisplayWidth {
    # 中文/全角字符占 2 列，用字符数补齐会错位，这里按显示宽度算。
    param([string]$Text)
    if (-not $Text) { return 0 }
    $width = 0
    foreach ($ch in $Text.ToCharArray()) {
        $c = [int][char]$ch
        if (($c -ge 0x1100 -and $c -le 0x115F) -or
            ($c -ge 0x2E80 -and $c -le 0xA4CF) -or
            ($c -ge 0xAC00 -and $c -le 0xD7A3) -or
            ($c -ge 0xF900 -and $c -le 0xFAFF) -or
            ($c -ge 0xFE30 -and $c -le 0xFE6F) -or
            ($c -ge 0xFF00 -and $c -le 0xFF60) -or
            ($c -ge 0xFFE0 -and $c -le 0xFFE6)) { $width += 2 }
        else { $width += 1 }
    }
    return $width
}

function SayField {
    param([string]$Label, [string]$Value, [string]$LabelEn = '', [string]$Color = '')
    $label = $Label
    if ($script:AsciiMode) {
        if ($LabelEn) { $label = $LabelEn } else { $label = ConvertTo-AsciiText $Label }
        # 值里也可能带中文（例如服务返回的 provider note），ASCII 模式下同样降级
        $Value = ConvertTo-AsciiText $Value
    }
    $pad = 16 - (Get-DisplayWidth $label)
    if ($pad -lt 1) { $pad = 1 }
    Emit ('      ' + $label + (' ' * $pad) + ': ' + $Value) $Color
}
function SayFieldOk   { param([string]$Label, [string]$Value, [string]$LabelEn = ''); SayField $Label $Value $LabelEn 'Green' }
function SayFieldWarn { param([string]$Label, [string]$Value, [string]$LabelEn = ''); SayField $Label $Value $LabelEn 'Yellow' }
function SayFieldBad  { param([string]$Label, [string]$Value, [string]$LabelEn = ''); SayField $Label $Value $LabelEn 'Red' }

function Test-DoubleClickLaunch {
    # 双击运行 wingman.cmd 时：explorer.exe -> cmd.exe /c ""...wingman.cmd"" -> powershell.exe
    # 只有这种形状才在退出前等待回车，自动化/命令行调用（cmd /c、终端里手敲）不会卡住。
    if ($script:PauseChecked) { return $script:PauseOnExit }
    $script:PauseChecked = $true
    $script:PauseOnExit = $false
    try {
        $self = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $PID) -ErrorAction Stop
        $parent = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $self.ParentProcessId) -ErrorAction Stop
        if ($parent.Name -ne 'cmd.exe') { return $false }
        $parentCmdLine = [string]$parent.CommandLine
        if ($parentCmdLine -notmatch '\s/c\s') { return $false }
        $grand = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $parent.ParentProcessId) -ErrorAction Stop
        $script:PauseOnExit = ($grand.Name -eq 'explorer.exe')
    }
    catch {
        $script:PauseOnExit = $false
    }
    return $script:PauseOnExit
}

function Exit-Now {
    param([int]$Code = 0, [bool]$PauseIfDoubleClick = $false)
    if ($PauseIfDoubleClick -and (Test-DoubleClickLaunch)) {
        Say ''
        Say (Resolve-Text '按任意键关闭这个窗口…' 'Press any key to close this window...')
        # 用 cmd 的 pause 而不是 Read-Host：启动器以 -NonInteractive 运行，
        # 而且 cmd 的 pause 在任何 stdin 状态下都不会把窗口挂死。
        try { $null = & "$env:SystemRoot\System32\cmd.exe" '/c' 'pause' 2>&1 } catch { }
    }
    Restore-Console
    exit $Code
}

function Fail-Preflight {
    param([string]$Zh, [string]$En = '', [string]$FixZh = '', [string]$FixEn = '')
    SayErr ("{0} {1}" -f $script:Sym.Bad, (Resolve-Text $Zh $En))
    if ($FixZh) { Say ("      {0} {1}" -f $script:Sym.Arrow, (Resolve-Text $FixZh $FixEn)) }
    Say ''
    SayDim ("{0} 2 = {1}" -f (Resolve-Text '退出码' 'exit code'), (Resolve-Text '前置自检未通过，没有创建或修改任何文件' 'preflight refused, nothing was created or modified'))
    Exit-Now $script:ExitPreflight $true
}

# ============================================================ 2. 帮助与参数

function Show-Help {
    Say $script:Sym.Line '' 'White'
    Say (Resolve-Text ' WingMan 一键启动器 — 用法' ' WingMan launcher - usage') '' 'White'
    Say $script:Sym.Line '' 'White'
    Say ''
    Say (Resolve-Text '用法：wingman.cmd [选项]' 'Usage: wingman.cmd [options]')
    Say ''
    Say (Resolve-Text '在本机跑起 WingMan：自动创建虚拟环境、按锁文件安装依赖、启动服务、' 'Get WingMan running locally: create the venv, install locked deps, start the')
    Say (Resolve-Text '等待 /api/health 通过后打印地址并打开浏览器。' 'service, wait for /api/health, print the URL and open the browser.')
    Say ''
    Say (Resolve-Text '选项：' 'Options:')
    Say (Resolve-Text '  （无参数）       安装（如需要）并启动，默认端口 8787，自动打开浏览器' '  (no option)      install if needed, start, default port 8787, open browser')
    Say (Resolve-Text '  --port N         指定端口（1024-65535），例如 --port 8788' '  --port N         use port N (1024-65535)')
    Say (Resolve-Text '  --setup-only     只做安装与自检，不启动服务，退出码 0' '  --setup-only     install and verify only, do not start')
    Say (Resolve-Text '  --doctor         只做体检：不改配置、不建库、不启动服务（仅生成 __pycache__ 缓存）' '  --doctor         health check only (no config/db/service; leaves __pycache__)')
    Say (Resolve-Text '  --no-browser     启动后不自动打开浏览器' '  --no-browser     do not open the browser')
    Say (Resolve-Text '  --help           显示这份帮助' '  --help           show this help')
    Say ''
    Say (Resolve-Text '退出码：' 'Exit codes:')
    Say (Resolve-Text '  0  成功' '  0  success')
    Say (Resolve-Text '  2  前置自检未通过（Python 版本过低、端口被占用、参数错误…）；不做任何改动' '  2  preflight refused (bad Python, port busy, bad args); nothing is modified')
    Say (Resolve-Text '  3  依赖准备失败（创建虚拟环境或 pip 安装失败）' '  3  dependency setup failed (venv creation or pip install)')
    Say (Resolve-Text '  1  其他错误（启动失败、健康检查超时、意外异常）' '  1  other error (startup failed, health timeout, unexpected)')
    Say ''
    Say (Resolve-Text '固定路径：' 'Fixed paths:')
    Say (Resolve-Text '  虚拟环境  backend\.venv\Scripts\python.exe' '  venv      backend\.venv\Scripts\python.exe')
    Say (Resolve-Text '  依赖锁    backend\requirements.lock.txt' '  lock      backend\requirements.lock.txt')
    Say (Resolve-Text '  数据文件  backend\data\wingman.db' '  data      backend\data\wingman.db')
    Say ''
    Say (Resolve-Text '环境变量：' 'Environment variables:')
    Say (Resolve-Text '  PORT                  默认端口（--port 优先）' '  PORT                default port (--port wins)')
    Say (Resolve-Text '  WINGMAN_ASCII=1       强制纯 ASCII 输出' '  WINGMAN_ASCII=1     force ASCII-only output')
    Say (Resolve-Text '  WINGMAN_POWERSHELL    指定 powershell 可执行文件（默认系统自带 5.1）' '  WINGMAN_POWERSHELL  override the powershell executable')
    Say (Resolve-Text '  HTTP_PROXY/HTTPS_PROXY/PIP_INDEX_URL   影响依赖下载' '  HTTP_PROXY/HTTPS_PROXY/PIP_INDEX_URL   affect pip downloads')
    Say ''
    Say (Resolve-Text '示例：' 'Examples:')
    Say (Resolve-Text '  wingman.cmd                  直接启动（首次会自动安装）' '  wingman.cmd                  start (installs on first run)')
    Say (Resolve-Text '  wingman.cmd --setup-only     只安装依赖' '  wingman.cmd --setup-only     install deps only')
    Say (Resolve-Text '  wingman.cmd --port 8788      换端口启动' '  wingman.cmd --port 8788      start on another port')
    Say (Resolve-Text '  wingman.cmd --doctor         体检（Python/依赖/端口/数据目录）' '  wingman.cmd --doctor         diagnose the environment')
    Say ''
    Say (Resolve-Text '停止服务：在运行窗口按 Ctrl+C（若提示 Terminate batch job，按 Y 回车），或直接关闭窗口。' 'Stop: press Ctrl+C in the window (answer Y to "Terminate batch job"), or close the window.')
    Say $script:Sym.Line '' 'White'
}

function Read-WingmanArgs {
    param([string]$Raw)
    $text = [string]$Raw
    if (-not $text) { $text = [string]$env:WINGMAN_ARGS }
    $text = $text.Trim()

    $tokens = @()
    if ($text) {
        foreach ($t in ($text -split '\s+')) {
            $tt = $t.Trim().Trim('"').Trim("'")
            if ($tt) { $tokens += $tt }
        }
    }

    $o = @{
        Help = $false; SetupOnly = $false; Doctor = $false; NoBrowser = $false
        PortGiven = $false; PortRaw = ''; Unknown = @(); MissingPortValue = $false
    }

    $i = 0
    while ($i -lt $tokens.Count) {
        $tok = $tokens[$i]
        if ($tok -in @('--help', '-h', '-?', '/?')) { $o.Help = $true; $i++ }
        elseif ($tok -eq '--setup-only') { $o.SetupOnly = $true; $i++ }
        elseif ($tok -eq '--doctor') { $o.Doctor = $true; $i++ }
        elseif ($tok -eq '--no-browser') { $o.NoBrowser = $true; $i++ }
        elseif ($tok -eq '--port') {
            if ($i + 1 -ge $tokens.Count) { $o.MissingPortValue = $true; $i++ }
            else { $o.PortRaw = $tokens[$i + 1]; $o.PortGiven = $true; $i += 2 }
        }
        elseif ($tok -like '--port=*') { $o.PortRaw = $tok.Substring(7); $o.PortGiven = $true; $i++ }
        else { $o.Unknown += $tok; $i++ }
    }
    return $o
}

function Resolve-ArgumentErrors {
    # 返回 $true 表示参数合法；否则打印中文提示并以退出码 2 结束（零副作用）。
    if ($script:Opt.Unknown.Count -gt 0) {
        $bad = ($script:Opt.Unknown -join ' ')
        SayErr ("{0} {1}" -f $script:Sym.Bad, (Resolve-Text ("无法识别的参数：$bad") ("Unknown argument(s): $bad")))
        Say (Resolve-Text '      支持的参数：--port N  --setup-only  --doctor  --no-browser  --help' '      Supported: --port N  --setup-only  --doctor  --no-browser  --help')
        SayDim (Resolve-Text '      查看完整说明：wingman.cmd --help' '      Full help: wingman.cmd --help')
        Say ''
        Exit-Now $script:ExitPreflight $true
    }
    if ($script:Opt.MissingPortValue) {
        SayErr ("{0} {1}" -f $script:Sym.Bad, (Resolve-Text '--port 后面缺少端口号，例如：wingman.cmd --port 8788' '--port requires a value, e.g. wingman.cmd --port 8788'))
        Say ''
        Exit-Now $script:ExitPreflight $true
    }
    if ($script:Opt.SetupOnly -and $script:Opt.Doctor) {
        SayErr ("{0} {1}" -f $script:Sym.Bad, (Resolve-Text '--setup-only 与 --doctor 不能同时使用（都只做检查后退出），请二选一' '--setup-only and --doctor cannot be combined'))
        Say ''
        Exit-Now $script:ExitPreflight $true
    }

    $port = $script:DefaultPort
    if ($script:Opt.PortGiven) {
        $parsed = 0
        if (-not [int]::TryParse($script:Opt.PortRaw, [ref]$parsed)) {
            SayErr ("{0} {1}" -f $script:Sym.Bad, (Resolve-Text ("端口必须是数字，收到：" + $script:Opt.PortRaw) ("Port must be a number, got: " + $script:Opt.PortRaw)))
            Say ''
            Exit-Now $script:ExitPreflight $true
        }
        $port = $parsed
    }
    elseif ($env:PORT) {
        $parsed = 0
        if ([int]::TryParse($env:PORT, [ref]$parsed)) { $port = $parsed; $script:PortFromEnv = $true }
        else { SayWarn ("{0} {1}" -f $script:Sym.Warn, (Resolve-Text ("环境变量 PORT 不是数字（$($env:PORT)），已忽略") ("Environment variable PORT is not a number ($($env:PORT)), ignored"))) }
    }

    if ($port -lt 1024 -or $port -gt 65535) {
        SayErr ("{0} {1}" -f $script:Sym.Bad, (Resolve-Text ("端口 $port 超出可用范围 1024-65535") ("Port $port is outside 1024-65535")))
        SayDim (Resolve-Text '      1024 以下的端口需要管理员权限，本启动器不请求管理员权限' '      Ports below 1024 need administrator rights; this launcher never asks for them')
        Say ''
        Exit-Now $script:ExitPreflight $true
    }
    $script:Port = $port
    return $true
}

# ============================================================ 3. 前置自检

function Assert-RepoLayout {
    $missing = @()
    foreach ($p in @(
            @{ Path = (Join-Path $script:BackendDir 'app\main.py'); What = 'backend\app\main.py' },
            @{ Path = (Join-Path $script:FrontendDir 'index.html'); What = 'frontend\index.html' },
            @{ Path = $script:LockFile; What = 'backend\requirements.lock.txt' })) {
        if (-not (Test-Path -LiteralPath $p.Path -PathType Leaf)) { $missing += $p.What }
    }
    if ($missing.Count -gt 0) {
        SayErr ("{0} {1}" -f $script:Sym.Bad, (Resolve-Text "仓库不完整，缺少文件：$($missing -join '、')" ("Repository is incomplete, missing: " + ($missing -join ', '))))
        SayDim (Resolve-Text ("      当前仓库根目录：$($script:RepoRoot)") ("      Repo root: $($script:RepoRoot)"))
        Say (Resolve-Text '      怎么修：重新完整克隆或解压 WingMan，不要在仓库里单独拷贝文件' '      Fix: re-clone or re-extract the whole WingMan repository')
        Say ''
        SayDim ("{0} 2 = {1}" -f (Resolve-Text '退出码' 'exit code'), (Resolve-Text '前置自检未通过，没有创建或修改任何文件' 'preflight refused, nothing was created or modified'))
        Exit-Now $script:ExitPreflight $true
    }
}

function Get-PythonProbe {
    param([string]$Exe, [string[]]$Prefix = @())
    $result = @{
        Label = $Exe; Exe = $Exe; Prefix = $Prefix; Found = $false; Stub = $false; Path = ''
        Version = ''; Major = 0; Minor = 0; Patch = 0; Raw = ''; Ok = $false; Note = ''
    }
    $cmd = Get-Command $Exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $cmd) {
        $result.Note = '未找到'
        return $result
    }
    $result.Found = $true
    $result.Path = [string]$cmd.Source

    if ($result.Path -match '\\WindowsApps\\python3?\.exe$') {
        $result.Stub = $true
        $result.Note = 'Microsoft Store 应用执行别名（不是真正的 Python）'
        return $result
    }

    $raw = ''
    try {
        $raw = (& $result.Path @Prefix '--version' 2>&1 | ForEach-Object { [string]$_ }) -join ' '
    }
    catch {
        $raw = ''
    }
    $result.Raw = $raw.Trim()
    if ($result.Raw -match 'Python\s+(\d+)\.(\d+)\.(\d+)') {
        $result.Major = [int]$Matches[1]
        $result.Minor = [int]$Matches[2]
        $result.Patch = [int]$Matches[3]
        $result.Version = ('{0}.{1}.{2}' -f $result.Major, $result.Minor, $result.Patch)
        if ($result.Major -eq 3 -and $result.Minor -ge $script:MinPythonMinor) {
            $result.Ok = $true
            $result.Note = '可用'
        }
        elseif ($result.Major -eq 3) {
            $result.Note = ('版本过低，需要 >= 3.{0}' -f $script:MinPythonMinor)
        }
        else {
            $result.Note = '不是 Python 3'
        }
    }
    else {
        $result.Note = '无法获取版本号'
    }
    return $result
}

function Resolve-PythonInterpreter {
    $candidates = @(
        @{ Label = 'py -3'; Exe = 'py'; Prefix = @('-3') },
        @{ Label = 'py'; Exe = 'py'; Prefix = @() },
        @{ Label = 'python'; Exe = 'python'; Prefix = @() },
        @{ Label = 'python3'; Exe = 'python3'; Prefix = @() }
    )
    $tried = @()
    $chosen = $null
    $seen = @{}
    foreach ($c in $candidates) {
        $probe = Get-PythonProbe -Exe $c.Exe -Prefix $c.Prefix
        $probe.Label = $c.Label
        if ($probe.Found -and -not $probe.Stub -and $probe.Path) {
            $key = $probe.Path.ToLowerInvariant()
            if ($seen.ContainsKey($key)) { $probe.Note = ('与 ' + $seen[$key] + ' 是同一个解释器') }
            else { $seen[$key] = $probe.Label }
        }
        $tried += $probe
        if ($probe.Ok -and -not $chosen) { $chosen = $probe; break }
    }

    Say (Resolve-Text '      探测结果：' '      Probes:')
    foreach ($p in $tried) {
        $label = ('        {0,-9} ' -f $p.Label)
        if ($p.Ok) { Say (Resolve-Text ($label + $script:Sym.Arrow + ' Python ' + $p.Version) ($label + '-> Python ' + $p.Version)) '' 'Green' }
        elseif ($p.Found -and -not $p.Stub) { SayWarn (Resolve-Text ($label + $script:Sym.Arrow + ' ' + $p.Raw + '（' + $p.Note + '）') ($label + '-> ' + $p.Raw + ' (' + (ConvertTo-AsciiText $p.Note) + ')')) }
        else { SayDim ($label + $script:Sym.Arrow + ' ' + (Resolve-Text $p.Note (ConvertTo-AsciiText $p.Note))) }
    }

    if (-not $chosen) {
        Say ''
        SayErr ("{0} {1}" -f $script:Sym.Bad, (Resolve-Text '没有找到可用的 Python 3.11+' 'No usable Python 3.11+ found'))
        Say (Resolve-Text ("      检测到什么：上面 4 个候选里没有一个满足 Python >= 3.$($script:MinPythonMinor)") '      Detected: none of the 4 candidates is Python 3.11+')
        Say (Resolve-Text ("      需要什么：Python 3.$($script:MinPythonMinor) - 3.$($script:MaxTestedMinor)（64 位）") '      Need: Python 3.11 - 3.13 (64-bit)')
        Say (Resolve-Text '      怎么修：' '      Fix:')
        Say (Resolve-Text '        1) 到 https://www.python.org/downloads/windows/ 下载 64 位安装包' '        1) Download the 64-bit installer from python.org/downloads/windows')
        Say (Resolve-Text '        2) 安装时勾选 “Add python.exe to PATH”，保持 pip / venv 默认勾选' '        2) Tick "Add python.exe to PATH", keep pip/venv enabled')
        Say (Resolve-Text '        3) 装好后关掉这个窗口重新打开，再运行 wingman.cmd' '        3) Reopen this window, then run wingman.cmd again')
        Say (Resolve-Text '        4) 已装好但不在 PATH：把安装目录加入 PATH 后重试（本启动器不会改系统 PATH）' '        4) Already installed but not on PATH: add it to PATH and retry')
        Say ''
        SayDim ("{0} 2 = {1}" -f (Resolve-Text '退出码' 'exit code'), (Resolve-Text '前置自检未通过，没有创建或修改任何文件' 'preflight refused, nothing was created or modified'))
        Exit-Now $script:ExitPreflight $true
    }
    return @{ Chosen = $chosen; Tried = $tried }
}

function Test-PythonCapability {
    param($Probe = $script:Python)
    # venv/ensurepip 缺失属于“装不出环境”，在创建任何文件之前就要拒绝（退出码 2）。
    # 注意：这段探测代码里不能出现双引号 —— Windows PowerShell 5.1 把参数交给原生
    # 程序时会吃掉内嵌的双引号，-c 的代码会被改坏（实测 SyntaxError）。
    $pyPath = [string]$Probe.Path
    $pyPrefix = @($Probe.Prefix)
    $code = 'import venv, ensurepip, sys; print(sys.version_info[0], sys.version_info[1], sys.version_info[2], 64 if sys.maxsize > 2**32 else 32)'
    $raw = ''
    try { $raw = (& $pyPath @pyPrefix '-c' $code 2>&1 | ForEach-Object { [string]$_ }) -join ' ' } catch { $raw = '' }
    if ($LASTEXITCODE -ne 0 -or $raw -notmatch '(\d+)\s+(\d+)\s+(\d+)\s+(\d+)') {
        Say ''
        SayErr ("{0} {1}" -f $script:Sym.Bad, (Resolve-Text '这个 Python 不能创建虚拟环境（缺少 venv / ensurepip 模块）' 'This Python cannot create virtualenvs (venv / ensurepip missing)'))
        Say (Resolve-Text ("      检测到什么：$pyPath 执行 import venv, ensurepip 失败") ("      Detected: $pyPath cannot import venv/ensurepip"))
        if ($raw.Trim()) { SayDim ('      ' + $raw.Trim()) }
        Say (Resolve-Text '      需要什么：带 pip 与 venv 的完整 Python 安装' '      Need: a full Python install including pip and venv')
        Say (Resolve-Text '      怎么修：重新运行 Python 安装包，选择 Modify 并勾选 pip 与 venv（或换一个完整安装的 Python）' '      Fix: re-run the installer, choose Modify and enable pip + venv')
        Say ''
        SayDim ("{0} 2 = {1}" -f (Resolve-Text '退出码' 'exit code'), (Resolve-Text '前置自检未通过，没有创建或修改任何文件' 'preflight refused, nothing was created or modified'))
        Exit-Now $script:ExitPreflight $true
    }
    $bits = [int]$Matches[4]
    return $bits
}

function Get-PortOwner {
    param([int]$Port)
    $owner = @{ Pid = 0; Name = ''; Path = ''; CommandLine = '' }
    $ownerPid = 0
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalAddress -in @('0.0.0.0', '127.0.0.1') } |
            Select-Object -First 1
        if ($conn) { $ownerPid = [int]$conn.OwningProcess }
    }
    catch { $ownerPid = 0 }

    if (-not $ownerPid) {
        try {
            $hit = & netstat -ano 2>$null | Select-String -Pattern (":{0}\s+\S+\s+LISTENING\s+(\d+)" -f $Port) | Select-Object -First 1
            if ($hit) { $ownerPid = [int]$hit.Matches[0].Groups[1].Value }
        }
        catch { $ownerPid = 0 }
    }

    if ($ownerPid -gt 0) {
        $owner.Pid = $ownerPid
        try {
            $p = Get-Process -Id $ownerPid -ErrorAction Stop
            $owner.Name = $p.ProcessName
            try { $owner.Path = [string]$p.Path } catch { $owner.Path = '' }
        }
        catch { $owner.Name = '未知进程' }
        try {
            $ci = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $ownerPid) -ErrorAction Stop
            $owner.CommandLine = ([string]$ci.CommandLine).Trim()
        }
        catch { $owner.CommandLine = '' }
    }
    return $owner
}

function Test-PortAvailable {
    param([int]$Port)
    $result = @{ Free = $true; Reason = ''; Owner = @{ Pid = 0 } }

    $owner = Get-PortOwner -Port $Port
    if ($owner.Pid -gt 0) {
        $result.Free = $false
        $result.Reason = 'listen'
        $result.Owner = $owner
        return $result
    }

    # 再用真实 bind 复核一次（netstat 看不到的保留端口/半开状态）
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
        try { $listener.Start() } finally { $listener.Stop() }
    }
    catch {
        $result.Free = $false
        $result.Reason = 'bind'
        $result.BindError = $_.Exception.Message
    }
    return $result
}

function Assert-PortFree {
    param([int]$Port)
    $check = Test-PortAvailable -Port $Port
    if ($check.Free) { return }
    SayErr ("{0} {1}" -f $script:Sym.Bad, (Resolve-Text ("端口 $Port 已被占用，无法启动") ("Port $Port is already in use")))
    $o = $check.Owner
    if ($o.Pid -gt 0) {
        $pathText = ''
        if ($o.Path) { $pathText = '  ' + $o.Path }
        Say (Resolve-Text ("      占用者：PID $($o.Pid)  $($o.Name)$pathText") ("      Owner: PID $($o.Pid)  $($o.Name)$pathText"))
        if ($o.CommandLine) {
            $cl = $o.CommandLine
            if ($cl.Length -gt 160) { $cl = $cl.Substring(0, 160) + '...' }
            SayDim (Resolve-Text ("      命令行：$cl") ("      Command: $cl"))
        }
    }
    elseif ($check.Reason -eq 'bind') {
        SayDim (Resolve-Text ("      系统拒绝绑定：$($check.BindError)") ("      Bind failed: $($check.BindError)"))
    }
    Say (Resolve-Text '      解决：' '      Fix:')
    Say (Resolve-Text ("        1) 换一个端口：wingman.cmd --port $($Port + 1)") ("        1) Use another port: wingman.cmd --port $($Port + 1)"))
    if ($o.Pid -gt 0) {
        Say (Resolve-Text ("        2) 或结束占用进程：taskkill /PID $($o.Pid) /F") ("        2) Or kill it: taskkill /PID $($o.Pid) /F"))
        SayDim (Resolve-Text '           （如果那就是你自己刚才启动的 WingMan，关掉那个窗口即可）' '           (if it is your own WingMan instance, just close that window)')
    }
    SayDim (Resolve-Text '      常见占用者：另一个 WingMan、Docker、IIS、公司代理/安全软件' '      Usual suspects: another WingMan, Docker, IIS, corporate proxy/security tools')
    Say ''
    SayDim ("{0} 2 = {1}" -f (Resolve-Text '退出码' 'exit code'), (Resolve-Text '前置自检未通过，没有创建或修改任何文件' 'preflight refused, nothing was created or modified'))
    Exit-Now $script:ExitPreflight $true
}

function Show-EnvironmentHeader {
    Say $script:Sym.Line '' 'White'
    Say (Resolve-Text ' WingMan 一键启动器' ' WingMan launcher') '' 'White'
    Say $script:Sym.Line '' 'White'
    $os = ''
    try { $os = [System.Environment]::OSVersion.VersionString } catch { $os = 'unknown' }
    SayField (Resolve-Text '仓库目录' 'repo') $script:RepoRoot 'repo' 'DarkGray'
    SayField (Resolve-Text '操作系统' 'windows') $os 'windows' 'DarkGray'
    SayField (Resolve-Text '控制台编码' 'console') (Get-ConsoleDescription) 'console' 'DarkGray'
}

function Get-ConsoleDescription {
    if ($script:AsciiMode) {
        return (Resolve-Text ("纯 ASCII 降级模式（原代码页 $($script:OriginalCodePage)）") ("ASCII fallback mode (original code page $($script:OriginalCodePage))"))
    }
    if ($script:OriginalCodePage -gt 0) {
        return (Resolve-Text ("UTF-8 / chcp 65001（原 $($script:OriginalCodePage)，退出时恢复）") ("UTF-8 / chcp 65001 (was $($script:OriginalCodePage), restored on exit)"))
    }
    return 'UTF-8 / chcp 65001'
}

function Show-PreflightSummary {
    SayField (Resolve-Text 'Python' 'python') (Resolve-Text ("$($script:Python.Version) （$($script:Python.Label)，$($script:PythonBits) 位）") ("$($script:Python.Version) ($($script:Python.Label), $($script:PythonBits)-bit)")) 'python' 'DarkGray'
    SayField (Resolve-Text '解释器' 'exe') $script:Python.Path 'exe' 'DarkGray'
    SayField (Resolve-Text '虚拟环境' 'venv') (Resolve-Text 'backend\.venv（按要求固定此路径）' 'backend\.venv (fixed path)') 'venv' 'DarkGray'
}

# ============================================================ 4. 依赖准备（code 3 区）

function Get-LockPins {
    param([string]$Path)
    $pins = [ordered]@{}
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $pins }
    $lines = [System.IO.File]::ReadAllLines($Path, [System.Text.Encoding]::UTF8)
    foreach ($line in $lines) {
        $l = $line.Trim()
        if (-not $l -or $l.StartsWith('#')) { continue }
        if ($l -match '^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*==\s*([^\s#]+)') {
            $pins[$Matches[1]] = $Matches[3]
        }
    }
    return $pins
}

function Get-TextSha256 {
    param([string]$Text)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
        return (([BitConverter]::ToString($sha.ComputeHash($bytes))) -replace '-', '').ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}

function Get-FileSha256 {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return '' }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Invoke-PipInstall {
    param([string[]]$Requirements, [string]$LogPath = $script:InstallLog)
    $pipArgs = @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '--progress-bar', 'off', '--upgrade')
    foreach ($r in $Requirements) { $pipArgs += @('-r', $r) }

    $collected = New-Object System.Collections.Generic.List[string]
    $exitCode = 0
    try {
        & $script:VenvPython @pipArgs 2>&1 | ForEach-Object {
            $text = ''
            if ($_ -is [System.Management.Automation.ErrorRecord]) { $text = [string]$_.Exception.Message }
            else { $text = [string]$_ }
            foreach ($line in ($text -split "`r?`n")) {
                if ($line -and $line.Trim().Length -gt 0) {
                    $collected.Add($line)
                    Emit ('      ' + $line) 'DarkGray'
                }
            }
        }
        $exitCode = $LASTEXITCODE
    }
    catch {
        $collected.Add('launcher: ' + $_.Exception.Message)
        $exitCode = 1
    }

    if ($collected.Count -gt 0) {
        try {
            [System.IO.File]::WriteAllLines($LogPath, $collected, ([System.Text.UTF8Encoding]::new($false)))
        }
        catch { }
    }
    return @{ ExitCode = $exitCode; Lines = $collected }
}

function Show-PipFailure {
    param([hashtable]$Result, [string]$What, [string]$LogPath = $script:InstallLog)
    $all = ($Result.Lines -join "`n")
    Say ''
    SayErr ("{0} {1}" -f $script:Sym.Bad, (Resolve-Text ("pip 安装失败：$What（pip 退出码 $($Result.ExitCode)）") ("pip install failed: $What (pip exit code $($Result.ExitCode))")))
    Say (Resolve-Text '      ---- pip 原始输出末尾 ----' '      ---- tail of raw pip output ----')
    $tail = @($Result.Lines)
    if ($tail.Count -gt 30) { $tail = $tail[($tail.Count - 30)..($tail.Count - 1)] }
    foreach ($line in $tail) { Emit ('      | ' + $line) 'DarkGray' }

    $guesses = @()
    if ($all -match 'No matching distribution found|Could not find a version') {
        $guesses += @(Resolve-Text '当前 Python 版本/架构没有对应的安装包（32 位 Python 或过新的 Python 最常见）' 'No wheel for this Python version/arch (32-bit or too-new Python)')
        if ($all -match 'from versions: none') {
            $guesses += @(Resolve-Text 'pip 源里查不到这些版本：源地址不可达、被代理拦截，或源里确实没有该版本（检查 PIP_INDEX_URL / 网络 / 代理）' 'The index returned no versions at all: unreachable index, blocked proxy, or the version is missing there (check PIP_INDEX_URL)')
        }
    }
    if ($all -match 'ProxyError|Tunnel connection failed|407|proxy') {
        $guesses += @(Resolve-Text '代理不可用或被拒绝（检查 HTTP_PROXY / HTTPS_PROXY 环境变量）' 'Proxy refused (check HTTP_PROXY / HTTPS_PROXY)')
    }
    if ($all -match 'CERTIFICATE_VERIFY_FAILED|SSLError|SSL:') {
        $guesses += @(Resolve-Text 'HTTPS 证书校验失败（公司中间人代理、或需要更新证书）' 'TLS certificate verification failed (MITM proxy?)')
    }
    if ($all -match 'Temporary failure in name resolution|getaddrinfo|Failed to establish a new connection|Connection refused|Read timed out|NewConnectionError|ConnectionError') {
        $guesses += @(Resolve-Text '网络不通：pip 源不可达（断网、DNS、公司网络限制）' 'Network unreachable: pip index not reachable')
    }
    if ($all -match 'WinError 5|Access is denied|Permission denied|being used by another process') {
        $guesses += @(Resolve-Text '文件被占用或权限不足（另一个 WingMan 正在运行、杀毒软件拦截）' 'File locked or access denied (running instance, antivirus)')
    }
    if ($all -match 'No space left|磁盘空间') {
        $guesses += @(Resolve-Text '磁盘空间不足' 'Out of disk space')
    }
    if ($all -match 'Microsoft Visual C\+\+|error: subprocess-exited-with-error') {
        $guesses += @(Resolve-Text '需要编译但没有编译器（通常说明装错了 Python 版本/架构）' 'Build tools required (usually wrong Python version/arch)')
    }
    if ($guesses.Count -eq 0) {
        $guesses += @(Resolve-Text '未识别的失败，请把上面的原始输出发给维护者' 'Unrecognized failure; please share the raw output above')
    }

    Say (Resolve-Text '      ---- 可能的原因（按可能性排序）----' '      ---- likely causes ----')
    foreach ($g in $guesses) { Say ('        ' + $script:Sym.Bullet + ' ' + $g) '' 'Yellow' }
    Say (Resolve-Text '      ---- 可操作的修复 ----' '      ---- what to try ----')
    Say (Resolve-Text '        1) 确认网络能用：curl -I https://pypi.org/simple/' '        1) Check the network: curl -I https://pypi.org/simple/')
    Say (Resolve-Text '        2) 换国内镜像源后重试：' '        2) Retry with a mirror index:')
    SayDim '             set PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple'
    SayDim '             wingman.cmd --setup-only'
    Say (Resolve-Text '        3) 看完整日志（本次 pip 的全部输出）：' '        3) Full log of this pip run:')
    SayDim ('             ' + $LogPath)
    Say (Resolve-Text '        4) 手动重试同样的安装：' '        4) Retry the very same install by hand:')
    SayDim ('             ' + $script:VenvPython + ' -m pip install -r ' + $script:LockFile)
    Say ''
    SayDim ("{0} 3 = {1}" -f (Resolve-Text '退出码' 'exit code'), (Resolve-Text '依赖准备失败' 'dependency setup failed'))
    Exit-Now $script:ExitDeps $true
}

function Assert-VenvRemovable {
    # 删除前多重确认：解析后的绝对路径必须正好是 <repo>\backend\.venv
    $expected = [System.IO.Path]::GetFullPath($script:VenvDir).TrimEnd('\')
    $resolved = $expected
    if (Test-Path -LiteralPath $script:VenvDir) {
        $resolved = ([System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $script:VenvDir).Path)).TrimEnd('\')
    }
    $leaf = Split-Path -Leaf $resolved
    $inRepo = $resolved.StartsWith($script:RepoRoot, [System.StringComparison]::OrdinalIgnoreCase)
    if ($resolved -ne $expected -or $leaf -ne '.venv' -or -not $inRepo) {
        SayErr ("{0} {1}" -f $script:Sym.Bad, (Resolve-Text "拒绝删除非预期的目录：$resolved" "Refusing to delete unexpected path: $resolved"))
        Say (Resolve-Text ('      期望路径：' + $expected) ('      Expected: ' + $expected))
        Say (Resolve-Text '      怎么修：手动删除 backend\.venv 后重试' '      Fix: delete backend\.venv manually and retry')
        Exit-Now $script:ExitDeps $true
    }
    try {
        Remove-Item -LiteralPath $resolved -Recurse -Force -ErrorAction Stop
    }
    catch {
        SayErr ("{0} {1}" -f $script:Sym.Bad, (Resolve-Text '无法删除损坏的虚拟环境 backend\.venv' 'Cannot delete the broken venv backend\.venv'))
        SayDim (Resolve-Text ("      原因：$($_.Exception.Message)") ("      Reason: $($_.Exception.Message)"))
        Say (Resolve-Text '      怎么修：关掉正在运行的 WingMan / 结束占用它的 python 进程，再执行：' '      Fix: stop the running WingMan / python process, then run:')
        SayDim ('             wingman.cmd --setup-only')
        Say ''
        SayDim ("{0} 3 = {1}" -f (Resolve-Text '退出码' 'exit code'), (Resolve-Text '依赖准备失败' 'dependency setup failed'))
        Exit-Now $script:ExitDeps $true
    }
}

function Test-VenvPythonUsable {
    if (-not (Test-Path -LiteralPath $script:VenvPython -PathType Leaf)) { return $false }
    $raw = ''
    try { $raw = (& $script:VenvPython '-c' 'import sys; print(sys.version.split()[0])' 2>&1 | ForEach-Object { [string]$_ }) -join ' ' } catch { $raw = '' }
    if ($LASTEXITCODE -eq 0 -and $raw -match '(\d+\.\d+\.\d+)') {
        $script:VenvPythonVersion = $Matches[1]
        return $true
    }
    return $false
}

function Initialize-Venv {
    # 如果只需要体检（--doctor），这一步不会走到。
    $script:VenvCreatedThisRun = $false
    if (Test-Path -LiteralPath $script:VenvDir) {
        if (Test-VenvPythonUsable) { return }
        SayWarn ("{0} {1}" -f $script:Sym.Warn, (Resolve-Text '虚拟环境存在但不可用，自动重建 backend\.venv' 'Existing venv is broken, rebuilding backend\.venv'))
        Assert-VenvRemovable
        $script:VenvCreatedThisRun = $true
    }
    else {
        $script:VenvCreatedThisRun = $true
    }

    Say (Resolve-Text ("      创建虚拟环境：backend\.venv（用 $($script:Python.Label)）") ("      Creating venv: backend\.venv (with $($script:Python.Label))"))
    SayDim (Resolve-Text ("      命令：$($script:Python.Label) -m venv backend\.venv") ("      Command: $($script:Python.Label) -m venv backend\.venv"))

    # 以产物为准 + 失败重试一次：实测有机器上 py.exe 返回了 -1、但 venv 其实已经建好
    # （杀毒软件/文件锁一类的瞬时干扰），所以只看退出码会误报失败。
    $attempt = 0
    $created = $false
    $out = ''
    $code = 0
    while ($attempt -lt 2 -and -not $created) {
        $attempt++
        $out = ''
        try {
            $out = (& $script:Python.Path @($script:Python.Prefix) '-m' 'venv' $script:VenvDir 2>&1 | ForEach-Object { [string]$_ }) -join "`n"
            $code = $LASTEXITCODE
        }
        catch {
            $out = $_.Exception.Message
            $code = 1
        }
        if (Test-Path -LiteralPath $script:VenvPython -PathType Leaf) { $created = $true }
        elseif ($attempt -lt 2) {
            SayWarn ("{0} {1}" -f $script:Sym.Warn, (Resolve-Text ("第一次创建虚拟环境失败（退出码 $code），2 秒后重试一次") ("First venv attempt failed (exit code $code), retrying once in 2s")))
            Start-Sleep -Seconds 2
        }
    }

    if (-not $created) {
        SayErr ("{0} {1}" -f $script:Sym.Bad, (Resolve-Text ("创建虚拟环境失败（退出码 $code）") ("Failed to create the venv (exit code $code)")))
        foreach ($line in (($out -split "`r?`n") | Where-Object { $_.Trim() } | Select-Object -Last 15)) { Emit ('      | ' + $line) 'DarkGray' }
        Say (Resolve-Text '      怎么修：确认磁盘可写、路径没有权限问题；Python 安装不完整时重装 Python（勾选 pip/venv）' '      Fix: check write permissions and that Python includes pip/venv')
        Say ''
        SayDim ("{0} 3 = {1}" -f (Resolve-Text '退出码' 'exit code'), (Resolve-Text '依赖准备失败' 'dependency setup failed'))
        Exit-Now $script:ExitDeps $true
    }
    if ($code -ne 0) {
        SayWarn ("{0} {1}" -f $script:Sym.Warn, (Resolve-Text ("创建命令返回了退出码 $code，但虚拟环境已经建好，继续") ("The create command returned exit code $code but the venv is in place, continuing")))
    }

    # venv 自带 pip 检查（少数发行版缺 ensurepip）
    $pipOk = $false
    try {
        $null = (& $script:VenvPython '-m' 'pip' '--version' 2>&1)
        $pipOk = ($LASTEXITCODE -eq 0)
    }
    catch { $pipOk = $false }
    if (-not $pipOk) {
        SayWarn ("{0} {1}" -f $script:Sym.Warn, (Resolve-Text '虚拟环境里没有 pip，尝试用 ensurepip 修复' 'pip missing in the venv, trying ensurepip'))
        try { $null = (& $script:VenvPython '-m' 'ensurepip' '--upgrade' 2>&1) } catch { }
        try {
            $null = (& $script:VenvPython '-m' 'pip' '--version' 2>&1)
            $pipOk = ($LASTEXITCODE -eq 0)
        }
        catch { $pipOk = $false }
    }
    if (-not $pipOk) {
        SayErr ("{0} {1}" -f $script:Sym.Bad, (Resolve-Text '虚拟环境里没有可用的 pip' 'No usable pip inside the venv'))
        Say (Resolve-Text '      怎么修：删除 backend\.venv 后重试；仍失败请重装 Python（勾选 pip）' '      Fix: delete backend\.venv and retry; if it persists reinstall Python with pip')
        Say ''
        SayDim ("{0} 3 = {1}" -f (Resolve-Text '退出码' 'exit code'), (Resolve-Text '依赖准备失败' 'dependency setup failed'))
        Exit-Now $script:ExitDeps $true
    }

    $null = Test-VenvPythonUsable
}

function Test-Dependencies {
    # 快速校验：venv 里的包是否与锁文件完全一致，并且 app.main 能导入。
    # 返回 @{ Ok = $bool; Problems = @(); Python = '3.x.y' }
    $pins = Get-LockPins -Path $script:LockFile
    $pinsJson = ($pins | ConvertTo-Json -Compress)
    $template = @'
import json, sys
import importlib.metadata as md
pins = json.loads(r"""__PINS_JSON__""")
problems = []
for name, want in pins.items():
    try:
        got = md.version(name)
    except Exception:
        problems.append({"pkg": name, "want": want, "got": "", "how": "missing"})
        continue
    if got != want:
        problems.append({"pkg": name, "want": want, "got": got, "how": "version"})
for mod in ("fastapi", "uvicorn", "pydantic", "pydantic_settings", "httpx", "numpy", "starlette", "cryptography"):
    try:
        __import__(mod)
    except Exception as exc:
        problems.append({"pkg": mod, "want": "", "got": "", "how": "import:" + type(exc).__name__})
try:
    import app.main  # noqa: F401
except Exception as exc:
    problems.append({"pkg": "app.main", "want": "", "got": "", "how": "import:" + type(exc).__name__ + ":" + str(exc)[:200]})
print(json.dumps({"ok": not problems, "problems": problems, "python": sys.version.split()[0]}))
sys.exit(0 if not problems else 1)
'@
    $code = $template.Replace('__PINS_JSON__', $pinsJson)

    $raw = ''
    Push-Location $script:BackendDir
    try {
        $raw = ($code | & $script:VenvPython '-') -join "`n"
    }
    catch {
        $raw = ''
    }
    finally {
        Pop-Location
    }

    $result = @{ Ok = $false; Problems = @(); Raw = $raw }
    $jsonText = $raw.Trim()
    if ($jsonText) {
        try {
            $parsed = $jsonText | ConvertFrom-Json
            $result.Ok = [bool]$parsed.ok
            $result.Problems = @($parsed.problems)
        }
        catch { $result.Ok = $false }
    }
    return $result
}

function Show-DependencyProblems {
    param([hashtable]$Check)
    SayWarn ("{0} {1}" -f $script:Sym.Warn, (Resolve-Text '检测到虚拟环境不完整，正在自动修复' 'Venv is incomplete, repairing automatically'))
    foreach ($p in $Check.Problems) {
        if ($p.how -eq 'missing') {
            SayDim (Resolve-Text ("        · 缺少 $($p.pkg)（需要 $($p.want)）") ("        - missing $($p.pkg) (want $($p.want))"))
        }
        elseif ($p.how -eq 'version') {
            SayDim (Resolve-Text ("        · $($p.pkg) 版本不符：需要 $($p.want)，实际 $($p.got)") ("        - $($p.pkg) version mismatch: want $($p.want), got $($p.got)"))
        }
        else {
            SayDim (Resolve-Text ("        · 导入失败 $($p.pkg)：$($p.how)") ("        - import failed $($p.pkg): $($p.how)"))
        }
    }
}

function Install-Dependencies {
    $lockHash = Get-FileSha256 -Path $script:LockFile
    $fingerprint = Get-TextSha256 ("$lockHash|$($script:Python.Version)|$($script:VenvPython)")

    $stamp = $null
    if (Test-Path -LiteralPath $script:StampFile -PathType Leaf) {
        try { $stamp = [System.IO.File]::ReadAllText($script:StampFile, [System.Text.Encoding]::UTF8) | ConvertFrom-Json }
        catch { $stamp = $null }
    }

    # 依赖指纹一致也仍然要做一次“能不能导入”的快速校验，避免带病启动。
    $check = Test-Dependencies
    $stampMatches = ($stamp -and $stamp.fingerprint -eq $fingerprint)

    $needMainInstall = $true
    if ($check.Ok -and $stampMatches) {
        $needMainInstall = $false
        SayOk ("{0} {1}" -f $script:Sym.Ok, (Resolve-Text '依赖已就绪（与锁文件一致），跳过安装' 'Dependencies already satisfied (lock matches), skipping install'))
    }
    elseif ($check.Ok -and -not $stampMatches) {
        $needMainInstall = $false
        SayOk ("{0} {1}" -f $script:Sym.Ok, (Resolve-Text '依赖已就绪（快速校验通过），跳过安装' 'Dependencies already satisfied (quick check passed), skipping install'))
    }
    else {
        if ($script:VenvCreatedThisRun -or $check.Problems.Count -eq 0) { Say (Resolve-Text '      首次运行需要下载依赖，约 1-3 分钟' '      First run downloads dependencies, about 1-3 minutes') }
        else { Show-DependencyProblems -Check $check }
        if (-not $script:VenvCreatedThisRun -and $check.Problems.Count -eq 0 -and (Test-Path -LiteralPath $script:VenvPython -PathType Leaf)) { SayWarn ("{0} {1}" -f $script:Sym.Warn, (Resolve-Text '依赖校验没能完成（无法运行 venv 里的 Python），按需要重装处理' 'Dependency check could not run; reinstalling')) }
        Say (Resolve-Text ("      安装：backend\requirements.lock.txt（$((Get-LockPins -Path $script:LockFile).Count) 个精确版本）") '      Installing: backend\requirements.lock.txt (exact versions)')
        SayDim (Resolve-Text '      ---- 以下为 pip 原始输出 ----' '      ---- raw pip output below ----')
        $pip = Invoke-PipInstall -Requirements @($script:LockFile)
        if ($pip.ExitCode -ne 0) {
            # 以产物为准 + 自动重试一次：实测在全新 venv 里首轮 pip 偶尔会被外部因素
            # 掐断（进程被杀，退出码 -1），但包已装了一半；pip 可重入，再跑一次即可补完。
            # 只有重试后依赖校验仍不通过，才判定失败（退出码 3）。
            $retryCheck = Test-Dependencies
            if ($retryCheck.Ok) {
                SayWarn ("{0} {1}" -f $script:Sym.Warn, (Resolve-Text ("pip 退出码 $($pip.ExitCode)，但依赖校验通过，继续") ("pip exit code $($pip.ExitCode) but dependency verification passed, continuing")))
            }
            else {
                SayWarn ("{0} {1}" -f $script:Sym.Warn, (Resolve-Text ("pip 退出码 $($pip.ExitCode)，依赖尚未装齐；2 秒后自动重试一次") ("pip exit code $($pip.ExitCode), dependencies incomplete; retrying once in 2s")))
                Start-Sleep -Seconds 2
                $pip = Invoke-PipInstall -Requirements @($script:LockFile)
                if ($pip.ExitCode -ne 0) {
                    $retryCheck2 = Test-Dependencies
                    if (-not $retryCheck2.Ok) { Show-PipFailure -Result $pip -What 'requirements.lock.txt' }
                    else { SayWarn ("{0} {1}" -f $script:Sym.Warn, (Resolve-Text ("pip 退出码 $($pip.ExitCode)，但依赖校验通过，继续") ("pip exit code $($pip.ExitCode) but dependency verification passed, continuing"))) }
                }
                else {
                    SayOk ("{0} {1}" -f $script:Sym.Ok, (Resolve-Text '重试成功，依赖已装齐' 'Retry succeeded, dependencies installed'))
                }
            }
        }

        $check2 = Test-Dependencies
        if (-not $check2.Ok) {
            SayErr ("{0} {1}" -f $script:Sym.Bad, (Resolve-Text '依赖安装命令成功，但校验仍然不通过' 'pip reported success but verification still fails'))
            Show-DependencyProblems -Check $check2
            Say (Resolve-Text '      怎么修：删除 backend\.venv 后重新运行 wingman.cmd' '      Fix: delete backend\.venv and run wingman.cmd again')
            Say ''
            SayDim ("{0} 3 = {1}" -f (Resolve-Text '退出码' 'exit code'), (Resolve-Text '依赖准备失败' 'dependency setup failed'))
            Exit-Now $script:ExitDeps $true
        }
    }

    $pinsJsonNow = (Get-LockPins -Path $script:LockFile)
    $stampObject = [ordered]@{
        schema          = 1
        fingerprint     = $fingerprint
        lock_sha256     = $lockHash
        python          = $script:Python.Version
        python_label    = $script:Python.Label
        venv_python     = $script:VenvPython
        installed_at    = (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')
        packages        = $pinsJsonNow
    }
    try {
        $json = $stampObject | ConvertTo-Json -Depth 6
        [System.IO.File]::WriteAllText($script:StampFile, $json, ([System.Text.UTF8Encoding]::new($false)))
    }
    catch {
        SayWarn ("{0} {1}" -f $script:Sym.Warn, (Resolve-Text '无法写入依赖指纹文件（不影响启动）' 'Could not write the dependency stamp (non-fatal)'))
    }

    return @{}
}

function Assert-DataDir {
    if (-not (Test-Path -LiteralPath $script:DataDir)) {
        try { $null = New-Item -ItemType Directory -Path $script:DataDir -Force -ErrorAction Stop }
        catch {
            SayErr ("{0} {1}" -f $script:Sym.Bad, (Resolve-Text '无法创建数据目录 backend\data' 'Cannot create the data directory backend\data'))
            SayDim (Resolve-Text ("      原因：$($_.Exception.Message)") ("      Reason: $($_.Exception.Message)"))
            SayDim ("{0} 3 = {1}" -f (Resolve-Text '退出码' 'exit code'), (Resolve-Text '依赖准备失败' 'dependency setup failed'))
            Exit-Now $script:ExitDeps $true
        }
    }
}

# ============================================================ 5. 启动与健康检查

function Get-Http {
    # 用 HttpWebRequest 而不是 Invoke-WebRequest：
    #  1) IWR 在 PowerShell 5.1 下遇到没有 charset 的 application/json 会按错误编码解码，
    #     中文 note 字段会变成乱码（实测），这里统一按 UTF-8 解码原始字节；
    #  2) 显式关掉代理，避免公司代理拦掉 127.0.0.1 的本地健康检查；
    #  3) 不依赖 System.Net.Http 程序集 —— Windows PowerShell 5.1 默认没有加载它，
    #     直接用 [System.Net.Http.HttpClient] 会抛「找不到类型」（实测）。
    param([string]$Url, [int]$TimeoutSec = 3)
    $ms = 3000
    if ($TimeoutSec -gt 0) { $ms = $TimeoutSec * 1000 }
    $req = [System.Net.WebRequest]::Create($Url)
    $req.Method = 'GET'
    $req.Proxy = $null
    $req.Timeout = $ms
    $req.ReadWriteTimeout = $ms
    try { $req.UserAgent = 'WingMan-launcher/1.0' } catch { }

    $resp = $null
    try {
        $resp = $req.GetResponse()
    }
    catch [System.Net.WebException] {
        $webResp = $_.Exception.Response
        if ($null -eq $webResp) { throw }
        $resp = $webResp
    }
    try {
        $status = [int]$resp.StatusCode
        $stream = $resp.GetResponseStream()
        $buffer = New-Object System.IO.MemoryStream
        $stream.CopyTo($buffer)
        return @{
            Status = $status
            Text   = [System.Text.Encoding]::UTF8.GetString($buffer.ToArray())
        }
    }
    finally {
        try { $resp.Close() } catch { }
    }
}

function Start-WingManServer {
    $env:PORT = [string]$script:Port

    # 用 ProcessStartInfo 而不是 Start-Process -PassThru：PowerShell 5.1 里
    # Start-Process 返回的对象读不到退出码（ExitCode 为空），会让启动器错误地
    # 报“成功”。自己 new Process 可以稳定拿到真实退出码。
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $script:VenvPython
    $psi.WorkingDirectory = $script:BackendDir
    $psi.UseShellExecute = $false
    $psi.Arguments = ('-m uvicorn ' + $script:AppTarget + ' --host 127.0.0.1 --port ' + [string]$script:Port)
    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    try {
        $null = $proc.Start()
    }
    catch {
        SayErr ("{0} {1}" -f $script:Sym.Bad, (Resolve-Text '无法启动服务进程' 'Cannot start the service process'))
        SayDim (Resolve-Text ("      原因：$($_.Exception.Message)") ("      Reason: $($_.Exception.Message)"))
        Say (Resolve-Text '      怎么修：确认 backend\.venv\Scripts\python.exe 存在且能运行' '      Fix: make sure backend\.venv\Scripts\python.exe exists and runs')
        SayDim ("{0} 1 = {1}" -f (Resolve-Text '退出码' 'exit code'), (Resolve-Text '其他错误' 'other error'))
        Exit-Now $script:ExitOther $true
    }
    return $proc
}

function Wait-WingManReady {
    param([System.Diagnostics.Process]$Proc, [int]$Port)
    $url = "http://127.0.0.1:$Port/api/health"
    $deadline = (Get-Date).AddSeconds($script:HealthTimeout)
    $health = $null
    $lastError = ''
    while ((Get-Date) -lt $deadline) {
        if ($Proc.HasExited) {
            SayErr ("{0} {1}" -f $script:Sym.Bad, (Resolve-Text ("服务进程提前退出（退出码 $($Proc.ExitCode)）") ("The service process exited early (exit code $($Proc.ExitCode))")))
            Say (Resolve-Text '      以上是 uvicorn 的原始输出；常见原因：端口被别的进程抢走、依赖损坏、数据目录不可写' '      See the uvicorn output above; usual causes: port stolen, broken deps, unwritable data dir')
            Say (Resolve-Text '      怎么修：先跑 wingman.cmd --doctor 体检，再重试' '      Fix: run wingman.cmd --doctor, then retry')
            Say ''
            SayDim ("{0} 1 = {1}" -f (Resolve-Text '退出码' 'exit code'), (Resolve-Text '其他错误' 'other error'))
            Exit-Now $script:ExitOther $true
        }
        try {
            $resp = Get-Http -Url $url -TimeoutSec 3
            if ($resp.Status -eq 200 -and $resp.Text) {
                $parsed = $resp.Text | ConvertFrom-Json
                if ($parsed.version -and $parsed.db -and $parsed.counts -and $parsed.providers) {
                    $health = $parsed
                    break
                }
                $lastError = '/api/health 返回的 JSON 缺少 version/db/counts/providers 字段'
            }
        }
        catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $health) {
        SayErr ("{0} {1}" -f $script:Sym.Bad, (Resolve-Text ("等待 $($script:HealthTimeout) 秒后 /api/health 仍未就绪") ("/api/health not ready after $($script:HealthTimeout)s")))
        if ($lastError) { SayDim (Resolve-Text ("      最后一次错误：$lastError") ("      Last error: $lastError")) }
        Say (Resolve-Text '      怎么修：另开一个窗口执行 wingman.cmd --doctor；或换端口 wingman.cmd --port 8789' '      Fix: run wingman.cmd --doctor, or try wingman.cmd --port 8789')
        try { if (-not $Proc.HasExited) { $Proc.Kill() } } catch { }
        Say ''
        SayDim ("{0} 1 = {1}" -f (Resolve-Text '退出码' 'exit code'), (Resolve-Text '其他错误' 'other error'))
        Exit-Now $script:ExitOther $true
    }

    # 首页也要能打开（前端静态文件 + 标题）
    $titleOk = $false
    try {
        $indexResp = Get-Http -Url ("http://127.0.0.1:$Port/") -TimeoutSec 5
        $titleOk = ($indexResp.Status -eq 200 -and $indexResp.Text -match '<title>WingMan')
    }
    catch { $titleOk = $false }

    return @{ Health = $health; TitleOk = $titleOk }
}

function Show-ReadyBanner {
    param([hashtable]$Ready, [int]$Port, [System.Diagnostics.Process]$Proc)
    $h = $Ready.Health
    $url = "http://127.0.0.1:$Port/"
    Say ''
    Say $script:Sym.Line '' 'Green'
    Say (Resolve-Text ' WingMan 已就绪' ' WingMan is ready') '' 'Green'
    Say $script:Sym.Line '' 'Green'
    SayFieldOk (Resolve-Text '访问地址' 'URL') $url 'URL'
    SayField (Resolve-Text '数据文件' 'data file') ([string]$h.db) 'data file' 'DarkGray'
    SayField (Resolve-Text '数据目录' 'data dir') $script:DataDir 'data dir' 'DarkGray'
    SayField (Resolve-Text '版本' 'version') ([string]$h.version) 'version' 'DarkGray'
    if ($h.counts) {
        SayField (Resolve-Text '已有数据' 'counts') (Resolve-Text ("消息 $($h.counts.messages) / 事实 $($h.counts.facts) / 会话 $($h.counts.chats)") ("messages $($h.counts.messages) / facts $($h.counts.facts) / chats $($h.counts.chats)")) 'counts' 'DarkGray'
    }
    foreach ($p in @($h.providers)) {
        $flag = 'OK'
        if (-not $p.available) { $flag = '!!' }
        $note = [string]$p.note
        SayField ([string]$p.kind) ("{0,-14} {1}  {2}" -f [string]$p.name, $flag, $note) ([string]$p.kind) 'DarkGray'
    }
    SayField (Resolve-Text '服务进程' 'pid') ([string]$Proc.Id) 'pid' 'DarkGray'
    if (-not $Ready.TitleOk) {
        SayFieldWarn (Resolve-Text '前端页面' 'frontend') (Resolve-Text '首页未返回预期的 <title>WingMan，接口可用但界面可能有问题' 'Home page did not return <title>WingMan') 'frontend'
    }
    Say $script:Sym.Thin '' 'Green'
    Say (Resolve-Text '停止服务：' 'To stop:')
    Say (Resolve-Text '  · 在这个窗口按 Ctrl+C（若提示 Terminate batch job (Y/N)，按 Y 再回车）' '  - press Ctrl+C in this window (answer Y to "Terminate batch job")')
    Say (Resolve-Text ("  · 或强制结束：taskkill /PID $($Proc.Id) /F") ("  - or force kill: taskkill /PID $($Proc.Id) /F"))
    Say (Resolve-Text '  · 或直接关闭这个窗口' '  - or just close this window')
    Say $script:Sym.Line '' 'Green'
    Say ''
}

function Open-Browser {
    param([int]$Port)
    try {
        Start-Process ("http://127.0.0.1:$Port/") -ErrorAction Stop | Out-Null
        Say (Resolve-Text '      已尝试用默认浏览器打开上面的地址（可用 --no-browser 关闭）' '      Default browser launch attempted (use --no-browser to skip)')
    }
    catch {
        SayWarn ("{0} {1}" -f $script:Sym.Warn, (Resolve-Text ("没能自动打开浏览器，请手动访问 http://127.0.0.1:$Port/") ("Could not open the browser, please visit http://127.0.0.1:$Port/ manually")))
    }
}

function Wait-ServerExit {
    param([System.Diagnostics.Process]$Proc)
    try {
        while (-not $Proc.HasExited) { Start-Sleep -Milliseconds 400 }
    }
    catch {
        Say ''
        SayWarn ("{0} {1}" -f $script:Sym.Warn, (Resolve-Text '正在停止 WingMan（等待服务优雅退出）…' 'Stopping WingMan (waiting for graceful shutdown)...'))
        try {
            if (-not $Proc.WaitForExit(15000)) {
                SayWarn ("{0} {1}" -f $script:Sym.Warn, (Resolve-Text ("服务未在 15 秒内退出，可强制结束：taskkill /PID $($Proc.Id) /F") ("Service did not exit in 15s; force kill: taskkill /PID $($Proc.Id) /F")))
            }
        }
        catch { }
    }
    # 自己 new 出来的 Process 对象持有句柄，退出码是可靠的；读不到时按“用户主动停止”处理。
    try {
        $code = $Proc.ExitCode
        if ($null -eq $code) { return 0 }
        return [int]$code
    }
    catch { return 0 }
}

# ============================================================ 6. 三条主流程

function Invoke-SetupOnly {
    Say ''
    SayInfo ("{0} 2/3 {1}" -f $script:Sym.Info, (Resolve-Text '准备依赖（--setup-only 不启动服务）' 'preparing dependencies (--setup-only does not start)'))
    Initialize-Venv
    Assert-DataDir
    $null = Install-Dependencies
    $pins = Get-LockPins -Path $script:LockFile

    Say ''
    Say $script:Sym.Line '' 'Green'
    Say (Resolve-Text ' 安装完成' ' Setup complete') '' 'Green'
    Say $script:Sym.Line '' 'Green'
    SayFieldOk (Resolve-Text '虚拟环境' 'venv') $script:VenvPython 'venv'
    SayField (Resolve-Text 'Python' 'python') (Resolve-Text ("$($script:VenvPythonVersion)（$($script:Python.Label)）") ("$($script:VenvPythonVersion) ($($script:Python.Label))")) 'python' 'DarkGray'
    SayField (Resolve-Text '已锁定' 'pinned') (Resolve-Text ("$($pins.Count) 个包（与 requirements.lock.txt 完全一致）") ("$($pins.Count) packages (exactly requirements.lock.txt)")) 'pinned' 'DarkGray'
    SayField (Resolve-Text '数据目录' 'data dir') $script:DataDir 'data dir' 'DarkGray'
    Say $script:Sym.Thin '' 'Green'
    Say (Resolve-Text '下一步：运行 wingman.cmd 启动服务（默认 http://127.0.0.1:8787/）' 'Next: run wingman.cmd to start the service')
    Say $script:Sym.Line '' 'Green'
    Exit-Now $script:ExitOk $true
}

function Invoke-Run {
    Assert-PortFree -Port $script:Port

    Say ''
    SayInfo ("{0} 2/3 {1}" -f $script:Sym.Info, (Resolve-Text '准备依赖' 'preparing dependencies'))
    Initialize-Venv
    Assert-DataDir
    $null = Install-Dependencies

    Say ''
    SayInfo ("{0} 3/3 {1}" -f $script:Sym.Info, (Resolve-Text '启动服务并自证健康' 'starting the service and waiting for health'))
    Say (Resolve-Text ("      地址：http://127.0.0.1:$($script:Port)/    数据目录：backend\data") '      URL and data dir below')
    SayDim (Resolve-Text '      ---- 以下为 WingMan 服务原始日志 ----' '      ---- raw service log below ----')
    $proc = Start-WingManServer
    $ready = Wait-WingManReady -Proc $proc -Port $script:Port
    Show-ReadyBanner -Ready $ready -Port $script:Port -Proc $proc
    if (-not $script:Opt.NoBrowser) { Open-Browser -Port $script:Port }
    else { Say (Resolve-Text '      已按 --no-browser 跳过打开浏览器' '      --no-browser given, browser not opened') }
    $code = Wait-ServerExit -Proc $proc
    Say ''
    Say (Resolve-Text 'WingMan 已退出，窗口可以关闭了。' 'WingMan has exited, this window can be closed.')
    Restore-Console
    if ($code -ne 0) { exit $script:ExitOther }
    exit $script:ExitOk
}

function Invoke-Doctor {
    Say ''
    SayInfo ("{0} {1}" -f $script:Sym.Info, (Resolve-Text '体检（不修改配置、不建库、不动你的数据、不启动服务）' 'diagnose only (no config/db/data changes, no service)'))
    SayDim (Resolve-Text '      唯一会留下的是 Python 自动生成的 __pycache__ 字节码缓存' '      the only leftover is Python''s auto-generated __pycache__ bytecode cache')
    Say ''

    $failPreflight = 0
    $failDeps = 0
    $failData = 0
    $warnCount = 0

    # ---- 仓库完整性
    $missing = @()
    foreach ($item in @(
            @{ Path = (Join-Path $script:BackendDir 'app\main.py'); What = 'backend\app\main.py' },
            @{ Path = (Join-Path $script:FrontendDir 'index.html'); What = 'frontend\index.html' },
            @{ Path = $script:LockFile; What = 'backend\requirements.lock.txt' })) {
        if (-not (Test-Path -LiteralPath $item.Path -PathType Leaf)) { $missing += $item.What }
    }
    if ($missing.Count -gt 0) {
        $failPreflight++
        SayFieldBad (Resolve-Text '仓库完整性' 'repo') (Resolve-Text ("缺少：" + ($missing -join '、')) ("missing: " + ($missing -join ', '))) 'repo'
        Say (Resolve-Text '        修：重新完整克隆或解压仓库' '        fix: re-clone the repository')
    }
    else {
        SayFieldOk (Resolve-Text '仓库完整性' 'repo') (Resolve-Text 'backend/app/main.py、frontend/index.html、requirements.lock.txt 都在' 'all required files present') 'repo'
    }

    # ---- Python
    $py = Resolve-PythonInterpreter
    $python = $py.Chosen
    if ($python) {
        $script:Python = $python
        $bits = 0
        try { $bits = Test-PythonCapability -Probe $python } catch { $bits = 0 }
        if ($bits -eq 0) {
            $failPreflight++
        }
        else {
            $script:PythonBits = $bits
            $pkgText = (Resolve-Text ("$($python.Version)（$($python.Label)，$bits 位）") ("$($python.Version) ($($python.Label), $bits-bit)"))
            if ($bits -eq 32) {
                $warnCount++
                SayFieldWarn (Resolve-Text 'Python' 'python') ($pkgText + (Resolve-Text ' —— 32 位 Python 可能装不上 numpy 等 wheel，建议装 64 位' ' - 32-bit Python may lack wheels, prefer 64-bit')) 'python'
            }
            elseif ($python.Minor -gt $script:MaxTestedMinor) {
                $warnCount++
                SayFieldWarn (Resolve-Text 'Python' 'python') ($pkgText + (Resolve-Text (" —— 未在 3.$($script:MinPythonMinor)-3.$($script:MaxTestedMinor) 之外实测") ' - outside the tested range')) 'python'
            }
            else {
                SayFieldOk (Resolve-Text 'Python' 'python') $pkgText 'python'
            }
            SayField (Resolve-Text '解释器路径' 'python exe') $python.Path 'python exe' 'DarkGray'
        }
    }
    else {
        $failPreflight++
    }

    # ---- 虚拟环境
    $venvText = $script:VenvPython
    if (Test-Path -LiteralPath $script:VenvPython -PathType Leaf) {
        $vraw = ''
        try { $vraw = (& $script:VenvPython '-c' 'import sys; print(sys.version.split()[0])' 2>&1 | ForEach-Object { [string]$_ }) -join ' ' } catch { $vraw = '' }
        if ($LASTEXITCODE -eq 0) {
            $script:VenvPythonVersion = $vraw.Trim()
            SayFieldOk (Resolve-Text '虚拟环境' 'venv') (Resolve-Text ("存在，Python $($script:VenvPythonVersion)（$venvText）") ("exists, Python $($script:VenvPythonVersion)")) 'venv'
        }
        else {
            $failDeps++
            SayFieldBad (Resolve-Text '虚拟环境' 'venv') (Resolve-Text '存在但无法运行，需要重建' 'exists but is broken, needs a rebuild') 'venv'
            Say (Resolve-Text '        修：wingman.cmd --setup-only' '        fix: wingman.cmd --setup-only')
        }
    }
    else {
        $failDeps++
        SayFieldBad (Resolve-Text '虚拟环境' 'venv') (Resolve-Text '尚未创建（backend\.venv\Scripts\python.exe 不存在）' 'not created yet (backend\.venv\Scripts\python.exe missing)') 'venv'
        Say (Resolve-Text '        修：wingman.cmd --setup-only' '        fix: wingman.cmd --setup-only')
    }

    # ---- 依赖
    $pins = Get-LockPins -Path $script:LockFile
    SayField (Resolve-Text '依赖锁' 'lock') (Resolve-Text ("$($pins.Count) 个精确版本（backend\requirements.lock.txt）") ("$($pins.Count) exact pins (backend\requirements.lock.txt)")) 'lock' 'DarkGray'
    if (Test-Path -LiteralPath $script:VenvPython -PathType Leaf) {
        $check = Test-Dependencies
        if ($check.Ok) {
            SayFieldOk (Resolve-Text '依赖完整性' 'deps') (Resolve-Text ("与锁文件一致，且 app.main 可导入（$($pins.Count) 个包）") ("matches the lock and app.main imports ($($pins.Count) packages)")) 'deps'
        }
        else {
            $failDeps++
            SayFieldBad (Resolve-Text '依赖完整性' 'deps') (Resolve-Text ("有 $($check.Problems.Count) 项不符合") ("$($check.Problems.Count) problem(s)")) 'deps'
            Show-DependencyProblems -Check $check
            Say (Resolve-Text '        修：wingman.cmd --setup-only' '        fix: wingman.cmd --setup-only')
        }
    }
    else {
        $failDeps++
        SayFieldBad (Resolve-Text '依赖完整性' 'deps') (Resolve-Text '无法校验（还没有虚拟环境）' 'cannot verify (no venv yet)') 'deps'
    }

    # ---- 数据目录与路径（这类问题属于「环境」，不算依赖问题，建议也不能指向 --setup-only）
    if (Test-Path -LiteralPath $script:DataDir) {
        if (-not (Test-Path -LiteralPath $script:DataDir -PathType Container)) {
            $failData++
            SayFieldBad (Resolve-Text '数据目录' 'data dir') (Resolve-Text ("backend\data 存在但不是目录（同名文件？）") ("backend\data exists but is not a directory (same-name file?)")) 'data dir'
            Say (Resolve-Text '        修：把 backend\data 这个同名文件删掉或改名，让它恢复成目录后重试（--setup-only 修不了这个）' '        fix: delete/rename that same-name file so backend\data is a directory again, then retry (--setup-only cannot fix this)')
        }
        else {
            $probe = Join-Path $script:DataDir '.wingman-write-test'
            $writable = $false
            try {
                [System.IO.File]::WriteAllText($probe, 'ok')
                $writable = $true
                Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
            }
            catch { $writable = $false }
            $dbPath = Join-Path $script:DataDir 'wingman.db'
            $dbText = (Resolve-Text '首次启动会自动创建' 'created on first start')
            if (Test-Path -LiteralPath $dbPath) { $dbText = (Resolve-Text '已存在' 'exists') }
            if ($writable) {
                SayFieldOk (Resolve-Text '数据目录' 'data dir') (Resolve-Text ("可写（$($script:DataDir)），数据库：$dbText") ("writable ($($script:DataDir)), db: $dbText")) 'data dir'
            }
            else {
                $failData++
                SayFieldBad (Resolve-Text '数据目录' 'data dir') (Resolve-Text '目录存在但不可写（只读 / 权限 / 被占用）' 'directory exists but is not writable (read-only / permissions / locked)') 'data dir'
                Say (Resolve-Text '        修：检查 backend\data 的写权限；只读或被别的东西占用（安全软件、同步盘、编辑器）请解除后重试（--setup-only 修不了这个）' '        fix: check write permissions; clear read-only/locks (antivirus, sync drives, editors) and retry (--setup-only cannot fix this)')
            }
        }
    }
    else {
        $warnCount++
        SayFieldWarn (Resolve-Text '数据目录' 'data dir') (Resolve-Text ("不存在（$($script:DataDir)），启动时会自动创建") ("missing ($($script:DataDir)), will be created") ) 'data dir'
    }

    # ---- 端口
    $portCheck = Test-PortAvailable -Port $script:Port
    if ($portCheck.Free) {
        SayFieldOk (Resolve-Text '端口' 'port') (Resolve-Text ("127.0.0.1:$($script:Port) 可用") ("127.0.0.1:$($script:Port) is free")) 'port'
    }
    else {
        $failPreflight++
        $o = $portCheck.Owner
        $detail = (Resolve-Text ("127.0.0.1:$($script:Port) 已被占用") ("127.0.0.1:$($script:Port) is in use"))
        if ($o.Pid -gt 0) { $detail = $detail + (' - PID ' + $o.Pid + ' ' + $o.Name) }
        SayFieldBad (Resolve-Text '端口' 'port') $detail 'port'
        Say (Resolve-Text ("        修：wingman.cmd --port $($script:Port + 1)（若这是你自己已启动的 WingMan，可以忽略本条）") '        fix: wingman.cmd --port <other>')
    }

    Say ''
    if ($failPreflight -gt 0) {
        $extraZh = ''
        $extraEn = ''
        if ($failDeps -gt 0) { $extraZh += "，另有 $failDeps 项依赖问题"; $extraEn += ", plus $failDeps dependency issue(s)" }
        if ($failData -gt 0) { $extraZh += "，另有 $failData 项数据目录/路径问题"; $extraEn += ", plus $failData data-dir/path issue(s)" }
        SayErr ("{0} {1}" -f $script:Sym.Bad, (Resolve-Text ("体检未通过：$failPreflight 项前置问题" + $extraZh) ("Health check failed: $failPreflight preflight issue(s)" + $extraEn)))
        SayDim (Resolve-Text '体检不会改动你的配置与数据；请按上面的「修」逐条处理后重试' 'Your config and data were not modified; fix the items above and retry')
        Exit-Now $script:ExitPreflight $true
    }
    elseif ($failDeps -gt 0 -or $failData -gt 0) {
        # 依赖问题与环境问题分开报，建议各自对症：数据目录类问题跑 --setup-only 没有用
        if ($failDeps -gt 0) {
            SayErr ("{0} {1}" -f $script:Sym.Bad, (Resolve-Text ("依赖尚未就绪：$failDeps 项") ("Dependencies not ready: $failDeps issue(s)")))
            Say (Resolve-Text '下一步：wingman.cmd --setup-only' 'Next: wingman.cmd --setup-only')
        }
        if ($failData -gt 0) {
            SayErr ("{0} {1}" -f $script:Sym.Bad, (Resolve-Text ("数据目录/路径有问题：$failData 项") ("Data directory / path problem(s): $failData")))
            Say (Resolve-Text '下一步：确认 backend\data 是目录且有写权限；只读或被占用请解除后重试（这类问题 --setup-only 修不了）' 'Next: make sure backend\data is a directory and writable; clear read-only/locks and retry (--setup-only cannot fix this)')
        }
        Exit-Now $script:ExitDeps $true
    }
    else {
        SayOk ("{0} {1}" -f $script:Sym.Ok, (Resolve-Text '体检通过：可以直接运行 wingman.cmd 启动服务' 'All checks passed: run wingman.cmd to start'))
        if ($warnCount -gt 0) { SayDim (Resolve-Text ("（有 $warnCount 条提醒，不阻断启动）") ("($warnCount warning(s), not blocking)")) }
        Exit-Now $script:ExitOk $true
    }
}

# ============================================================ 7. 入口

try {
    Initialize-Console
    $script:Opt = Read-WingmanArgs -Raw $RawArgs

    if ($script:Opt.Help) {
        Show-Help
        Exit-Now $script:ExitOk $true
    }
    $null = Resolve-ArgumentErrors

    if ($script:Opt.Doctor) {
        Show-EnvironmentHeader
        Invoke-Doctor
    }

    Show-EnvironmentHeader
    Say ''
    SayInfo ("{0} 1/3 {1}" -f $script:Sym.Info, (Resolve-Text '前置自检' 'preflight checks'))
    Assert-RepoLayout
    $resolved = Resolve-PythonInterpreter
    $script:Python = $resolved.Chosen
    $script:PythonBits = Test-PythonCapability
    Say (Resolve-Text ("      Python 可用：$($script:Python.Version)（$($script:Python.Label)，$($script:PythonBits) 位）") ("      Python ok: $($script:Python.Version) ($($script:Python.Label), $($script:PythonBits)-bit)")) '' 'Green'
    if ($script:PythonBits -eq 32) {
        SayWarn ("{0} {1}" -f $script:Sym.Warn, (Resolve-Text '检测到 32 位 Python：numpy 等包可能没有对应 wheel，建议改用 64 位 Python' '32-bit Python detected: some wheels may be missing, prefer 64-bit'))
    }
    elseif ($script:Python.Minor -gt $script:MaxTestedMinor) {
        SayWarn ("{0} {1}" -f $script:Sym.Warn, (Resolve-Text ("检测到 Python $($script:Python.Version)：只在 3.$($script:MinPythonMinor)-3.$($script:MaxTestedMinor) 上实测过；若装依赖失败请改用 3.11-3.13") 'Untested Python version; use 3.11-3.13 if installs fail'))
    }
    SayField (Resolve-Text '虚拟环境' 'venv') (Resolve-Text 'backend\.venv\Scripts\python.exe（固定路径）' 'backend\.venv\Scripts\python.exe (fixed)') 'venv' 'DarkGray'
    if ($script:Opt.SetupOnly) {
        SayField (Resolve-Text '端口' 'port') (Resolve-Text '跳过（--setup-only 不启动服务，不占用端口）' 'skipped (--setup-only does not listen)') 'port' 'DarkGray'
    }
    else {
        $portZh = "127.0.0.1:$($script:Port)"
        $portEn = $portZh
        if ($script:PortFromEnv) {
            $portZh += '（来自环境变量 PORT）'
            $portEn += ' (from env PORT)'
        }
        SayField (Resolve-Text '端口' 'port') (Resolve-Text $portZh $portEn) 'port' 'DarkGray'
    }

    if ($script:Opt.SetupOnly) {
        Invoke-SetupOnly
    }
    else {
        Invoke-Run
    }
}
catch {
    try {
        Say ''
        SayErr ("{0} {1}" -f $script:Sym.Bad, (Resolve-Text ("启动器遇到未预期的错误：" + $_.Exception.Message) ("Launcher hit an unexpected error: " + $_.Exception.Message)))
        if ($_.InvocationInfo) { SayDim ('      ' + [string]$_.InvocationInfo.PositionMessage) }
        if ($_.ScriptStackTrace) { SayDim ('      ' + [string]$_.ScriptStackTrace) }
        Say (Resolve-Text '      怎么修：把上面的信息连同 wingman.cmd --doctor 的输出一起反馈' '      Fix: report this together with the output of wingman.cmd --doctor')
    }
    catch { }
    Exit-Now $script:ExitOther $true
}
finally {
    Restore-Console
}
