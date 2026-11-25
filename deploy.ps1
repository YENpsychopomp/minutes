# =============================================================================
# summery 一鍵啟動／部署（Windows PowerShell 版，對應 deploy.sh）
#
#   .\deploy.ps1              建置前端（需要的話）並在本機啟動完整服務
#   .\deploy.ps1 --dev        開發模式：同時跑 Vite 前端熱更新 + Python API
#   .\deploy.ps1 --build      強制重新建置前端
#   .\deploy.ps1 --no-build   跳過前端建置（直接用現成的 frontend\dist）
#   .\deploy.ps1 --port 9000  換一個連接埠
#   .\deploy.ps1 --host 0.0.0.0  綁到整個網路（請自行處理防火牆／身分驗證）
#   .\deploy.ps1 --jobs-dir D:\jobs  換工作目錄
#   .\deploy.ps1 --python .venv\Scripts\python.exe  指定 Python 直譯器
#   .\deploy.ps1 --env myenv  自動啟用的 conda 環境名稱（預設 summery；不必先 conda activate）
#   .\deploy.ps1 --no-conda   不要自動啟用 conda 環境
#   .\deploy.ps1 --open       啟動後打開瀏覽器
#
# 參數也可以寫成 PowerShell 風格：-Dev、-Port 9000、-NoBuild、-JobsDir D:\jobs …
#
# 被執行原則擋下時，改用 deploy.cmd，或：
#   powershell -ExecutionPolicy Bypass -File .\deploy.ps1
#
# 重要：這個腳本「從頭到尾在前台執行」。
#   * 按 Ctrl+C，或直接關閉 PowerShell 視窗 → 服務與所有子工程式立即一起停止。
#   * 沒有背景服務、沒有排程、沒有開機自啟；視窗關掉，系統就停了。
#
# 相容 Windows PowerShell 5.1 與 PowerShell 7+。本檔需以 UTF-8（含 BOM）儲存，
# 否則 Windows PowerShell 5.1 會把中文讀成亂碼。
# =============================================================================
Set-StrictMode -Version 2.0

$Root = $PSScriptRoot
# 服務要在專案根目錄執行（.env 與預設的 summery_jobs 都相對於它）。
# PowerShell 腳本跑在呼叫者的工作階段裡，結束時要把目錄還原，不能像 bash 一樣直接 cd。
Push-Location -LiteralPath $Root

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$BindHost = '127.0.0.1'
$Port = '8765'
$JobsDir = ''
$Python = if ($env:PYTHON) { $env:PYTHON } else { '' }
$Build = 'auto'       # auto | yes | no
$Dev = $false
$Open = $false
$CondaEnv = if ($env:SUMMERY_CONDA_ENV) { $env:SUMMERY_CONDA_ENV } else { 'summery' }
$script:SavedEnv = $null   # 自動啟用 conda 前的環境變數，結束時還原

function Write-Info([string]$Message) { Write-Host '• ' -ForegroundColor Cyan -NoNewline; Write-Host $Message }
function Write-Warn([string]$Message) { Write-Host '! ' -ForegroundColor Yellow -NoNewline; Write-Host $Message }
function Write-Ok([string]$Message) { Write-Host '✓ ' -ForegroundColor Green -NoNewline; Write-Host $Message }

function Restore-CondaEnv {
    # 腳本跑在呼叫者的 PowerShell 裡：自動啟用的環境只給這次服務用，結束後把變數還原
    if ($null -eq $script:SavedEnv) { return }
    foreach ($key in $script:SavedEnv.Keys) {
        $value = $script:SavedEnv[$key]
        if ($null -eq $value) { Remove-Item -LiteralPath "Env:$key" -ErrorAction SilentlyContinue }
        else { Set-Item -LiteralPath "Env:$key" -Value $value }
    }
    $script:SavedEnv = $null
}

function Exit-Script([int]$Code) { Restore-CondaEnv; Pop-Location; exit $Code }
function Fail([string]$Message) { Write-Host "✗ $Message" -ForegroundColor Red; Exit-Script 1 }

function Show-Usage {
    $lines = Get-Content -LiteralPath $PSCommandPath -Encoding UTF8
    foreach ($line in ($lines | Select-Object -Skip 1)) {
        if ($line -notmatch '^#') { break }
        Write-Host ($line -replace '^# ?', '')
    }
}

