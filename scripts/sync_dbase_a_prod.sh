#!/usr/bin/env bash
#
# sync_dbase_a_prod.sh — un solo comando para llevar los DBFs frescos
# de tu Mac a la RDS de producción vía S3 + SSM.
#
# Flujo:
#   1. Hace tarball de /Users/tamaraeliscovich/Documents/INTELA copy/Files/*.DBF
#   2. Sube el tarball a s3://intela-deploy-<acct>/dbf-snapshots/<timestamp>.tar.gz
#   3. Presigned URL (1h validez)
#   4. SSM al EC2: descarga, extrae a C:\tmp\dbf-fresh\, corre sync_dbase_actual.py
#      con --backup (snapshot de tablas Postgres antes del TRUNCATE+INSERT, por si
#      algo sale mal podés restaurar).
#   5. Polling del Command ID hasta Success/Failed.
#
# IMPORTANTE: el script asume que CloudShell ya tiene AWS creds activas y que
# `scripts/sync_dbase_actual.py` ya está deployado en EC2 (vía deploy normal de
# GitHub Actions). Si no lo está, primero `git push` para que el deploy lo lleve.
#
# Uso (en CloudShell, NO en tu Mac — necesita awscli con creds AWS activas):
#   bash sync_dbase_a_prod.sh
#
# Pero los DBFs viven en tu Mac, no en CloudShell. Dos opciones para resolver eso:
#   (a) Subís el tarball desde tu Mac primero (Actions → Upload file en CloudShell UI).
#       Después corrés este script con el tarball como argumento.
#   (b) Corrés la primera parte (tarball + upload a S3 vía aws s3 cp) DESDE TU MAC.
#       Después la segunda parte (SSM) desde CloudShell.
#
# Elegimos (a) por simplicidad: el script empieza asumiendo que vos ya subiste
# el tarball a CloudShell vía la UI.

set -euo pipefail

# ---------------- Config ----------------
INSTANCE_ID="${INSTANCE_ID:-i-0fcca4d7029f08489}"
REGION="${REGION:-us-east-2}"
TARBALL_LOCAL="${1:-$HOME/dbf-fresh.tar.gz}"  # path al tarball en CloudShell

if [ ! -f "$TARBALL_LOCAL" ]; then
    echo "ERROR: $TARBALL_LOCAL no existe."
    echo ""
    echo "Pasos:"
    echo "  1. En tu Mac terminal:"
    echo "       cd '/Users/tamaraeliscovich/Documents/INTELA copy/Files'"
    echo "       tar -czf ~/dbf-fresh.tar.gz *.DBF"
    echo "  2. En CloudShell: Actions → Upload file → seleccioná ~/dbf-fresh.tar.gz."
    echo "  3. Re-correr este script."
    exit 1
fi

# ---------------- Upload a S3 ----------------
ACCT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="intela-deploy-${ACCT}"
TS=$(date -u +%Y%m%dT%H%M%SZ)
S3_KEY="dbf-snapshots/dbf-${TS}.tar.gz"
S3_URI="s3://${BUCKET}/${S3_KEY}"

echo "→ Subiendo $TARBALL_LOCAL a $S3_URI..."
aws s3 cp "$TARBALL_LOCAL" "$S3_URI" --region "$REGION"

# Presigned URL valida 1 hora
URL=$(aws s3 presign "$S3_URI" --expires-in 3600 --region "$REGION")
echo "✓ Upload OK"

# ---------------- SSM: descargar, extraer, sincronizar ----------------
echo ""
echo "→ Disparando sync vía SSM..."

# PowerShell one-liner (newlines flatened — ver intela-aws-deploy SKILL gotcha)
PS_CMD="\$ErrorActionPreference='Stop'; Invoke-WebRequest -Uri '$URL' -OutFile C:\\tmp\\dbf-fresh.tar.gz; if (Test-Path C:\\tmp\\dbf-fresh) { Remove-Item -Recurse -Force C:\\tmp\\dbf-fresh }; New-Item -ItemType Directory -Path C:\\tmp\\dbf-fresh | Out-Null; tar -xzf C:\\tmp\\dbf-fresh.tar.gz -C C:\\tmp\\dbf-fresh; cd C:\\programa-core; \$env:DATABASE_URL = [System.Environment]::GetEnvironmentVariable('DATABASE_URL','Machine'); \$env:I_KNOW_THIS_IS_PROD = '1'; & 'C:\\Python312\\python.exe' scripts\\sync_dbase_actual.py --source C:\\tmp\\dbf-fresh"

CMD=$(aws ssm send-command --region "$REGION" --instance-ids "$INSTANCE_ID" \
    --document-name "AWS-RunPowerShellScript" \
    --parameters "commands=[\"$PS_CMD\"]" \
    --query 'Command.CommandId' --output text)

echo "✓ SSM Command ID: $CMD"
echo ""
echo "→ Esperando que termine (polling cada 5s, max 5 min)..."

for i in $(seq 1 60); do
    sleep 5
    STATUS=$(aws ssm get-command-invocation --region "$REGION" \
        --instance-id "$INSTANCE_ID" --command-id "$CMD" \
        --query 'Status' --output text 2>/dev/null || echo "InProgress")
    echo "  [${i}/60] Status: $STATUS"
    case "$STATUS" in
        Success|Failed|Cancelled|TimedOut)
            break
            ;;
    esac
done

echo ""
echo "=========================================="
echo "RESULTADO FINAL:"
echo "=========================================="
aws ssm get-command-invocation --region "$REGION" \
    --instance-id "$INSTANCE_ID" --command-id "$CMD" \
    --query '{Status:Status,Out:StandardOutputContent,Err:StandardErrorContent}' \
    --output json

echo ""
echo "Si Status: Success → /facturas en producción ya tiene los DBFs frescos."
echo "Si Status: Failed → ver Err arriba para diagnóstico."
echo ""
echo "Limpieza del tarball local en CloudShell:"
echo "  rm -f $TARBALL_LOCAL"
