$ErrorActionPreference = "Continue"
Set-Location "c:\Users\admin\Desktop\Backend_modular"
New-Item -ItemType Directory -Force -Path "logs" | Out-Null

$files = [ordered]@{
  "daphne"   = "logs\daphne.log"
  "django"   = "django.log"
  "default"  = "logs\celery_default.log"
  "campaign" = "logs\celery_campaign.log"
  "incoming" = "logs\celery_incoming.log"
  "retry"    = "logs\celery_retry.log"
  "beat"     = "logs\celery_beat.log"
}

foreach ($path in $files.Values) {
  if (-not (Test-Path $path)) {
    New-Item -ItemType File -Force -Path $path | Out-Null
  }
}

$positions = @{}
foreach ($name in $files.Keys) {
  $positions[$name] = (Get-Item $files[$name]).Length
}

Write-Host "Tailing logs. Press Ctrl+C to stop." -ForegroundColor Cyan
Write-Host (($files.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join "  ")

while ($true) {
  foreach ($name in $files.Keys) {
    $path = $files[$name]
    try {
      $stream = [System.IO.File]::Open($path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
      try {
        if ($stream.Length -lt $positions[$name]) { $positions[$name] = 0 }
        $stream.Seek($positions[$name], [System.IO.SeekOrigin]::Begin) | Out-Null
        $reader = New-Object System.IO.StreamReader($stream)
        $text = $reader.ReadToEnd()
        $positions[$name] = $stream.Position
        if ($text) {
          $text -split "`r?`n" | Where-Object { $_ } | ForEach-Object {
            Write-Host "[$name] $_"
          }
        }
      } finally {
        $stream.Close()
      }
    } catch {
      Write-Host "[$name] tail error: $($_.Exception.Message)" -ForegroundColor Yellow
    }
  }
  Start-Sleep -Seconds 1
}
