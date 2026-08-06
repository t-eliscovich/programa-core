#!/usr/bin/env bash
# Deploy MANUAL de Programa Core — réplica 1:1 del job "deploy" de
# .github/workflows/deploy.yml, para cuando GitHub Actions está caído
# (estreno: outage de Actions del 06/08/2026).
#
# Correr desde AWS CloudShell (ya autenticado):
#   git clone --depth 1 https://github.com/t-eliscovich/programa-core.git ~/pc-deploy
#   bash ~/pc-deploy/scripts/deploy_manual.sh
#
# ⚠ Esto SALTEA el CI. Antes de usarlo, la suite tiene que haber corrido
#   verde en otro lado (python3 -m pytest tests/ -q sobre el mismo HEAD).
set -euo pipefail
export AWS_PAGER=""

REGION=us-east-2
INSTANCE=i-0fcca4d7029f08489

cd "$(dirname "$0")/.."
SHA=$(git rev-parse HEAD 2>/dev/null || echo "sin-git")
echo "=== Deploy manual de $SHA desde $(pwd) ==="

# 1. Package (mismos excludes que deploy.yml)
tar --exclude='./.venv' --exclude='./venv' \
    --exclude='./__pycache__' --exclude='./.git' \
    --exclude='./.pytest_cache' --exclude='./.ruff_cache' \
    --exclude='./.tw-build' \
    --exclude='./node_modules' \
    --exclude='./.github' \
    --exclude='*.rar' --exclude='./intela12042026.sql' \
    --exclude='*.pyc' --exclude='*.pyo' \
    --exclude='./AUDIT_*' --exclude='./BUG_HUNT_*' \
    --exclude='./E2E_TESTS_*' --exclude='./HALLAZGOS_*' \
    --exclude='./SMOKE_TEST_*' --exclude='./PLAN_BUG_*' \
    -czf /tmp/programa_core.tar.gz .
ls -lh /tmp/programa_core.tar.gz

# 2. Upload a S3 + presigned URL
ACCT=$(aws sts get-caller-identity --query Account --output text)
S3_KEY="programa_core_deploy/programa_core.tar.gz"
aws s3 cp /tmp/programa_core.tar.gz "s3://intela-deploy-${ACCT}/${S3_KEY}" --region "$REGION"
URL=$(aws s3 presign "s3://intela-deploy-${ACCT}/${S3_KEY}" --expires-in 1800 --region "$REGION")

