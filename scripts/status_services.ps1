$ErrorActionPreference = "Continue"
Set-Location "c:\Users\admin\Desktop\Backend_modular"

Write-Host ""
Write-Host "Daphne / port 8000" -ForegroundColor Cyan
netstat -ano | Select-String ":8000" | Select-Object -First 12

Write-Host ""
Write-Host "Celery processes" -ForegroundColor Cyan
Get-Process -Name celery -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -like "*Backend_modular*" } |
  Select-Object Id, ProcessName, Path, StartTime |
  Format-Table -AutoSize

Write-Host ""
Write-Host "Backend Python processes" -ForegroundColor Cyan
Get-Process -Name python -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -like "*Backend_modular*" } |
  Select-Object Id, ProcessName, Path, StartTime |
  Format-Table -AutoSize

Write-Host ""
Write-Host "Recent log files" -ForegroundColor Cyan
Get-ChildItem logs\*.log,django.log,daphne.combined.log -ErrorAction SilentlyContinue |
  Select-Object Name, Length, LastWriteTime |
  Format-Table -AutoSize
