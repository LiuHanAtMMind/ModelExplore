$ErrorActionPreference = "Stop"

$dist = "D:\projects\model_explore\.bazel_distdir"
$target = Join-Path $dist "zlib-1.3.1.tar.gz"
$expected = "9a93b2b7dfdac77ceba5a558a580e74667dd6fede4585b91eefb60f03b72df23"
$urls = @(
  "https://zlib.net/fossils/zlib-1.3.1.tar.gz",
  "https://www.zlib.net/fossils/zlib-1.3.1.tar.gz",
  "https://zlib.net/zlib-1.3.1.tar.gz",
  "https://www.zlib.net/zlib-1.3.1.tar.gz",
  "https://fossils.zlib.net/zlib-1.3.1.tar.gz"
)

New-Item -ItemType Directory -Path $dist -Force | Out-Null

$ok = $false
for ($round = 1; $round -le 3 -and -not $ok; $round++) {
  Write-Host "ROUND $round"
  foreach ($url in $urls) {
    try {
      Write-Host "TRY $url"
      & curl.exe -L --retry 5 --retry-delay 3 --connect-timeout 20 --max-time 1800 -C - -o $target $url
      if ($LASTEXITCODE -ne 0) {
        Write-Host "CURL_FAILED code=$LASTEXITCODE"
        continue
      }

      $sha = (Get-FileHash -Algorithm SHA256 $target).Hash.ToLower()
      Write-Host "SHA $sha"
      if ($sha -eq $expected) {
        Write-Host "ZLIB_SHA_OK"
        $ok = $true
        break
      }

      Write-Host "ZLIB_SHA_MISMATCH"
      # Wrong bytes may have been resumed from a different source; restart file cleanly.
      if (Test-Path $target) {
        Remove-Item $target -Force
      }
    }
    catch {
      Write-Host ("FAILED {0} :: {1}" -f $url, $_.Exception.Message)
    }
  }
}

if (-not $ok) {
  throw "Unable to fetch zlib archive with expected SHA256"
}