# ---------------------------------------------------------------- 參數
$i = 0
while ($i -lt $args.Count) {
    $raw = [string]$args[$i]
    $name = ($raw -replace '^-{1,2}', '').ToLowerInvariant()
    $next = if ($i + 1 -lt $args.Count) { [string]$args[$i + 1] } else { $null }
    switch ($name) {
        'dev' { $Dev = $true; $i += 1 }
        'build' { $Build = 'yes'; $i += 1 }
        { $_ -in 'no-build', 'nobuild' } { $Build = 'no'; $i += 1 }
        'port' { if (-not $next) { Fail '--port 需要一個數字' }; $Port = $next; $i += 2 }
        { $_ -in 'host', 'bindhost' } { if (-not $next) { Fail '--host 需要一個位址' }; $BindHost = $next; $i += 2 }
        { $_ -in 'jobs-dir', 'jobsdir' } { if (-not $next) { Fail '--jobs-dir 需要一個路徑' }; $JobsDir = $next; $i += 2 }
        'python' { if (-not $next) { Fail '--python 需要一個 Python 直譯器路徑' }; $Python = $next; $i += 2 }
        { $_ -in 'env', 'conda-env', 'condaenv' } { if (-not $next) { Fail '--env 需要一個 conda 環境名稱' }; $CondaEnv = $next; $i += 2 }
        { $_ -in 'no-conda', 'noconda' } { $CondaEnv = ''; $i += 1 }
        'open' { $Open = $true; $i += 1 }
        { $_ -in 'h', 'help', '?' } { Show-Usage; Exit-Script 0 }
        default { Fail "不認識的參數：$raw（用 --help 看說明）" }
    }
}
if ($Port -notmatch '^\d+$') { Fail "--port 需要一個數字（收到：$Port）" }

# ---------------------------------------------------------------- 0. conda 環境
function Find-CondaEnv([string]$Name) {
    # 先看常見的安裝位置（快），找不到再問 conda 本身（較慢，但涵蓋自訂位置）
    $roots = @()
    if ($env:CONDA_EXE) { $roots += (Split-Path -Parent (Split-Path -Parent $env:CONDA_EXE)) }
    foreach ($dir in 'anaconda3', 'miniconda3', 'miniforge3', 'mambaforge') {
        $roots += (Join-Path $env:USERPROFILE $dir)
        if ($env:LOCALAPPDATA) { $roots += (Join-Path $env:LOCALAPPDATA $dir) }
        if ($env:ProgramData) { $roots += (Join-Path $env:ProgramData $dir) }
    }
    $candidates = @($roots | ForEach-Object { Join-Path (Join-Path $_ 'envs') $Name })
    $candidates += (Join-Path (Join-Path (Join-Path $env:USERPROFILE '.conda') 'envs') $Name)
    foreach ($dir in $candidates) {
        if (Test-Path -LiteralPath (Join-Path $dir 'python.exe') -PathType Leaf) { return $dir }
    }
    if (Get-Command conda -ErrorAction SilentlyContinue) {
        try {
            $info = (& conda env list --json 2>$null | Out-String) | ConvertFrom-Json
            foreach ($dir in @($info.envs)) {
                if ((Split-Path -Leaf $dir) -ieq $Name -and (Test-Path -LiteralPath (Join-Path $dir 'python.exe'))) { return $dir }
            }
        }
        catch { }
    }
    return $null
}

