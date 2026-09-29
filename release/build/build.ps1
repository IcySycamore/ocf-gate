# Build the release: one artifact, the installer .exe.
#
#     .\release\build\build.ps1
#
# `ocf.py package` decides what is in the release - the payload plus release\payload - so this script
# only decides the format. It cleans dist\ocf-gate-<version> before packaging, deliberately: the
# packager writes files and never deletes anything, so the one place that removes a tree is the one
# place that owns the output directory.
#
# The staged tree is left in place rather than compressed: it is what the installer packs, and it is
# also the only form in which the payload can be deployed and checked without running the .exe -
# `python <tree>\.github\ocf\ocf.py install <repo>` is the same deployment the installer performs.
# There is deliberately no .zip: one artifact means one thing to hand somebody, and a second one that
# is only ever "the same thing, unpacked" is a second thing to keep in step.

[CmdletBinding()]
param([string]$Python = 'python')

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$version = (Get-Content (Join-Path $root 'VERSION') -Raw).Trim()
$dist = Join-Path $root 'dist'
$stage = Join-Path $dist "ocf-gate-$version"
$setup = Join-Path $dist "ocf-gate-$version-setup.exe"

if (-not $version) { throw "VERSION is empty, so the release has no name" }
if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
if (Test-Path -LiteralPath $setup) { Remove-Item -LiteralPath $setup -Force }

Push-Location $root
try {
    # Both suites have to pass before anything is packaged. The first is the one the person who deploys
    # this gate will run; the second is the engine's own, which is deliberately not shipped and so has
    # this as its only chance to stop a release. The build used to run no checks at all.
    & $Python '.github\ocf\tests\run.py'
    if ($LASTEXITCODE -ne 0) { throw "the shipped suite failed, so nothing was packaged" }
    & $Python 'release\build\engine_checks.py'
    if ($LASTEXITCODE -ne 0) { throw "the engine's own checks failed, so nothing was packaged" }

    & $Python '.github\ocf\ocf.py' package $dist
    if ($LASTEXITCODE -ne 0) { throw "packaging failed with exit code $LASTEXITCODE" }

    $iscc = @("$env:ProgramFiles\Inno Setup 6\ISCC.exe",
              "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") |
            Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $iscc) {
        $hint = 'Inno Setup 6 was not found, so the release would be nothing at all. Install it from jrsoftware.org, or deploy the staged tree by hand: it is in '
        throw ($hint + $stage)
    }
    & $iscc "/DPayloadDir=$stage" "/DAppVersion=$version" (Join-Path $PSScriptRoot 'ocf-gate.iss')
    if ($LASTEXITCODE -ne 0) { throw "ISCC failed with exit code $LASTEXITCODE" }

    Write-Host ''
    Write-Host "release    $setup"
    Write-Host "payload    $stage  (what it packs; that tree holds the whole gate)"
} finally {
    Pop-Location
}
