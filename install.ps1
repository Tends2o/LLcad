<#
.SYNOPSIS
Installs LLcad on Windows: tools, build, drawing support, Claude Code skill and MCP server entry.

.DESCRIPTION
No administrator rights are needed. Everything is installed into this folder (tools\, models\,
LLcad\) and into the Claude Code configuration folder. The script can be run again at any time:
downloads and builds that are present are reused and interrupted downloads resume. LLcad does
not run as a service: the stdio bridge starts the server on the first CAD tool call and the
server exits by itself after -IdleMinutes without requests.

  powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1                     full install
  powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -DrawingModel yes   also download the local vision model
  powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -SkipBuild          only rewrite the configuration
  powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -NoClaude           no skill, memory or MCP entry

.PARAMETER DrawingModel
ask (default): ask before downloading the local vision model for drawing generation (15.4 GB).
yes: download it without asking. no: skip it. In a non-interactive session 'ask' skips it.

.PARAMETER ClaudeCode
ask (default): offer to install Claude Code with its official installer if it is missing.
yes: install it without asking. no: never install it.

.PARAMETER SkipBuild
Keep tools and build as they are and only rewrite the configuration.

.PARAMETER NoClaude
Do not install the skill, the memory rule or the MCP entry.

.PARAMETER NoShortcut
Do not create the Start menu entry "LLcad Viewer".

.PARAMETER Port
Local port of the LLcad server (default 4310).

.PARAMETER IdleMinutes
The server exits after this many minutes without requests (default 15).
#>
param(
  [ValidateSet('ask', 'yes', 'no')][string]$DrawingModel = 'ask',
  [ValidateSet('ask', 'yes', 'no')][string]$ClaudeCode = 'ask',
  [switch]$SkipBuild,
  [switch]$NoClaude,
  [switch]$NoShortcut,
  [int]$Port = 4310,
  [int]$IdleMinutes = 15
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$OutputEncoding = New-Object Text.UTF8Encoding $false
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Package = $PSScriptRoot
$LLcad = Join-Path $Package 'LLcad'
$Data = Join-Path $LLcad 'data'
$Tools = Join-Path $Package 'tools'
$Downloads = Join-Path $Tools 'downloads'
$Native = Join-Path $Tools 'native'
$Models = Join-Path $Package 'models'
$Fonts = Join-Path $Package 'fonts'
$Projects = Split-Path $Package -Parent
$ClaudeHome = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $env:USERPROFILE '.claude' }
$SkillDir = Join-Path $ClaudeHome 'skills\llcad-modelling'
$Url = "http://127.0.0.1:$Port"
$NodeDir = Join-Path $Tools 'node'
$Node = Join-Path $NodeDir 'node.exe'
$Py = Join-Path $LLcad '.venv\Scripts\python.exe'
$Bridge = Join-Path $LLcad 'bin\llcad-bridge.mjs'
$Interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected -and
  -not ([Environment]::GetCommandLineArgs() | Where-Object { $_ -like '-NonI*' })

