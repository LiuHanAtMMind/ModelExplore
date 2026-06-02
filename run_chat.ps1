$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Bin = Join-Path $ScriptDir "model_chat\install\x64\bin\model_chat.exe"
$ModelDir = Join-Path $ScriptDir "models"

$models = Get-ChildItem -Path $ModelDir -Filter "*.gguf" -File -Recurse |
    Where-Object { $_.Name -notlike "mmproj*.gguf" } |
    Sort-Object FullName |
    Select-Object -ExpandProperty FullName

if ($models.Count -eq 0) {
    Write-Host "No .gguf models found in $ModelDir"
    exit 1
}

Write-Host "Available models:"
for ($index = 0; $index -lt $models.Count; $index++) {
    Write-Host "  $($index + 1). $($models[$index])"
}

Write-Host ""
$choice = Read-Host "Select model [1-$($models.Count)]"

if ($choice -notmatch '^\d+$' -or [int]$choice -lt 1 -or [int]$choice -gt $models.Count) {
    Write-Host "Invalid selection"
    exit 1
}

$model = $models[[int]$choice - 1]
Write-Host "Loading: $model"
Write-Host ""

& $Bin -m $model -n 8192 --ctx-size 16384 --use-direct-io -ngl -1
exit $LASTEXITCODE