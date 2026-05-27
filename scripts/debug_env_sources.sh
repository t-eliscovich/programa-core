#!/usr/bin/env bash
# debug_env_sources.sh — averiguar de dónde lee el web server las env vars
# de Asinfo / Metabase / formulas_app. Necesario para que scripts standalone
# (backfill, debug) puedan acceder a la misma config.
#
# Uso:
#   curl -sL https://raw.githubusercontent.com/t-eliscovich/programa-core/main/scripts/debug_env_sources.sh | bash

set -euo pipefail
INSTANCE_ID="${INSTANCE_ID:-i-0fcca4d7029f08489}"
REGION="${REGION:-us-east-2}"

echo "=== Diag env sources — Programa Core en EC2 ==="

PS_CMD='
Write-Host "=== 1) Files .env* en C:\programa-core ===" -ForegroundColor Cyan
Get-ChildItem -Path "C:\programa-core" -Filter ".env*" -File -Force 2>$null | ForEach-Object {
  Write-Host ("  {0}  ({1} bytes, modificado {2:yyyy-MM-dd HH:mm})" -f $_.Name, $_.Length, $_.LastWriteTime)
}

Write-Host ""
Write-Host "=== 2) Vars dentro de C:\programa-core\.env (sanitizado) ===" -ForegroundColor Cyan
if (Test-Path "C:\programa-core\.env") {
  $envFile = Get-Content "C:\programa-core\.env"
  foreach ($line in $envFile) {
    if ($line -match "^\s*#") { continue }
    if ($line -notmatch "=") { continue }
    $parts = $line -split "=", 2
    $k = $parts[0].Trim()
    $v = $parts[1].Trim()
    if ($v.Length -gt 30) { $v = $v.Substring(0,20) + "...<truncated>" }
    if ($k -match "PASSWORD|SECRET|TOKEN|KEY") { $v = "(seteada, len=" + $v.Length + ")" }
    Write-Host ("  {0,-35} = {1}" -f $k, $v)
  }
} else {
  Write-Host "  NO existe C:\programa-core\.env" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== 3) Vars dentro de .env.prod si existe ===" -ForegroundColor Cyan
if (Test-Path "C:\programa-core\.env.prod") {
  $envFile = Get-Content "C:\programa-core\.env.prod"
  foreach ($line in $envFile) {
    if ($line -match "^\s*#") { continue }
    if ($line -notmatch "=") { continue }
    $parts = $line -split "=", 2
    $k = $parts[0].Trim()
    $v = $parts[1].Trim()
    if ($v.Length -gt 30) { $v = $v.Substring(0,20) + "...<truncated>" }
    if ($k -match "PASSWORD|SECRET|TOKEN|KEY") { $v = "(seteada, len=" + $v.Length + ")" }
    Write-Host ("  {0,-35} = {1}" -f $k, $v)
  }
} else {
  Write-Host "  NO existe C:\programa-core\.env.prod"
}

Write-Host ""
Write-Host "=== 4) Machine env vars (ASINFO_*, METABASE_*, FORMULAS_*, DB_*, DATABASE_URL) ===" -ForegroundColor Cyan
$machineVars = [System.Environment]::GetEnvironmentVariables("Machine")
$found = $false
foreach ($key in $machineVars.Keys) {
  if ($key -match "^(ASINFO_|METABASE_|FORMULAS_|DB_|DATABASE_URL)") {
    $val = $machineVars[$key]
    if ($val.Length -gt 30) { $val = $val.Substring(0,20) + "...<truncated>" }
    if ($key -match "PASSWORD|SECRET|TOKEN") { $val = "(seteada, len=" + $val.Length + ")" }
    Write-Host ("  {0,-35} = {1}" -f $key, $val)
    $found = $true
  }
}
if (-not $found) { Write-Host "  (ninguna)" -ForegroundColor Yellow }