function Say($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Info($text) { Write-Host "    $text" }
function Warn($text) { Write-Host "!!  $text" -ForegroundColor Yellow }
function Die($text) { Write-Host "ERROR: $text" -ForegroundColor Red; exit 1 }
function Fwd($path) { $path -replace '\\', '/' }

function Run {
  # Native programs: check the exit code. Their stderr output (progress, warnings) must not abort
  # the script, which Windows PowerShell 5.1 would do with ErrorActionPreference 'Stop' when the
  # caller redirects the output.
  $exe = $args[0]
  $rest = @($args | Select-Object -Skip 1)
  $old = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try { & $exe @rest } finally { $ErrorActionPreference = $old }
  if ($LASTEXITCODE -ne 0) { Die "$exe $($rest -join ' ') failed with exit code $LASTEXITCODE" }
}

function Test-Sha256($file, $sha256) {
  if (-not $sha256) { return $true }
  return (Get-FileHash -Algorithm SHA256 $file).Hash.ToLower() -eq $sha256.ToLower()
}

function Get-File($url, $dest, $sha256, [double]$size = 0) {
  # Downloads to <dest>.part (resumable), checks SHA-256 and only then renames to <dest>.
  if (Test-Path $dest) {
    if (Test-Sha256 $dest $sha256) { return $dest }
    Warn "$(Split-Path $dest -Leaf) does not match its checksum; downloading it again"
    Remove-Item -Force $dest
  }
  New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
  $part = "$dest.part"
  $name = Split-Path $dest -Leaf
  if ($size -gt 0) { Info ('downloading {0} ({1:N1} MB)' -f $name, ($size / 1MB)) } else { Info "downloading $name" }
  $shown = if ($size -gt 200MB) { '--progress-bar' } else { '--silent' }
  $old = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try { & $CurlExe -fL --show-error $shown @CurlRetry -C - -o $part $url } finally { $ErrorActionPreference = $old }
  if ($LASTEXITCODE -ne 0) { Die "Download of $name failed (curl exit code $LASTEXITCODE). Run install.ps1 again to resume it." }
  if (-not (Test-Sha256 $part $sha256)) {
    Remove-Item -Force $part
    Die "Checksum of $name does not match; the download was removed. Run install.ps1 again."
  }
  Move-Item -Force $part $dest
  return $dest
}
function Fetch($url, $name, $sha256) { return Get-File $url (Join-Path $Downloads $name) $sha256 }
function Get-HfUrl($repo, $revision, $path) { return "https://huggingface.co/$repo/resolve/$revision/$path" }

function Confirm-Choice($mode, $question, $consequence) {
  if ($mode -eq 'yes') { return $true }
  if ($mode -eq 'no') { return $false }
  if (-not $Interactive) {
    Warn "Skipped without asking (non-interactive session). $consequence"
    return $false
  }
  Write-Host ''
  Write-Host $question -ForegroundColor White
  Write-Host $consequence
  while ($true) {
    $answer = (Read-Host 'Proceed? [y/N]').Trim().ToLower()
    if ($answer -in @('y', 'yes')) { return $true }
    if ($answer -in @('', 'n', 'no')) { return $false }
  }
}

function Get-FreeBytes($path) {
  $drive = (Resolve-Path $path).Drive
  return [double](Get-PSDrive $drive.Name).Free
}

# ------------------------------------------------------------------ 0 Checks
Say 'Checking the system'
foreach ($path in @($Package, $ClaudeHome)) {
  if ($path -match ' ') { Die "Paths with spaces are not supported (the hook commands use plain paths): $path" }
}
if (-not (Test-Path (Join-Path $LLcad 'package.json'))) { Die "LLcad source not found in $LLcad" }
if (-not [Environment]::Is64BitOperatingSystem) { Die 'Only 64-bit Windows (x86-64) is supported.' }
# Use the programs that ship with Windows: Git for Windows can put GNU tar and other builds of
# curl on the PATH, and GNU tar reads 'C:\...' as a remote host.
$CurlExe = Join-Path $env:WINDIR 'System32\curl.exe'
$TarExe = Join-Path $env:WINDIR 'System32\tar.exe'
foreach ($exe in @($CurlExe, $TarExe)) {
  if (-not (Test-Path $exe)) { Die "$exe not found; it ships with Windows 10 version 1803 and later." }
}
$CurlVersion = [version]((((& $CurlExe --version) | Select-Object -First 1) -split ' ')[1] -replace '[^0-9.].*$', '')
$CurlRetry = @('--retry', '8', '--retry-delay', '5', '--connect-timeout', '30')
if ($CurlVersion -ge [version]'7.71.0') { $CurlRetry += '--retry-all-errors' }

if (-not $SkipBuild -and (Get-FreeBytes $Package) -lt 6GB) { Die 'At least 6 GB of free disk space are needed for tools and build.' }
Info "Windows $([Environment]::OSVersion.Version), PowerShell $($PSVersionTable.PSVersion), curl $CurlVersion"
Info "Package folder: $Package"

# ------------------------------------------------------------------ 1 Tools
Say 'Tools (Node 24, Python 3.13 via uv, Zig, CGAL, Boost, GMP/MPFR, fonts)'
$Pins = (Get-Content -Raw -Encoding UTF8 (Join-Path $LLcad 'deployment\native-packages.json') | ConvertFrom-Json).windows

if (-not (Test-Path $Node)) {
  $zip = Fetch 'https://nodejs.org/dist/v24.19.0/node-v24.19.0-win-x64.zip' 'node-v24.19.0-win-x64.zip' '57f71ab3652e797d84acddc79c81cc9ff1c6ddb2a1974cdb83f00fee9bff4c73'
  Expand-Archive $zip -DestinationPath $Tools -Force
  Move-Item (Join-Path $Tools 'node-v24.19.0-win-x64') $NodeDir
}
Info "Node $(& $Node -v)"

$Uv = Join-Path $Tools 'uv\uv.exe'
if (-not (Test-Path $Uv)) {
  $zip = Fetch 'https://github.com/astral-sh/uv/releases/download/0.12.17/uv-x86_64-pc-windows-msvc.zip' 'uv-0.12.17-x86_64-pc-windows-msvc.zip' 'a252121d5b59398fcb137c6ea448176459a44010f33f67e0072305a637119ca7'
  Expand-Archive $zip -DestinationPath (Join-Path $Tools 'uv') -Force
}
Info "$(& $Uv --version)"

$Zig = Join-Path $Tools 'zigenv\Scripts\python.exe'
if (-not (Test-Path $Zig)) {
  Run $Uv venv --python 3.13 (Join-Path $Tools 'zigenv')
  Run $Uv pip install --python $Zig "ziglang==$($Pins.zig)"
}

$archives = $Pins.archives
if (-not (Test-Path (Join-Path $Native "CGAL-$($Pins.cgal)\include\CGAL\version.h"))) {
  $name = "CGAL-$($Pins.cgal)-library.zip"
  $zip = Fetch "https://github.com/CGAL/cgal/releases/download/v$($Pins.cgal)/$name" $name $archives.$name
  Expand-Archive $zip -DestinationPath $Native -Force
}
if (-not (Test-Path (Join-Path $Native 'gmp-mpfr\auxiliary\gmp\include\gmp.h'))) {
  $name = "CGAL-$($Pins.cgal)-win64-auxiliary-libraries-gmp-mpfr.zip"
  $zip = Fetch "https://github.com/CGAL/cgal/releases/download/v$($Pins.cgal)/$name" $name $archives.$name
  Expand-Archive $zip -DestinationPath (Join-Path $Native 'gmp-mpfr') -Force
}
if (-not (Test-Path (Join-Path $Native 'boost_1_83_0\boost\version.hpp'))) {
  $tar = Fetch 'https://archives.boost.io/release/1.83.0/source/boost_1_83_0.tar.bz2' 'boost_1_83_0.tar.bz2' $archives.'boost_1_83_0.tar.bz2'
  Info 'unpacking the Boost headers'
  Run $TarExe -xjf $tar -C $Native 'boost_1_83_0/boost'
}

# Fonts that Linux distributions ship as packages (DejaVu Sans is the default text font).
if (-not (Test-Path (Join-Path $Fonts 'DejaVuSans.ttf'))) {
  $zip = Fetch 'https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.zip' 'dejavu-fonts-ttf-2.37.zip' '7576310b219e04159d35ff61dd4a4ec4cdba4f35c00e002a136f00e96a908b0a'
  $tmp = Join-Path $Downloads 'dejavu'
  Expand-Archive $zip -DestinationPath $tmp -Force
  Copy-Item (Join-Path $tmp 'dejavu-fonts-ttf-2.37\ttf\DejaVuSans*.ttf') $Fonts
  Remove-Item -Recurse -Force $tmp
}
if (-not (Test-Path (Join-Path $Fonts 'LiberationSans-Regular.ttf'))) {
  $tar = Fetch 'https://github.com/liberationfonts/liberation-fonts/files/7261482/liberation-fonts-ttf-2.1.5.tar.gz' 'liberation-fonts-ttf-2.1.5.tar.gz' '7191c669bf38899f73a2094ed00f7b800553364f90e2637010a69c0e268f25d0'
  Run $TarExe -xzf $tar -C $Downloads
  Copy-Item (Join-Path $Downloads 'liberation-fonts-ttf-2.1.5\LiberationSans-*.ttf') $Fonts
}
if (-not (Test-Path (Join-Path $Fonts 'LiberationSansNarrow-Regular.ttf'))) {
  $tar = Fetch 'https://github.com/liberationfonts/liberation-sans-narrow/releases/download/1.07.5/liberation-narrow-fonts-ttf-1.07.5.tar.gz' 'liberation-narrow-fonts-ttf-1.07.5.tar.gz' '64948564a34858912b746abc7aa239233c0083115c9386c2079ae92764040b22'
  Run $TarExe -xzf $tar -C $Downloads
  Copy-Item (Join-Path $Downloads 'liberation-narrow-fonts-ttf-1.07.5\LiberationSansNarrow-*.ttf') $Fonts
}

# ------------------------------------------------------------------ 2 Build
$env:Path = "$NodeDir;$env:Path"
Push-Location $LLcad
try {
  if (-not $SkipBuild) {
    # A running server holds files of the old build; stop it before rebuilding.
    if (Test-Path 'dist\packages\mcp-gateway\main.js') {
      $old = $ErrorActionPreference
      $ErrorActionPreference = 'Continue'
      try { & $Node $Bridge stop | Out-Null } finally { $ErrorActionPreference = $old }
    }
    Say 'Building LLcad (Python venv with Open CASCADE, drawing tools, npm ci, schemas, native mesh checker, TypeScript, viewer)'
    if (-not (Test-Path $Py)) { Run $Uv venv --python 3.13 (Join-Path $LLcad '.venv') }
    Run $Uv pip install --python $Py --only-binary ':all:' -r requirements.lock
    # Drawing tools (PDF rasterizer and OCR for the legibility check); not part of the build hash.
    Run $Uv pip install --python $Py --only-binary ':all:' -r requirements-tools.lock
    # python3 for viewer agent runs (their prompts call `python3 -c`).
    Copy-Item -Force $Py (Join-Path $LLcad '.venv\Scripts\python3.exe')
    Run npm.cmd ci --no-audit --no-fund
    Run npm.cmd run schemas
    $env:LLCAD_NATIVE_TOOLS = $Native
    Run $Py scripts\build-native.py
    Run npx.cmd tsc
    Run npx.cmd esbuild packages/viewer/app.ts --bundle --format=esm --minify --outfile=public/app.js
    Say 'Chromium for viewer captures and reference review (Playwright)'
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & npx.cmd playwright install chromium } finally { $ErrorActionPreference = $old }
    if ($LASTEXITCODE -ne 0) { Warn 'Chromium could not be installed; later run: npx playwright install chromium (in LLcad\)' }
  } elseif (-not (Test-Path 'dist\packages\mcp-gateway\main.js')) {
    Die 'No build present; run install.ps1 without -SkipBuild.'
  }
} finally { Pop-Location }

