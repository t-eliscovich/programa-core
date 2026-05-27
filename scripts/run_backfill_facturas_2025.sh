#!/usr/bin/env bash
# run_backfill_facturas_2025.sh — corre el backfill bulk de Asinfo 2025+
# desde CloudShell, vía SSM, contra la EC2 de prod.
#
# Uso:
#   # 1) Dry-run primero para ver qué inserta sin tocar nada:
#   curl -sL https://raw.githubusercontent.com/t-eliscovich/programa-core/main/scripts/run_backfill_facturas_2025.sh | DRY_RUN=1 bash
#
#   # 2) Real (sin DRY_RUN):
#   curl -sL https://raw.githubusercontent.com/t-eliscovich/programa-core/main/scripts/run_backfill_facturas_2025.sh | bash
#
# El backfill SOLO toca filas que no están todavía en scintela.factura.
# Las que mete las marca usuario_crea='asinfo-backfill' y a partir de ahí
# el sync DBF las preserva (no las truncate).

set -euo pipefail

INSTANCE_ID="${INSTANCE_ID:-i-0fcca4d7029f08489}"
REGION="${REGION:-us-east-2}"
DRY_RUN="${DRY_RUN:-}"
DESDE="${DESDE:-2025-01-01}"

ARGS="--desde $DESDE"
if [ -n "$DRY_RUN" ]; then
  ARGS="$ARGS --dry-run"
  echo "=== Backfill Asinfo → PC (DRY-RUN) — desde $DESDE ==="
else
  echo "=== Backfill Asinfo → PC (REAL) — desde $DESDE ==="
fi

# 1) Pull último main + correr el script Python. Todo dentro de PowerShell
#    porque la EC2 es Windows.
#
# CRÍTICO: SSM subprocess no hereda los Machine env vars del Scheduled Task.
# Hay que copiar explícitamente DB_*, FORMULAS_DATABASE_URL, METABASE_*,
# y ASINFO_CARD_FACTURAS al \$env: del proceso PowerShell antes de invocar
# Python. Sin esto, formulas_db.disponible()=False y Asinfo devuelve 0 filas
# silenciosamente.
PS=$(cat <<EOF
cd C:\\programa-core;
foreach (\$v in @('DATABASE_URL','DB_HOST','DB_PORT','DB_NAME','DB_USER','DB_PASSWORD','FORMULAS_DATABASE_URL','FORMULAS_POOL_MIN','FORMULAS_POOL_MAX','METABASE_URL','METABASE_USERNAME','METABASE_PASSWORD','ASINFO_CARD_FACTURAS','ASINFO_CARD_VENDEDOR_USD','ASINFO_CARD_VENDEDOR_KG','ASINFO_CARD_CLIENTE_KG')) {
  \$val = [System.Environment]::GetEnvironmentVariable(\$v, 'Machine');
  if (\$val) { Set-Item -Path "env:\$v" -Value \$val }
}
git fetch origin main;
git reset --hard origin/main;
& 'C:\\Python312\\python.exe' 'C:\\programa-core\\scripts\\backfill_facturas_2025_asinfo.py' $ARGS
EOF
)

# Armamos payload JSON robusto con Python (evita quoting hell de awscli).
# CRÍTICO: usar r""" (raw string) para que Python NO interprete los \b, \p, etc
# del path Windows como escape sequences. Sin la r, '\backfill_...' se vuelve
# '\x08ackfill_...' y PowerShell no encuentra el archivo.
python3 <<PYEOF > /tmp/ssm_backfill_payload.json
import json, os
cmd = r"""$PS"""
payload = {
    "InstanceIds": ["$INSTANCE_ID"],
    "DocumentName": "AWS-RunPowerShellScript",
    "Parameters": {"commands": [cmd]},
}
print(json.dumps(payload))
PYEOF

ID=$(aws ssm send-command --region "$REGION" --cli-input-json file:///tmp/ssm_backfill_payload.json \
  --query 'Command.CommandId' --output text)

echo "Command ID: $ID"
echo "Esperando — el backfill puede tardar 60-120s (Metabase + insert bulk)..."
sleep 30

STATUS="Pending"
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
  STATUS=$(aws ssm get-command-invocation --region "$REGION" \
    --instance-id "$INSTANCE_ID" --command-id "$ID" \
    --query 'Status' --output text 2>/dev/null || echo "Pending")
  if [ "$STATUS" = "Success" ] || [ "$STATUS" = "Failed" ]; then
    break
  fi
  echo "  ... $STATUS (esperando $((i*10))s más)"
  sleep 10
done

echo ""
echo "=== RESULTADO (status=$STATUS) ==="
aws ssm get-command-invocation --region "$REGION" \
  --instance-id "$INSTANCE_ID" --command-id "$ID" \
  --query 'StandardOutputContent' --output text
echo ""
echo "=== ERRORS (si hubo) ==="
aws ssm get-command-invocation --region "$REGION" \
  --instance-id "$INSTANCE_ID" --command-id "$ID" \
  --query 'StandardErrorContent' --output text

if [ "$STATUS" != "Success" ]; then
  echo ""
  echo "!! El comando NO terminó OK. Mirá los errores arriba."
  exit 1
fi

echo ""
if [ -n "$DRY_RUN" ]; then
  echo "DRY-RUN listo. Si los números pintan bien, corré sin DRY_RUN para insertar."
else
  echo "Backfill REAL listo. Estas facturas quedan marcadas 'asinfo-backfill' — el sync DBF NO las pisa."
fi
