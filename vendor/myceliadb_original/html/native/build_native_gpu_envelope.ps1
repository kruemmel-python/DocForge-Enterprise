param(
    [string]$Generator = "",
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Write-Step($msg) {
    Write-Host "[Mycelia Native Build] $msg"
}

function Test-DllX64($Path) {
    if (-not (Test-Path $Path)) { return $false }
    $bytes = [System.IO.File]::ReadAllBytes((Resolve-Path $Path))
    if ($bytes.Length -lt 0x40 -or $bytes[0] -ne 0x4D -or $bytes[1] -ne 0x5A) { return $false }
    $pe = [BitConverter]::ToInt32($bytes, 0x3c)
    if ($pe -le 0 -or ($pe + 6) -ge $bytes.Length) { return $false }
    $machine = [BitConverter]::ToUInt16($bytes, $pe + 4)
    return ($machine -eq 0x8664)
}

if ($Clean) {
    if (Test-Path "build") {
        Write-Step "Bereinige Build-Verzeichnis: build"
        Remove-Item -Recurse -Force "build"
    }
    Remove-Item ".\mycelia_gpu_envelope.dll", ".\*.obj", ".\*.exp", ".\*.lib" -Force -ErrorAction SilentlyContinue
}

$built = $false

# Prefer a deterministic x64 MSVC build because Python is 64-bit and ctypes
# rejects x86 DLLs with WinError 193.
$vcvars64 = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
if (Test-Path $vcvars64) {
    Write-Step "Direktbuild mit MSVC vcvars64 + /MT + /MACHINE:X64"
    $cmd = "call `"$vcvars64`" && cd /d `"$Root`" && cl /nologo /LD /O2 /MT /TC mycelia_gpu_envelope_contract.c /Fe:mycelia_gpu_envelope.dll /link /DLL /MACHINE:X64"
    & cmd /c $cmd
    if ($LASTEXITCODE -ne 0) { throw "MSVC vcvars64 Direktbuild fehlgeschlagen mit ExitCode $LASTEXITCODE" }
    if (-not (Test-DllX64 ".\mycelia_gpu_envelope.dll")) {
        throw "Build erzeugte keine gültige x64 Windows-DLL. Prüfe vcvars64/Compiler."
    }
    $built = $true
}

if (-not $built) {
    $cl = Get-Command cl -ErrorAction SilentlyContinue
    if ($cl) {
        Write-Step "Direktbuild mit aktuellem MSVC cl + /MT + /MACHINE:X64"
        & cl /nologo /LD /O2 /MT /TC mycelia_gpu_envelope_contract.c /Fe:mycelia_gpu_envelope.dll /link /DLL /MACHINE:X64
        if ($LASTEXITCODE -ne 0) { throw "MSVC Direktbuild fehlgeschlagen mit ExitCode $LASTEXITCODE" }
        if (-not (Test-DllX64 ".\mycelia_gpu_envelope.dll")) {
            throw "Build erzeugte keine gültige x64 Windows-DLL. Starte eine x64 Developer PowerShell."
        }
        $built = $true
    }
}

if (-not $built) {
    $cmake = Get-Command cmake -ErrorAction SilentlyContinue
    if ($cmake) {
        try {
            if (-not $Generator -or $Generator.Trim() -eq "") {
                $help = (& cmake --help) -join "`n"
                if ($help -match "Visual Studio 17 2022") {
                    $Generator = "Visual Studio 17 2022"
                } elseif ($help -match "Ninja") {
                    $Generator = "Ninja"
                } else {
                    $Generator = "Unix Makefiles"
                }
            }

            Write-Step "Konfiguriere mit Generator: $Generator"
            if ($Generator -like "Visual Studio*") {
                & cmake -S . -B build -G $Generator -A x64
            } else {
                & cmake -S . -B build -G $Generator
            }
            if ($LASTEXITCODE -ne 0) { throw "cmake fehlgeschlagen mit ExitCode $LASTEXITCODE" }

            Write-Step "Baue Release"
            & cmake --build build --config Release
            if ($LASTEXITCODE -ne 0) { throw "cmake build fehlgeschlagen mit ExitCode $LASTEXITCODE" }

            $candidates = @(
                "build\Release\mycelia_gpu_envelope.dll",
                "build\mycelia_gpu_envelope.dll"
            )
            foreach ($c in $candidates) {
                if (Test-Path $c) {
                    Copy-Item $c ".\mycelia_gpu_envelope.dll" -Force
                    if (-not (Test-DllX64 ".\mycelia_gpu_envelope.dll")) {
                        throw "CMake erzeugte keine gültige x64 Windows-DLL."
                    }
                    $built = $true
                    break
                }
            }
        } catch {
            Write-Warning "CMake-Build fehlgeschlagen: $_"
        }
    }
}

if (-not $built) {
    throw "Build fehlgeschlagen: keine gültige x64 mycelia_gpu_envelope.dll erzeugt."
}

Write-Host "Native GPU Residency DLL installiert: $Root\mycelia_gpu_envelope.dll"
Write-Host "v1.18F: OpenCL VRAM open/restore evidence aktiv, falls OpenCL.dll und ein GPU-Gerät verfügbar sind."

$ManifestTool = Resolve-Path "$Root\..\..\tools\generate_native_hash_manifest.py" -ErrorAction SilentlyContinue
if ($ManifestTool) {
    Write-Host "Aktualisiere Native-Library-Hashmanifest..."
    python $ManifestTool.Path
}
