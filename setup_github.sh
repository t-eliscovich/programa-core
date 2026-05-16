#!/usr/bin/env bash
# setup_github.sh — sube Programa Core a GitHub.
# Uso:
#   1) Creá el repo vacío en https://github.com/new
#      - Repository name:  programa-core
#      - Privacy:          Private  (¡importante! contiene lógica financiera)
#      - NO marques "Add a README" / .gitignore / license  → tiene que arrancar vacío
#   2) Copiá la URL que te muestra GitHub (ej. https://github.com/TUUSUARIO/programa-core.git)
#   3) Ejecutá:  bash setup_github.sh https://github.com/TUUSUARIO/programa-core.git
#
# Si ya corriste el script antes y querés volver a subir cambios, simplemente:
#   git add -A && git commit -m "tu mensaje" && git push
set -euo pipefail

REMOTE_URL="${1:-}"
if [ -z "$REMOTE_URL" ]; then
  echo "❌ Falta la URL del repo. Uso:"
  echo "   bash setup_github.sh https://github.com/TUUSUARIO/programa-core.git"
  exit 1
fi

cd "$(dirname "$0")"
echo "📍 Directorio: $(pwd)"

# Limpieza si quedó .git roto de algún intento anterior.
if [ -d .git ]; then
  echo "♻️  Limpiando .git existente…"
  rm -rf .git
fi

echo "🔧 git init…"
git init -b main >/dev/null
git config user.email "teliscovich@gmail.com"
git config user.name  "Tamara Eliscovich"

echo "📦 staging archivos (respetando .gitignore)…"
git add -A

n_files=$(git status --short | wc -l | tr -d ' ')
echo "    $n_files archivos staged"

echo "💾 commit inicial…"
git commit -m "Initial commit — Programa Core

Port de Flask + PostgreSQL del sistema legacy dBase/Clipper de Intela.
Incluye:
- Cobranza, gastos, facturas, compras, retiros, capital
- Cheques (cartera, depósito, endoso, rebote)
- Bancos + transferencias + emisión propia
- Posdatados (pasivos)
- Stock + informes + historial unificado
- Migration runner + importador DBF + skills" >/dev/null

echo "🌐 remote → $REMOTE_URL"
git remote add origin "$REMOTE_URL"

echo "⬆️  push…"
git push -u origin main

echo ""
echo "✅ Listo. Tu código está en GitHub."
echo "    Para próximos commits:"
echo "       git add -A && git commit -m \"mensaje\" && git push"