# ------------------------------------------------------------------ 3 Access token and environment
Say 'Local access token and server environment'
$portBusy = $false
try { $null = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "$Url/healthz"; $portBusy = $true } catch { }
if ($portBusy -and -not (Test-Path (Join-Path $Data '.store.owner'))) {
  Die "Port $Port is already used by another program or LLcad installation. Stop it or choose another port with -Port."
}
foreach ($dir in @($Data, (Join-Path $Data 'uploads'), (Join-Path $Data 'annotations'))) { New-Item -ItemType Directory -Force $dir | Out-Null }
# Windows has no POSIX permissions: an ACL restricts the data folder to this user (and SYSTEM).
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls.exe $Data /inheritance:r /grant:r "*${sid}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' /Q | Out-Null
if ($LASTEXITCODE -ne 0) { Warn 'The permissions of the data folder could not be restricted.' }
$TokenFile = Join-Path $Data 'local-token'
if (-not (Test-Path $TokenFile) -or (Get-Item $TokenFile).Length -eq 0) {
  $bytes = New-Object byte[] 32
  [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $token = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
  [IO.File]::WriteAllText($TokenFile, $token)
  Info "new access token: $TokenFile"
} else { Info "existing access token kept: $TokenFile" }

$ClaudeCli = (Get-Command claude -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
$NativeClaude = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
if (-not $ClaudeCli -and (Test-Path $NativeClaude)) { $ClaudeCli = $NativeClaude }
if (-not $ClaudeCli -and -not $NoClaude) {
  $install = Confirm-Choice $ClaudeCode 'Claude Code was not found. Install it now with the official installer (no administrator rights needed)?' `
    'Without Claude Code, LLcad still works with other MCP clients, but the skill and the MCP entry cannot be set up. Install it later from https://code.claude.com and run install.ps1 -SkipBuild again.'
  if ($install) {
    Say 'Installing Claude Code (official installer)'
    Run powershell.exe -NoProfile -ExecutionPolicy Bypass -Command 'irm https://claude.ai/install.ps1 | iex'
    if (Test-Path $NativeClaude) { $ClaudeCli = $NativeClaude } else { Warn 'Claude Code was installed, but claude.exe was not found in the expected place.' }
  }
}

$OcrDir = Join-Path $Models 'rapidocr'
$EnvFile = Join-Path $Package 'llcad.env'
$envLines = @(
  '# Environment of the LLcad server (written by install.ps1). The bridge LLcad\bin\llcad-bridge.mjs reads it.',
  "MATHFORGE_ROOT=$LLcad",
  "MATHFORGE_DATA=$Data",
  'MATHFORGE_AUTH=local',
  'HOST=127.0.0.1',
  "PORT=$Port",
  "MATHFORGE_PUBLIC_URL=$Url",
  '# Folders that patch_ref files and SVG logos may be read from (separated by semicolons).',
  "LLCAD_PATCH_PATHS=$Projects;$Data\uploads;$Data\annotations",
  "LLCAD_FONT_DIRS=$Fonts",
  "LLCAD_IMAGE_PYTHON=$Py",
  '# OCR models of the drawing legibility check.',
  "LLCAD_RAPIDOCR_DIR=$OcrDir",
  '# Jev (effort routing for cad_feature_batch) is a separate tool and not installed here.',
  'LLCAD_FEATURE_JEV=0',
  '# Idle: no pre-warmed workers, no preview rendering at start, exit after N minutes without requests.',
  'MATHFORGE_WARM_WORKERS=0',
  'LLCAD_WARM_PREVIEWS=0',
  "LLCAD_IDLE_EXIT_MINUTES=$IdleMinutes",
  'PYTHONUTF8=1'
)
if ($ClaudeCli) { $envLines += "MATHFORGE_CLAUDE_CLI=$ClaudeCli" }
$envLines += @(
  '# Optional, judge through OpenRouter instead of the Claude CLI (file that holds the API key):',
  '# LLCAD_OPENROUTER_KEY_FILE=C:\path\to\openrouter.key',
  '# Parallel evaluation of independent subtrees (LLCAD_PARALLEL_EVAL) is not available on Windows (no fork).'
)
[IO.File]::WriteAllLines($EnvFile, $envLines, (New-Object Text.UTF8Encoding $false))

$Cmd = Join-Path $Package 'llcad.cmd'
[IO.File]::WriteAllLines($Cmd, @(
    '@echo off',
    'rem LLcad on demand: llcad viewer [model-id] ^| start ^| status ^| stop',
    "`"$Node`" `"$Bridge`" %*"
  ), (New-Object Text.UTF8Encoding $false))
Info "control: $Cmd viewer|start|status|stop"

if (-not $NoShortcut) {
  # Start menu: start the viewer on click only.
  $StartMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
  $shell = New-Object -ComObject WScript.Shell
  $lnk = $shell.CreateShortcut((Join-Path $StartMenu 'LLcad Viewer.lnk'))
  $lnk.TargetPath = $Cmd
  $lnk.Arguments = 'viewer'
  $lnk.WorkingDirectory = $Package
  $lnk.WindowStyle = 7
  $lnk.Description = 'Start LLcad if needed and open the viewer in the browser'
  $lnk.Save()
}

# ------------------------------------------------------------------ 4 Drawing generation (work in progress)
Say 'Drawing generation (work in progress): OCR models and optional local vision model'
$Draw = Get-Content -Raw -Encoding UTF8 (Join-Path $LLcad 'deployment\drawing-models.json') | ConvertFrom-Json
foreach ($f in $Draw.ocr.files) {
  $null = Get-File (Get-HfUrl $Draw.ocr.repo $Draw.ocr.revision $f.path) (Join-Path $OcrDir $f.name) $f.sha256 $f.size
}
Info "OCR models: $OcrDir"

$vm = $Draw.vision_model
$VmDir = Join-Path $Models $vm.dir
$vmTotal = [double]0
$vmMissing = [double]0
foreach ($f in $vm.files) {
  $vmTotal += [double]$f.size
  $file = Join-Path $VmDir $f.name
  if (-not ((Test-Path $file) -and (Get-Item $file).Length -eq [double]$f.size)) { $vmMissing += [double]$f.size }
}
$LlamaBuild = if (Test-Path (Join-Path $env:WINDIR 'System32\vulkan-1.dll')) { 'vulkan' } else { 'cpu' }
$LlamaDir = Join-Path $Tools "llama.cpp\$($Draw.llama_cpp.version)-$LlamaBuild"
$LlamaServer = Join-Path $LlamaDir 'llama-server.exe'
$ModelReady = ($vmMissing -eq 0) -and (Test-Path $LlamaServer)

if ($ModelReady) {
  Info "local vision model present: $VmDir"
} else {
  $ramGb = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
  $needGb = [math]::Round(($vmMissing + [double]$Draw.llama_cpp.builds.$LlamaBuild.size) / 1GB, 1)
  $ramNote = if ($ramGb -lt $vm.server.min_total_ram_gb) { " This computer has $ramGb GB of RAM, so the model will most likely not start." } else { '' }
  $question = "Download the local vision model for drawing generation? $($vm.description); $needGb GB from Hugging Face ($($vm.repo), license $($vm.license)) plus llama.cpp $($Draw.llama_cpp.version) ($LlamaBuild build, $($Draw.llama_cpp.license)). It needs about 18 GB of free RAM while it runs.$ramNote"
  $consequence = 'Without it, everything else works: the MCP server, modelling, rendering, reference review and the rule-based drawing generator (cad_drawing_* tools). Drawing generation is work in progress; the current version does not call the local model yet, later versions will. You can download it at any time with: install.ps1 -SkipBuild -DrawingModel yes'
  if (Confirm-Choice $DrawingModel $question $consequence) {
    $free = Get-FreeBytes $Package
    if ($free -lt $vmMissing + 5GB) {
      Warn ('Not enough free disk space: {0:N1} GB needed, {1:N1} GB free. The vision model was not downloaded.' -f (($vmMissing + 5GB) / 1GB), ($free / 1GB))
    } else {
      $b = $Draw.llama_cpp.builds.$LlamaBuild
      if (-not (Test-Path $LlamaServer)) {
        $zip = Fetch $b.url (Split-Path $b.url -Leaf) $b.sha256
        Expand-Archive $zip -DestinationPath $LlamaDir -Force
      }
      Run $LlamaServer --version
      foreach ($f in $vm.files) {
        $null = Get-File (Get-HfUrl $vm.repo $vm.revision $f.path) (Join-Path $VmDir $f.name) $f.sha256 $f.size
      }
      $ModelReady = $true
      Info "local vision model: $VmDir"
    }
  }
}

# Drawing configuration: written once, the user edits it afterwards (company lines, defaults,
# semi-finished products, keyword lists). Only the local model profile is added later.
$env:LLCAD_DRAWING_CONFIG = Join-Path $Data 'drawing-config.json'
$env:LLCAD_MODEL_PROFILE = ''
if ($ModelReady) {
  $model = ($vm.files | Where-Object { $_.role -eq 'model' }).name
  $mmproj = ($vm.files | Where-Object { $_.role -eq 'mmproj' }).name
  $env:LLCAD_MODEL_PROFILE = (@{
      name = $vm.profile; server = (Fwd $LlamaServer); model = (Fwd (Join-Path $VmDir $model)); mmproj = (Fwd (Join-Path $VmDir $mmproj))
      port = $vm.server.port; ctx = $vm.server.ctx; args = @($vm.server.args); reuse_ports = @()
    } | ConvertTo-Json -Compress)
}
@'
import json, os, pathlib
p = pathlib.Path(os.environ["LLCAD_DRAWING_CONFIG"])
existed = p.exists()
d = json.loads(p.read_text(encoding="utf-8")) if existed else {
    "company": [],
    "title_defaults": {"general_tolerance": "ISO 2768-m", "tolerancing": "ISO 8015"},
    "semi_finished": [],
    "part_roles": {"purchased": [], "standard": []},
}
changed = not existed
prof = os.environ.get("LLCAD_MODEL_PROFILE")
if prof:
    prof = json.loads(prof)
    name = prof.pop("name")
    lm = d.setdefault("local_model", {})
    profiles = lm.setdefault("profiles", {})
    if profiles.get(name) != prof:
        profiles[name] = prof
        changed = True
    if not lm.get("active"):
        lm["active"] = name
        changed = True
if changed:
    p.write_text(json.dumps(d, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
print("    drawing configuration: %s (%s)" % (p, "written" if changed else "unchanged"))
'@ | & $Py -X utf8 -
if ($LASTEXITCODE -ne 0) { Die 'The drawing configuration could not be written.' }

if ($NoClaude) { Say 'Done (without Claude Code configuration)'; exit 0 }

# ------------------------------------------------------------------ 5 Skill and hooks
Say "Skill llcad-modelling (with hooks) in $SkillDir"
if (Test-Path $SkillDir) {
  # Back up outside skills\, otherwise Claude Code loads the copy as a second skill.
  $backupRoot = Join-Path $ClaudeHome 'backups'
  New-Item -ItemType Directory -Force $backupRoot | Out-Null
  $backup = Join-Path $backupRoot "llcad-modelling.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
  Move-Item $SkillDir $backup
  Warn "existing skill moved to $backup"
}
New-Item -ItemType Directory -Force (Split-Path $SkillDir) | Out-Null
Copy-Item -Recurse (Join-Path $Package 'claude\skills\llcad-modelling') $SkillDir
$env:SKILL_DIR = Fwd $SkillDir; $env:LLCAD_DIR = Fwd $LLcad; $env:DATA_DIR = Fwd $Data
$env:PROJECTS_DIR = Fwd $Projects; $env:LLCAD_URL_NEW = $Url; $env:PY_FWD = Fwd $Py
@'
import os, pathlib, re
skill = pathlib.Path(os.environ["SKILL_DIR"])
py = os.environ["PY_FWD"] + " -X utf8"
# Longer paths first; forward slashes keep Python and JSON literals valid.
pairs = [
    # "~" is not necessarily the user profile in Git Bash (HOME can be a network drive).
    ("~/.claude/skills/llcad-modelling", os.environ["SKILL_DIR"]),
    ("/root/.claude/skills/llcad-modelling", os.environ["SKILL_DIR"]),
    ("/root/projects/LLcad/data/local-token", os.environ["DATA_DIR"] + "/local-token"),
    ("/root/projects/LLcad", os.environ["LLCAD_DIR"]),
    ('"/root/projects"', '"%s"' % os.environ["PROJECTS_DIR"]),
    ("http://127.0.0.1:4310", os.environ["LLCAD_URL_NEW"]),
]
hook = re.compile(r'python3 "\$\{CLAUDE_PLUGIN_ROOT:-[^}]*\}/scripts/([A-Za-z_]+\.py)"')
changed = 0
for f in list(skill.rglob("*.py")) + list(skill.rglob("*.json")) + list(skill.rglob("*.md")):
    text = f.read_text(encoding="utf-8")
    new = text
    for old, rep in pairs:
        new = new.replace(old, rep)
    if f.suffix != ".py":
        # Hook commands: absolute paths without shell expansion (they run in Git Bash, cmd and PowerShell).
        new = hook.sub(lambda m: "%s %s/scripts/%s" % (py, os.environ["SKILL_DIR"], m.group(1)), new)
        # On Windows python3 is only the Store placeholder: use the package venv.
        new = re.sub(r"(?<![\w/.-])python3(?= )", py, new)
    if new != text:
        f.write_text(new, encoding="utf-8", newline="\n")
        changed += 1
print("    paths adjusted in %d files" % changed)
'@ | & $Py -X utf8 -
if ($LASTEXITCODE -ne 0) { Die 'The skill paths could not be adjusted.' }

# ------------------------------------------------------------------ 6 Memory (project rule)
$enc = $LLcad -replace '[^A-Za-z0-9]', '-'
$MemDir = Join-Path $ClaudeHome "projects\$enc\memory"
New-Item -ItemType Directory -Force $MemDir | Out-Null
Copy-Item -Force (Join-Path $Package 'claude\memory\fillet-fine-polygon-precision.md') $MemDir
$memIndex = Join-Path $MemDir 'MEMORY.md'
if (-not ((Test-Path $memIndex) -and (Select-String -Quiet -SimpleMatch 'fillet-fine-polygon-precision.md' $memIndex))) {
  [IO.File]::AppendAllText($memIndex, "- [Fillet on fine polygons fails](fillet-fine-polygon-precision.md) - llcad fillet on profiles segmented finer than 22.5 degrees gives PRECISION_UNSUPPORTED; 8 segments per semicircle work`n", (New-Object Text.UTF8Encoding $false))
}
Info "memory: $MemDir"

# ------------------------------------------------------------------ 7 Claude Code: permission and MCP entry
Say 'Configuring Claude Code'
$Settings = Join-Path $ClaudeHome 'settings.json'
New-Item -ItemType Directory -Force $ClaudeHome | Out-Null
if (Test-Path $Settings) { Copy-Item $Settings "$Settings.bak-$(Get-Date -Format yyyyMMdd-HHmmss)" }
$env:SETTINGS_FILE = $Settings
@'
import json, os, pathlib
p = pathlib.Path(os.environ["SETTINGS_FILE"])
d = json.loads(p.read_text(encoding="utf-8")) if p.exists() and p.read_text(encoding="utf-8").strip() else {}
allow = d.setdefault("permissions", {}).setdefault("allow", [])
if "mcp__llcad" not in allow:
    allow.append("mcp__llcad")
p.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print("    permission mcp__llcad in", p)
'@ | & $Py -X utf8 -
if ($LASTEXITCODE -ne 0) { Die 'The Claude Code settings could not be updated.' }

$McpOk = $false
if ($ClaudeCli) {
  $old = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    & $ClaudeCli mcp remove llcad -s user *> $null
    # '--' in quotes: unquoted, PowerShell would swallow it as the end of its own parameters.
    & $ClaudeCli mcp add --scope user llcad '--' $Node $Bridge
  } finally { $ErrorActionPreference = $old }
  if ($LASTEXITCODE -ne 0) { Die 'claude mcp add failed.' }
  Info "MCP server llcad registered (user scope, stdio bridge to $Url on demand)"
} else {
  Warn "Claude Code not found. Add the MCP entry later: claude mcp add --scope user llcad -- `"$Node`" `"$Bridge`""
}

# ------------------------------------------------------------------ 8 Self-test
Say 'Self-test'
$old = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
  & $Node $Bridge start
  $started = $LASTEXITCODE -eq 0
  if ($ClaudeCli) {
    $mcp = (& $ClaudeCli mcp get llcad 2>&1 | Out-String)
    $McpOk = $mcp -cmatch '\bConnected\b'
  }
  & $Node $Bridge stop | Out-Null
} finally { $ErrorActionPreference = $old }
if (-not $started) { Die "The LLcad server did not start; see $Data\logs\server.log" }
Info 'server: starts and answers'
if ($ClaudeCli) {
  if ($McpOk) { Info 'Claude Code: MCP server llcad connected' } else { Warn "Claude Code does not report llcad as connected yet; check with: claude mcp get llcad" }
}

Say 'Done'
$modelLine = if ($ModelReady) { "installed ($VmDir)" } else { 'not installed (install.ps1 -SkipBuild -DrawingModel yes)' }
Write-Host @"
Idle:          no service, no autostart. Claude Code only starts the small bridge; the server starts
               on the first LLcad tool call and exits after $IdleMinutes min without requests.
Viewer:        on request only: Start menu 'LLcad Viewer', '$Cmd viewer' or ask Claude to open the viewer.
Status/stop:   $Cmd status | $Cmd stop
Drawings:      work in progress. Company lines and defaults: $Data\drawing-config.json
Vision model:  $modelLine
Claude Code:   restart it; /mcp must show llcad as connected.
First prompt:  "Create a 90 x 60 mm circuit board, 1.6 mm thick, with four M3 holes 4 mm from the corners, then open the viewer."
"@
