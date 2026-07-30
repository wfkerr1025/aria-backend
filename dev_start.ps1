# dev_start.ps1 - Kill existing listeners then launch backend and web UI
$REPO = 'D:\Users\William\ARIA-Lite Development\ARIA-Lite'
$ports = 3000,8765

# Kill any processes listening on the ports
$procs = Get-NetTCPConnection -State Listen | Where-Object LocalPort -in $ports | Select-Object -ExpandProperty OwningProcess -Unique
if ($procs) {
  Write-Host "Found processes listening on ports $($ports -join ', '). Killing: $($procs -join ', ')"
  $procs | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Seconds 1
}

Write-Host 'Starting ARIA Lite backend and web UI...'

# Start backend in a new pwsh window and tee logs
Start-Process -FilePath pwsh -ArgumentList '-NoProfile','-NoExit','-Command',"Set-Location '$REPO'; python -u backend\ws_server.py 2>&1 | Tee-Object -FilePath '$REPO\dev_backend.log'"

# Start web UI in a new pwsh window and tee logs
Start-Process -FilePath pwsh -ArgumentList '-NoProfile','-NoExit','-Command',"Set-Location '$REPO\webui'; python -u secure_server.py 2>&1 | Tee-Object -FilePath '$REPO\dev_server.log'"

Write-Host 'Launched. Logs: dev_backend.log dev_server.log'