function Enable-CondaEnv([string]$Dir, [string]$Name) {
    # 等同 conda activate 對 PATH 與環境變數的設定：python 與 ffmpeg（在 Library\bin）都會找得到
    $script:SavedEnv = @{}
    foreach ($key in 'PATH', 'CONDA_PREFIX', 'CONDA_DEFAULT_ENV', 'CONDA_PROMPT_MODIFIER') {
        $script:SavedEnv[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
    }
    $paths = @(
        $Dir,
        (Join-Path $Dir 'Library\mingw-w64\bin'),
        (Join-Path $Dir 'Library\usr\bin'),
        (Join-Path $Dir 'Library\bin'),
        (Join-Path $Dir 'Scripts'),
        (Join-Path $Dir 'bin')
    )
    $env:PATH = (($paths | Where-Object { Test-Path -LiteralPath $_ }) -join ';') + ';' + $env:PATH
    $env:CONDA_PREFIX = $Dir
    $env:CONDA_DEFAULT_ENV = $Name
    $env:CONDA_PROMPT_MODIFIER = "($Name) "
}

if ($CondaEnv) {
    $activeName = if ($env:CONDA_PREFIX) { Split-Path -Leaf $env:CONDA_PREFIX } else { '' }
    if ($activeName -ieq $CondaEnv) {
        Write-Ok "conda 環境 $CondaEnv 已啟用"
    }
    else {
        $envDir = Find-CondaEnv $CondaEnv
        if ($envDir) {
            Enable-CondaEnv $envDir $CondaEnv
            Write-Ok "已自動啟用 conda 環境 $CondaEnv（$envDir）"
        }
        else {
            Write-Warn "找不到 conda 環境 $CondaEnv，沿用目前的 Python（建立方式見 README；或用 --env 指定名稱、--no-conda 略過）"
        }
    }
}

# ---------------------------------------------------------------- 1. Python
function Test-PythonExe([string]$Exe) {
    # 排除 Windows「應用程式執行別名」那個只會打開 Microsoft Store 的假 python.exe
    & $Exe -c 'import sys' 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Test-PythonVersion([string]$Exe) {
    & $Exe -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Get-CondaPython {
    # conda activate 會設定 CONDA_PREFIX；Windows 的 conda 環境把 python.exe 放在環境根目錄
    if (-not $env:CONDA_PREFIX) { return $null }
    $exe = Join-Path $env:CONDA_PREFIX 'python.exe'
    if (Test-Path -LiteralPath $exe -PathType Leaf) { return $exe }
    return $null
}

function Resolve-Python {
    if ($script:Python) {
        if (Test-Path -LiteralPath $script:Python -PathType Leaf) {
            return (Resolve-Path -LiteralPath $script:Python).Path
        }
        $cmd = Get-Command $script:Python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $cmd) { Fail "指定的 Python 不可執行：$($script:Python)" }
        return $cmd.Source
    }
    $venvPython = Join-Path $Root '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) { return $venvPython }

    # 已啟用的 conda 環境。py 啟動器不認得 conda，所以要排在它前面，否則會選到系統的 Python。
    # 版本不到 3.11（例如舊版 Anaconda 自動啟用的 base）就略過，繼續往下找。
    $condaPython = Get-CondaPython
    if ($condaPython -and (Test-PythonVersion $condaPython)) { return $condaPython }

    # Python 官方安裝程式附的 py 啟動器：問它真正的 python.exe 在哪
    if (Get-Command py -CommandType Application -ErrorAction SilentlyContinue) {
        $exe = & py -3 -c 'import sys; print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $exe) { return ([string]$exe).Trim() }
    }
    foreach ($candidate in 'python', 'python3') {
        foreach ($cmd in @(Get-Command $candidate -CommandType Application -ErrorAction SilentlyContinue)) {
            if (Test-PythonExe $cmd.Source) { return $cmd.Source }
        }
    }
    Fail '找不到 Python。請先安裝 Python 3.11 以上版本（winget install Python.Python.3.12），用 conda 的話請先 conda activate 對應的環境。'
}

$Python = Resolve-Python
if (-not (Test-PythonExe $Python)) { Fail "指定的 Python 不可執行：$Python" }

$PyVersion = & $Python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if (-not (Test-PythonVersion $Python)) { Fail "需要 Python 3.11 以上，目前是 $PyVersion（$Python）" }
Write-Info "Python $PyVersion（$Python）"

# 缺套件時：在 .venv 裡就直接安裝（只裝一次；之後直接重用），否則用原始碼路徑跑
& $Python -c 'import summery' 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    $venvPython = Join-Path $Root '.venv\Scripts\python.exe'
    if ($Python -ieq $venvPython) {
        Write-Info '安裝 summery（第一次會慢一點）…'
        & $Python -m pip install --quiet --disable-pip-version-check -e $Root
        if ($LASTEXITCODE -ne 0) { Fail '安裝失敗，請查看上面的 pip 訊息' }
        Write-Ok '已安裝到 .venv'
    }
    else {
        # 沒裝成套件就用原始碼路徑跑，免安裝、免動到系統環境（Windows 的路徑分隔是分號）
        $src = Join-Path $Root 'src'
        $env:PYTHONPATH = if ($env:PYTHONPATH) { "$src;$($env:PYTHONPATH)" } else { $src }
        $installHint = if ($Python -ieq (Get-CondaPython)) { 'pip install -e .' } else { 'py -m venv .venv; .venv\Scripts\pip install -e .' }
        Write-Warn "未安裝為套件，改用 PYTHONPATH=src 執行（要安裝可先執行：$installHint）"
    }
}

# ---------------------------------------------------------------- 2. ffmpeg
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    Write-Ok 'ffmpeg 就緒'
}
else {
    Write-Warn '找不到 ffmpeg：轉錄需要它來切塊與抽音軌。可先執行 winget install Gyan.FFmpeg（裝完重開視窗）'
}

