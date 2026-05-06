# build.ps1 - Windows x64 本机构建脚本
#
# 两步构建:
#   1. 在 llama.cpp 目录下构建并 install → llama.cpp/install/x64/
#   2. qwen_chat 通过 find_package 链接预构建的 llama.cpp
#
# 用法:
#   .\build.ps1                           # 构建全部
#   .\build.ps1 -Target llama             # 只构建 llama.cpp
#   .\build.ps1 -Target app               # 只构建 qwen_chat (需先构建 llama)
#   .\build.ps1 -Clean                    # 清除后重新构建
#   .\build.ps1 -CudaOn                   # 启用 CUDA
#   .\build.ps1 -LlamaCppDir D:\llama.cpp  # 指定 llama.cpp 路径

param(
    [ValidateSet("all", "llama", "app")]
    [string]$Target = "all",

    [string]$LlamaCppDir = "",
    [switch]$Clean,
    [switch]$CudaOn
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# 自动检测 LLAMA_CPP_DIR
if (-not $LlamaCppDir) {
    if ($env:LLAMA_CPP_DIR) {
        $LlamaCppDir = $env:LLAMA_CPP_DIR
    } else {
        $DefaultDir = Join-Path (Split-Path $ScriptDir) "llama.cpp"
        if (Test-Path $DefaultDir) {
            $LlamaCppDir = $DefaultDir
        } else {
            Write-Error "请设置 LLAMA_CPP_DIR 环境变量或传入 -LlamaCppDir 参数"
            exit 1
        }
    }
}

$LlamaBuildDir   = Join-Path $LlamaCppDir "build\x64"
$LlamaInstallDir = Join-Path $LlamaCppDir "install\x64"
$AppBuildDir     = Join-Path $ScriptDir   "build\x64"

# ============================================================
# Step 1: 构建 llama.cpp 并 install
#   源码:   llama.cpp/
#   构建:   llama.cpp/build/x64/
#   安装:   llama.cpp/install/x64/  (headers + libs + bins)
# ============================================================
function Build-LlamaCpp {
    Write-Host "`n====================================" -ForegroundColor Cyan
    Write-Host "  [Step 1] 构建 llama.cpp (x64)" -ForegroundColor Cyan
    Write-Host "  源码:   $LlamaCppDir" -ForegroundColor Cyan
    Write-Host "  构建:   $LlamaBuildDir" -ForegroundColor Cyan
    Write-Host "  安装:   $LlamaInstallDir" -ForegroundColor Cyan
    Write-Host "  CUDA:   $(if ($CudaOn) { 'ON' } else { 'OFF' })" -ForegroundColor Cyan
    Write-Host "====================================" -ForegroundColor Cyan

    if ($Clean) {
        if (Test-Path $LlamaBuildDir)   { Remove-Item -Recurse -Force $LlamaBuildDir }
        if (Test-Path $LlamaInstallDir) { Remove-Item -Recurse -Force $LlamaInstallDir }
    }

    # configure
    $cmakeArgs = @(
        "-B", $LlamaBuildDir,
        "-S", $LlamaCppDir,
        "-A", "x64",
        "-DCMAKE_INSTALL_PREFIX=$LlamaInstallDir",
        "-DLLAMA_FLASH_ATTN=ON"
    )
    if ($CudaOn) { $cmakeArgs += "-DGGML_CUDA=ON" }

    Write-Host "`n[cmake configure]" -ForegroundColor Green
    & cmake @cmakeArgs
    if ($LASTEXITCODE -ne 0) { throw "llama.cpp configure 失败" }

    # build
    Write-Host "`n[cmake build]" -ForegroundColor Green
    & cmake --build $LlamaBuildDir --config Release -- /m
    if ($LASTEXITCODE -ne 0) { throw "llama.cpp build 失败" }

    # install
    Write-Host "`n[cmake install]" -ForegroundColor Green
    & cmake --install $LlamaBuildDir --config Release
    if ($LASTEXITCODE -ne 0) { throw "llama.cpp install 失败" }

    Write-Host "`n[OK] llama.cpp 构建 + 安装完成!" -ForegroundColor Green
    Write-Host "  安装目录: $LlamaInstallDir" -ForegroundColor Green
}

# ============================================================
# Step 2: 构建 qwen_chat
#   链接 llama.cpp/install/x64/ 的预构建库
#   产物:   cpp/build/x64/
# ============================================================
function Build-App {
    if (-not (Test-Path (Join-Path $LlamaInstallDir "lib\cmake\llama"))) {
        Write-Error "llama.cpp 尚未构建/安装, 请先运行: .\build.ps1 -Target llama"
        exit 1
    }

    Write-Host "`n====================================" -ForegroundColor Cyan
    Write-Host "  [Step 2] 构建 qwen_chat (x64)" -ForegroundColor Cyan
    Write-Host "  源码:   $ScriptDir" -ForegroundColor Cyan
    Write-Host "  构建:   $AppBuildDir" -ForegroundColor Cyan
    Write-Host "  链接:   $LlamaInstallDir" -ForegroundColor Cyan
    Write-Host "====================================" -ForegroundColor Cyan

    if ($Clean -and (Test-Path $AppBuildDir)) {
        Remove-Item -Recurse -Force $AppBuildDir
    }
    $AppInstallDir = Join-Path $ScriptDir "install\x64"
    if ($Clean -and (Test-Path $AppInstallDir)) {
        Remove-Item -Recurse -Force $AppInstallDir
    }

    $cmakeArgs = @(
        "-B", $AppBuildDir,
        "-S", $ScriptDir,
        "-DLLAMA_INSTALL_DIR=$LlamaInstallDir",
        "-DTARGET_PLATFORM=x64",
        "-A", "x64"
    )

    Write-Host "`n[cmake configure]" -ForegroundColor Green
    & cmake @cmakeArgs
    if ($LASTEXITCODE -ne 0) { throw "qwen_chat configure 失败" }

    Write-Host "`n[cmake build]" -ForegroundColor Green
    & cmake --build $AppBuildDir --config Release -- /m
    if ($LASTEXITCODE -ne 0) { throw "qwen_chat build 失败" }

    # install: 可执行文件 + 依赖 DLL → install/x64/bin/
    $AppInstallDir = Join-Path $ScriptDir "install\x64"
    Write-Host "`n[cmake install]" -ForegroundColor Green
    & cmake --install $AppBuildDir --config Release --prefix $AppInstallDir
    if ($LASTEXITCODE -ne 0) { throw "qwen_chat install 失败" }

    Write-Host "`n[OK] qwen_chat 构建完成!" -ForegroundColor Green
    Write-Host "  产物: $AppInstallDir\bin\qwen_chat.exe (含依赖 DLL)" -ForegroundColor Green
}

# ---- 执行 ----
switch ($Target) {
    "all"   { Build-LlamaCpp; Build-App }
    "llama" { Build-LlamaCpp }
    "app"   { Build-App }
}