# 3. PowerShell que corre en el EC2 (idéntico al de deploy.yml).
#    Heredoc QUOTED: todo literal; URL y SHA entran por sustitución bash
#    (literal, aguanta los & de la presigned URL).
PS=$(cat <<'PSEOF'
$ErrorActionPreference = 'Continue'
$env:PYTHONIOENCODING = 'utf-8'
$url = '__URL__'
$sha = '__SHA__'

Write-Output "=== Deploy commit $sha ==="
$ProgressPreference = 'SilentlyContinue'

# 1. Backup del .env (no se pisa, pero por las dudas)
if (Test-Path C:\programa-core\.env) { Copy-Item C:\programa-core\.env C:\tmp\dotenv.backup -Force }

# 2. Descargar tarball
(New-Object System.Net.WebClient).DownloadFile($url, 'C:\tmp\deploy.tar.gz')
Write-Output "Downloaded $((Get-Item C:\tmp\deploy.tar.gz).Length) bytes"

# 3. Extraer en sitio (los excludes del tarball protegen .env)
if (-not (Test-Path C:\programa-core)) { New-Item -ItemType Directory -Path C:\programa-core | Out-Null }
tar -xzf C:\tmp\deploy.tar.gz -C C:\programa-core
if ($LASTEXITCODE -ne 0) { Write-Output "ERROR: tar exit $LASTEXITCODE"; exit 1 }

# 4. pip install
Set-Location C:\programa-core
& C:\Python312\python.exe -m pip install -r requirements.txt --quiet --disable-pip-version-check 2>&1 | Select -Last 3

# 5. Restart: parar la task y matar por PUERTO 5002 (nunca por nombre —
#    formulas_app comparte python.exe; lección del 01/08)
Stop-ScheduledTask -TaskName ProgramaCoreApp -ErrorAction SilentlyContinue
Start-Sleep 2
for ($i = 0; $i -lt 6; $i++) {
  $owners = Get-NetTCPConnection -LocalPort 5002 -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
  if (-not $owners) { break }
  $owners | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
  Start-Sleep 1
}
Start-Sleep 3
$still = Get-NetTCPConnection -LocalPort 5002 -State Listen -ErrorAction SilentlyContinue
if ($still) { Write-Output "ERROR: puerto 5002 sigue tomado por PID(s) $($still.OwningProcess -join ',')"; exit 1 }
Start-ScheduledTask -TaskName ProgramaCoreApp
Start-Sleep 15

# 6. Health check
$state = (Get-ScheduledTask -TaskName ProgramaCoreApp).State
Write-Output "Task state: $state"
try {
    $code = (Invoke-WebRequest -UseBasicParsing http://localhost:5002/login -TimeoutSec 10).StatusCode
    Write-Output "Health: HTTP $code"
    if ($code -ne 200) { exit 1 }
} catch {
    Write-Output "Health FAIL: $_"
    exit 1
}
Write-Output "=== Deploy OK ==="
PSEOF
)
# Sustitución con python3, NUNCA con ${PS//x/$URL}: en bash 5.2 (CloudShell)
# el & del reemplazo se expande al patrón y rompe la presigned URL (400 del
# primer intento, 06/08/2026).
PS=$(printf '%s' "$PS" | URL="$URL" SHA="$SHA" python3 -c \
  'import os,sys; print(sys.stdin.read().replace("__URL__",os.environ["URL"]).replace("__SHA__",os.environ["SHA"]))')

# Pre-check: la URL tiene que responder ANTES de gastar el viaje por SSM
# (range-GET de 1 byte; la firma es para GET, un HEAD daría 403).
if ! curl -fsS -r 0-0 -o /dev/null "$URL"; then
  echo "ERROR: la presigned URL no responde desde CloudShell — no mando el SSM."
  exit 1
fi

# 4. Mandar por SSM (base64 → Invoke-Expression, igual que deploy.yml)
B64=$(printf '%s' "$PS" | base64 -w0)
cat > /tmp/p.json <<JSONEOF
{"commands":["[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('${B64}')) | Invoke-Expression"]}
JSONEOF

CMD_ID=$(aws ssm send-command \
  --region "$REGION" \
  --instance-ids "$INSTANCE" \
  --document-name AWS-RunPowerShellScript \
  --parameters file:///tmp/p.json \
  --query Command.CommandId --output text)
echo "Command ID: $CMD_ID"

# 5. Poll cada 5s, máx 4 min
STATUS=Pending
for i in {1..48}; do
  sleep 5
  STATUS=$(aws ssm get-command-invocation \
    --region "$REGION" --instance-id "$INSTANCE" --command-id "$CMD_ID" \
    --query Status --output text 2>/dev/null || echo "Pending")
  echo "Attempt $i: $STATUS"
  if [ "$STATUS" = "Success" ] || [ "$STATUS" = "Failed" ] || [ "$STATUS" = "TimedOut" ]; then
    break
  fi
done

echo "=== SSM output ==="
aws ssm get-command-invocation \
  --region "$REGION" --instance-id "$INSTANCE" --command-id "$CMD_ID" \
  --query '{Status:Status,Stdout:StandardOutputContent,Stderr:StandardErrorContent}' \
  --output json

if [ "$STATUS" != "Success" ]; then
  echo "Deploy failed (final status: $STATUS)"
  exit 1
fi
