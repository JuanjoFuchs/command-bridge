#
# command-bridge installer for Windows (PowerShell).
#
# There is no PyPI, npm or WinGet release yet, so this installs from the Git repository: it finds
# Python, clones (or updates) the repo, builds a private virtualenv beside it, installs
# command-bridge with all of its neural engines, and prints how to run it. Nothing touches the
# system Python or any active environment -- everything lives under the checkout and is removed by
# deleting that one directory.
#
#   Pipe it:       irm https://raw.githubusercontent.com/JuanjoFuchs/command-bridge/main/install.ps1 | iex
#   From a clone:  .\install.ps1
#
# Override with environment variables:
#   $env:CB_DIR  = '<path>'   where to clone       (default: $HOME\command-bridge)
#   $env:CB_REPO = '<url>'    which remote to use  (default: the GitHub repo)
#
$ErrorActionPreference = 'Stop'

$RepoUrl = if ($env:CB_REPO) { $env:CB_REPO } else { 'https://github.com/JuanjoFuchs/command-bridge.git' }
$Dest    = if ($env:CB_DIR)  { $env:CB_DIR }  else { Join-Path $HOME 'command-bridge' }

# 1. A Python 3.10+ interpreter. `py -3` is the Windows launcher and is often the only thing on
#    PATH; then bare `python`, then `python3`.
function Find-Python {
    $candidates = @(
        @{ Exe = 'py';      Pre = @('-3') },
        @{ Exe = 'python';  Pre = @() },
        @{ Exe = 'python3'; Pre = @() }
    )
    foreach ($c in $candidates) {
        if (Get-Command $c.Exe -ErrorAction SilentlyContinue) {
            $v = & $c.Exe @($c.Pre) -c 'import sys;print("%d.%d" % sys.version_info[:2])' 2>$null
            if ($LASTEXITCODE -eq 0 -and $v) {
                $p = $v.Trim().Split('.')
                if ([int]$p[0] -gt 3 -or ([int]$p[0] -eq 3 -and [int]$p[1] -ge 10)) { return $c }
            }
        }
    }
    return $null
}
$py = Find-Python
if (-not $py) { throw 'command-bridge needs Python 3.10+ on PATH. Install it from https://python.org and re-run.' }

# 2. The repository: reuse this checkout if we are already inside one, update it if it is already
#    cloned at CB_DIR, otherwise clone it fresh.
if ((Test-Path 'pyproject.toml') -and (Select-String -Path 'pyproject.toml' -Pattern 'name = "command-bridge"' -Quiet)) {
    $Dest = (Get-Location).Path
    Write-Host "Using this checkout: $Dest"
} elseif (Test-Path (Join-Path $Dest '.git')) {
    Write-Host "Updating existing checkout: $Dest"
    git -C $Dest pull --ff-only
} else {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'git is required to fetch the repo (or run this from a checkout).' }
    Write-Host "Cloning into: $Dest"
    git clone --depth 1 $RepoUrl $Dest
}

# 3. A private virtualenv beside the checkout. The shim in bin\ looks for exactly venv\, so this is
#    where it must go. `.[all]` pulls the engines (a neural voice, the fast recognizer, the turn
#    model) in one step -- required here because, without a PyPI release, `command-bridge setup`
#    cannot install them later; it can only download the model assets they use.
$Venv = Join-Path $Dest 'venv'
$VPy  = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path $VPy)) { & $py.Exe @($py.Pre) -m venv $Venv }

Write-Host 'Installing command-bridge and its neural engines (native wheels - this takes a minute)...'
& $VPy -m pip install --quiet --upgrade pip
Push-Location $Dest
try { & $VPy -m pip install --quiet -e '.[all]' } finally { Pop-Location }

# 4. Report, and the exact next steps. The models (a neural voice, fast ASR, the voiceprint and the
#    turn model) are a separate ~1 GB download kept behind an explicit command, so the install stays
#    fast and you choose what to pull.
$Bin = Join-Path $Dest 'bin\command-bridge.cmd'
Write-Host ''
Write-Host "  command-bridge is installed at $Dest"
Write-Host ''
Write-Host '  Put it on your PATH for this session:'
Write-Host "      `$env:PATH = `"$Dest\bin;`$env:PATH`""
Write-Host ''
Write-Host '  Then:'
Write-Host '      command-bridge setup                 # download the neural voice, fast ASR, voiceprint, turn model'
Write-Host "      command-bridge serve --wake claude   # start it under YOUR agent's name, and open the URL it prints"
Write-Host ''
Write-Host '  Or run it in place without touching PATH:'
Write-Host "      $Bin doctor"