# ---------------------------------------------------------------- 3. .env 檢查
$EnvFile = Join-Path $Root '.env'
if (Test-Path -LiteralPath $EnvFile -PathType Leaf) {
    if (Select-String -LiteralPath $EnvFile -Pattern '^\s*(ASR|LLM)_API_KEY=.+' -Quiet) {
        Write-Ok '已找到 .env（summery 啟動時會自動載入）'
    }
    else {
        Write-Warn '.env 存在但沒有看到 ASR_API_KEY／LLM_API_KEY，工作可能會失敗'
    }
}
else {
    Write-Warn '沒有 .env：請複製 .env.example 為 .env 並填入金鑰（Copy-Item .env.example .env）'
}

# ---------------------------------------------------------------- 4. 前端
$Frontend = Join-Path $Root 'frontend'
$Dist = Join-Path $Frontend 'dist\index.html'
$ViteJs = Join-Path $Frontend 'node_modules\vite\bin\vite.js'
$script:PM = @()        # 套件管理員指令，例如 @('pnpm') 或 @('corepack', 'pnpm')
$script:DevMode = ''    # pm | node | ''

function Test-NodeReady {
    if (-not (Get-Command node -CommandType Application -ErrorAction SilentlyContinue)) { return $false }
    & node -e "const [a]=process.versions.node.split('.');process.exit(Number(a)>=18?0:1)" 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Select-PackageManager {
    if (Get-Command pnpm -ErrorAction SilentlyContinue) {
        $script:PM = @('pnpm')
    }
    elseif (Get-Command corepack -ErrorAction SilentlyContinue) {
        & corepack pnpm --version 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $script:PM = @('corepack', 'pnpm') }
    }
    if ($script:PM.Count -eq 0 -and (Get-Command npm -ErrorAction SilentlyContinue)) {
        $script:PM = @('npm')
    }

    if ($script:PM.Count -gt 0) {
        $script:DevMode = 'pm'
    }
    elseif ((Test-NodeReady) -and (Test-Path -LiteralPath $ViteJs)) {
        # 有 node、依賴也裝好了，只是 PATH 上沒有套件管理員：直接叫用 Vite
        $script:DevMode = 'node'
    }
    else {
        $script:DevMode = ''
    }
}

function Invoke-PM {
    $exe = $script:PM[0]
    $prefix = @()
    if ($script:PM.Count -gt 1) { $prefix = $script:PM[1..($script:PM.Count - 1)] }
    & $exe @prefix @args
}

function Build-Frontend {
    if (-not (Test-NodeReady)) {
        if ($Build -eq 'yes') { Fail '建置前端需要 Node.js 18 以上（目前找不到可用的 node）' }
        Write-Warn '找不到 Node.js 18+，跳過前端建置，將使用內建的陽春介面'
        return
    }

    Select-PackageManager
    if ($script:PM.Count -eq 0) { Fail '需要 pnpm 或 npm 才能安裝前端依賴（請先安裝 Node.js 18+：winget install OpenJS.NodeJS.LTS）' }
    Write-Info "安裝前端依賴（$($script:PM -join ' ')）…"
    Push-Location -LiteralPath $Frontend
    try {
        Invoke-PM install --frozen-lockfile=false 2>$null
        if ($LASTEXITCODE -ne 0) {
            Invoke-PM install
            if ($LASTEXITCODE -ne 0) { Fail '前端依賴安裝失敗，請查看上面的訊息' }
        }

        Write-Info '建置前端（Vite）…'
        Invoke-PM run build | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail '前端建置失敗（可手動在 frontend 目錄執行 pnpm run build 看完整訊息）' }
    }
    finally {
        Pop-Location
    }
    if (-not (Test-Path -LiteralPath $Dist)) { Fail '前端建置未產生 dist\index.html' }
    Write-Ok '前端已建置 → frontend\dist'
}