Write-Host ""
Write-Host "=== 5) Mismo en User env vars ===" -ForegroundColor Cyan
$userVars = [System.Environment]::GetEnvironmentVariables("User")
$found = $false
foreach ($key in $userVars.Keys) {
  if ($key -match "^(ASINFO_|METABASE_|FORMULAS_|DB_|DATABASE_URL)") {
    $val = $userVars[$key]
    if ($val.Length -gt 30) { $val = $val.Substring(0,20) + "...<truncated>" }
    if ($key -match "PASSWORD|SECRET|TOKEN") { $val = "(seteada, len=" + $val.Length + ")" }
    Write-Host ("  {0,-35} = {1}" -f $key, $val)
    $found = $true
  }
}
if (-not $found) { Write-Host "  (ninguna)" -ForegroundColor Yellow }

Write-Host ""
Write-Host "=== 6) Process env del web server (Waitress / python.exe) ===" -ForegroundColor Cyan
$procs = Get-Process python -ErrorAction SilentlyContinue
if ($procs) {
  foreach ($p in $procs) {
    Write-Host ("  Process: PID={0} StartTime={1}" -f $p.Id, $p.StartTime)
  }
  # Get-Process no expone Environment de otro proceso sin privilegios elevados.
  # Pero podemos hacer un HTTP request al endpoint healthz/integraciones
  # que SI corre dentro del proceso del web server y conoce su env real.
  Write-Host ""
  Write-Host "  Probando GET http://localhost:5050/healthz/integraciones ..." -ForegroundColor Cyan
  try {
    $r = Invoke-WebRequest -UseBasicParsing -Uri "http://localhost:5050/healthz/integraciones" -TimeoutSec 5
    Write-Host ("  status={0}" -f $r.StatusCode)
    Write-Host ("  body: {0}" -f $r.Content)
  } catch {
    Write-Host ("  ERROR: " + $_.Exception.Message) -ForegroundColor Red
  }
} else {
  Write-Host "  No hay process python.exe corriendo (web server caido?)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== 7) Scheduled Task ProgramaCore — action / env ===" -ForegroundColor Cyan
try {
  $t = Get-ScheduledTask -TaskName "ProgramaCore" -ErrorAction Stop
  $info = Get-ScheduledTaskInfo -TaskName "ProgramaCore"
  Write-Host ("  State: {0}  LastRun: {1}  NextRun: {2}" -f $t.State, $info.LastRunTime, $info.NextRunTime)
  foreach ($action in $t.Actions) {
    Write-Host ("  Execute: {0}" -f $action.Execute)
    Write-Host ("  Args:    {0}" -f $action.Arguments)
    Write-Host ("  WorkDir: {0}" -f $action.WorkingDirectory)
  }
} catch {
  Write-Host "  Scheduled Task ProgramaCore no encontrada" -ForegroundColor Yellow
}
'

# Escapar para JSON robusto
echo "$PS_CMD" > /tmp/ps_diag_env.txt
python3 <<'PYEOF' > /tmp/ssm_diag_env_payload.json
import json, os
with open('/tmp/ps_diag_env.txt') as f:
    cmd = f.read()
payload = {
    "InstanceIds": [os.environ.get('INSTANCE_ID', 'i-0fcca4d7029f08489')],
    "DocumentName": "AWS-RunPowerShellScript",
    "Parameters": {"commands": [cmd]},
}
print(json.dumps(payload))
PYEOF

ID=$(aws ssm send-command --region "$REGION" --cli-input-json file:///tmp/ssm_diag_env_payload.json \
  --query 'Command.CommandId' --output text)
echo "Command ID: $ID"
sleep 10

for i in 1 2 3 4 5; do
  STATUS=$(aws ssm get-command-invocation --region "$REGION" \
    --instance-id "$INSTANCE_ID" --command-id "$ID" \
    --query 'Status' --output text 2>/dev/null || echo "Pending")
  if [ "$STATUS" = "Success" ] || [ "$STATUS" = "Failed" ]; then
    break
  fi
  sleep 5
done

echo ""
echo "=== RESULTADO (status=$STATUS) ==="
aws ssm get-command-invocation --region "$REGION" \
  --instance-id "$INSTANCE_ID" --command-id "$ID" \
  --query 'StandardOutputContent' --output text
echo ""
echo "=== ERRORS ==="
aws ssm get-command-invocation --region "$REGION" \
  --instance-id "$INSTANCE_ID" --command-id "$ID" \
  --query 'StandardErrorContent' --output text
