#!/usr/bin/env bash
#
# sync_dbf_one_shot.sh — orquestador completo:
#   Mac → tarball → S3 → SSM al EC2 → sync_dbase_actual.py → poll status.
#
# Uso (desde CloudShell):
#   1. Subir tarball ~/dbf-fresh.tar.gz vía Actions → Upload file
#   2. bash sync_dbf_one_shot.sh
#
# Si vino acá con `aws sts get-caller-identity` configurado y el tarball en
# ~/dbf-fresh.tar.gz, todo lo demás corre solo.

set -euo pipefail

# ---------- Config ----------
INSTANCE_ID="${INSTANCE_ID:-i-0fcca4d7029f08489}"
REGION="${REGION:-us-east-2}"
TARBALL="${TARBALL:-$HOME/dbf-fresh.tar.gz}"

# ---------- Validación ----------
if [ ! -f "$TARBALL" ]; then
    echo "✗ ERROR: $TARBALL no existe."
    echo ""
    echo "Pasos previos:"
    echo "  1. En tu Mac terminal:"
    echo "       cd '/Users/tamaraeliscovich/Documents/INTELA copy/Files'"
    echo "       tar -czf ~/dbf-fresh.tar.gz *.DBF"
    echo "  2. En CloudShell: Actions → Upload file → ~/dbf-fresh.tar.gz"
    echo "  3. Re-correr este script."
    exit 1
fi

echo "→ Tarball: $TARBALL ($(du -h "$TARBALL" | cut -f1))"

# ---------- Upload a S3 ----------
ACCT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="intela-deploy-${ACCT}"
TS=$(date -u +%Y%m%dT%H%M%SZ)
S3_KEY="dbf-snapshots/dbf-${TS}.tar.gz"

echo "→ Subiendo a s3://${BUCKET}/${S3_KEY}..."
aws s3 cp "$TARBALL" "s3://${BUCKET}/${S3_KEY}" --region "$REGION"

URL=$(aws s3 presign "s3://${BUCKET}/${S3_KEY}" --expires-in 3600 --region "$REGION")
echo "✓ Upload OK"

# ---------- SSM al EC2 ----------
echo ""
echo "→ Disparando sync en EC2 ($INSTANCE_ID)..."

export PS_CMD="\$ErrorActionPreference='Stop'; Invoke-WebRequest -Uri '$URL' -OutFile C:\\tmp\\dbf-fresh.tar.gz; if (Test-Path C:\\tmp\\dbf-fresh) { Remove-Item -Recurse -Force C:\\tmp\\dbf-fresh }; New-Item -ItemType Directory -Path C:\\tmp\\dbf-fresh | Out-Null; tar -xzf C:\\tmp\\dbf-fresh.tar.gz -C C:\\tmp\\dbf-fresh; cd C:\\programa-core; \$env:DATABASE_URL = [System.Environment]::GetEnvironmentVariable('DATABASE_URL','Machine'); \$env:I_KNOW_THIS_IS_PROD = '1'; & 'C:\\Python312\\python.exe' scripts\\sync_dbase_actual.py --source C:\\tmp\\dbf-fresh; Write-Host '== Backfill numf_completo desde Asinfo =='; & 'C:\\Python312\\python.exe' scripts\\backfill_numf_completo_from_asinfo.py"

python3 -c 'import json,os; print(json.dumps({"commands":[os.environ["PS_CMD"]]}))' > /tmp/ssm_params.json

CMD_ID=$(aws ssm send-command \
    --region "$REGION" \
    --instance-ids "$INSTANCE_ID" \
    --document-name "AWS-RunPowerShellScript" \
    --parameters file:///tmp/ssm_params.json \
    --query 'Command.CommandId' --output text)

echo "✓ SSM Command ID: $CMD_ID"

# ---------- Polling ----------
echo ""
echo "→ Esperando que termine (max 5 min)..."

for i in {1..30}; do
    sleep 10
    STATUS=$(aws ssm get-command-invocation \
        --region "$REGION" --instance-id "$INSTANCE_ID" --command-id "$CMD_ID" \
        --query 'Status' --output text 2>/dev/null || echo "InProgress")
    echo "  [${i}/30] $(date +%H:%M:%S) Status: $STATUS"
    case "$STATUS" in
        Success|Failed|Cancelled|TimedOut) break;;
    esac
done

# ---------- Resultado ----------
echo ""
echo "===================== RESULTADO ====================="
aws ssm get-command-invocation \
    --region "$REGION" --instance-id "$INSTANCE_ID" --command-id "$CMD_ID" \
    --query '{Status:Status,StdOut:StandardOutputContent,StdErr:StandardErrorContent}' \
    --output json

if [ "$STATUS" = "Success" ]; then
    echo ""
    echo "✅ Migración OK. Verificá en https://programa.intela.com.ec/informes/balance"
    echo ""
    echo "Limpieza opcional:"
    echo "  rm -f $TARBALL"
else
    echo ""
    echo "❌ Status: $STATUS. Revisar StdErr arriba."
    exit 1
fi