if ($Dev) {
    if (-not (Test-NodeReady)) { Fail '開發模式需要 Node.js 18 以上' }
    if (Test-Path -LiteralPath (Join-Path $Frontend 'node_modules')) { Select-PackageManager } else { Build-Frontend }
    if (-not $script:DevMode) { Fail '找不到可啟動 Vite 的方式（pnpm／npm／node_modules\vite 都沒有）' }
}
elseif ($Build -eq 'no') {
    if (Test-Path -LiteralPath $Dist) { Write-Ok '使用現成的 frontend\dist（--no-build）' }
    else { Write-Warn '沒有 frontend\dist 又指定了 --no-build，將使用內建的陽春介面' }
}
elseif ($Build -eq 'yes' -or -not (Test-Path -LiteralPath $Dist)) {
    Build-Frontend
}
else {
    Write-Ok '前端已就緒（frontend\dist）；要重建請加 --build'
}

# ---------------------------------------------------------------- 5. 啟動
$ServeArgs = @('-m', 'summery', 'serve', '--host', $BindHost, '--port', $Port)
if ($JobsDir) { $ServeArgs += @('--jobs-dir', $JobsDir) }
if ($Open) { $ServeArgs += '--open' }

$VitePort = if ($env:VITE_PORT) { $env:VITE_PORT } else { '5173' }

function Stop-Tree($Process) {
    # /T 連同子行程（例如 cmd → pnpm → node/vite 的多層結構）一起收掉，確保不留孤兒
    if ($null -ne $Process -and -not $Process.HasExited) {
        & taskkill.exe /PID $Process.Id /T /F 2>$null | Out-Null
    }
}

Write-Host ''
Write-Info "summery 啟動中 → http://$($BindHost):$Port/"
Write-Info '停止：按 Ctrl+C，或直接關閉這個 PowerShell 視窗'
Write-Host ''

if ($Dev) {
    # 開發模式：Python API + Vite 開發伺服器（Vite 會把 /api 反向代理到上面的埠）
    Write-Info "API   http://127.0.0.1:$Port"
    Write-Info "前端  http://127.0.0.1:$VitePort（/api 自動轉到 $Port）"

    # 讓 Vite 把 /api 轉到這次的埠，而不是寫死的 8765（子行程會繼承環境變數）
    $env:SUMMERY_API_ORIGIN = "http://127.0.0.1:$Port"
    $children = @()
    try {
        # -NoNewWindow：子行程共用這個主控台，關閉視窗時會跟著一起結束
        $children += Start-Process -FilePath $Python -NoNewWindow -PassThru -WorkingDirectory $Root `
            -ArgumentList "-m summery serve --host 127.0.0.1 --port $Port"

        if ($script:DevMode -eq 'pm') {
            # pnpm／npm 在 Windows 是 .cmd 包裝，交給 cmd.exe 解析最穩
            $viteArgs = "--host 127.0.0.1 --port $VitePort"
            if ($script:PM[0] -eq 'npm') { $viteArgs = "-- $viteArgs" }   # npm 需要 -- 才會轉交參數
            $children += Start-Process -FilePath $env:ComSpec -NoNewWindow -PassThru -WorkingDirectory $Frontend `
                -ArgumentList "/d /c $($script:PM -join ' ') run dev $viteArgs"
        }
        else {
            $children += Start-Process -FilePath 'node' -NoNewWindow -PassThru -WorkingDirectory $Frontend `
                -ArgumentList "`"$ViteJs`" --host 127.0.0.1 --port $VitePort"
        }

        # 任一邊結束就整套收掉
        while ($true) {
            $exited = @($children | Where-Object { $_.HasExited })
            if ($exited.Count -gt 0) { break }
            Start-Sleep -Milliseconds 500
        }
    }
    finally {
        Write-Host ''
        Write-Info '正在停止…（關閉視窗也會有同樣效果）'
        foreach ($child in $children) { Stop-Tree $child }
        Write-Ok '已全部停止'
        Restore-CondaEnv
        Pop-Location
    }
}
else {
    # 正式模式：直接在前台跑服務；Ctrl+C 或關閉視窗時 Python 會一起結束
    $code = 0
    try {
        & $Python @ServeArgs
        $code = $LASTEXITCODE
    }
    finally {
        Write-Host ''
        Write-Ok '已停止'
        Restore-CondaEnv
        Pop-Location
    }
    exit $code
}
