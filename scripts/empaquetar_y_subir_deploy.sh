#!/usr/bin/env bash
# Empaqueta el repo y lo sube a S3 con la key de ESTE commit.
#
# Vive acá y no adentro del workflow porque lo llaman DOS lugares: el job
# `paquete` de ci.yml (que corre en paralelo con los tests, para que cuando
# terminen el tarball ya esté arriba) y deploy.yml cuando se lo dispara a mano.
# La lista de exclusiones del tar es larga y se desactualiza sola si está
# escrita dos veces.
#
# Uso: scripts/empaquetar_y_subir_deploy.sh <sha> [role_arn]
set -euo pipefail

SHA="${1:?falta el sha del commit}"
ROLE_ARN="${2:-}"
REGION="${AWS_REGION:-us-east-2}"
TARBALL="${TARBALL:-/tmp/programa_core.tar.gz}"

# El número de cuenta sale del ARN del role (arn:aws:iam::<cuenta>:role/...) en
# vez de una llamada a STS: es el mismo dato y ahorra un viaje de red.
ACCT="$(printf '%s' "$ROLE_ARN" | cut -d: -f5)"
if [ -z "$ACCT" ]; then
  ACCT="$(aws sts get-caller-identity --query Account --output text)"
fi

# Exclusiones: deps locales, git, dumps SQL, archives, caches.
# NO incluímos .env (no existe en el repo, vive en el server).
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
    -czf "$TARBALL" .
ls -lh "$TARBALL"

# ⭐ La key va POR COMMIT. Con una key fija, dos deploys solapados se pisan el
# tarball entre ellos y uno termina deployando el commit del otro.
S3_URI="s3://intela-deploy-${ACCT}/programa_core_deploy/${SHA}.tar.gz"
aws s3 cp "$TARBALL" "$S3_URI" --region "$REGION"
echo "$S3_URI"
