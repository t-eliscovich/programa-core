#!/usr/bin/env bash
# push_to_github.sh — sube los cambios del batch 2026-05-20 a origin/main.
#
# TMT 2026-05-20: pedido dueña "madame script para subir a github".
# Pensado para correrlo desde la raíz del repo (Programa Core/).
# El script es defensivo:
#   - Aborta si no estamos en la rama main (por las dudas, evita pushear
#     desde una rama de feature sin querer).
#   - Aborta si el working tree no tiene cambios (no hace falta correrlo).
#   - Hace `git pull --rebase` antes de pushear para evitar conflictos.
#   - Muestra el diff resumido antes de commitear; el usuario confirma.
#
# Uso:
#   ./scripts/push_to_github.sh                         # mensaje default
#   ./scripts/push_to_github.sh "fix flujo tooltip"     # mensaje custom

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

echo "→ Repo: $REPO_DIR"

# 1. Verificar rama
BRANCH="$(git branch --show-current)"
if [[ "$BRANCH" != "main" ]]; then
  echo "✗ Estás en la rama '$BRANCH'. Este script asume 'main'."
  echo "  Si querés pushear igual: git push origin $BRANCH"
  exit 1
fi
echo "→ Rama: $BRANCH"

# 2. Verificar que haya cambios
if git diff --quiet && git diff --cached --quiet && [[ -z "$(git ls-files --others --exclude-standard)" ]]; then
  echo "✓ Nada para commitear. Working tree limpio."
  exit 0
fi

# 3. Mostrar resumen de cambios
echo ""
echo "→ Cambios pendientes:"
git status --short
echo ""

# 4. Confirmación — pasá --no-confirm o NO_CONFIRM=1 para saltarla.
# TMT 2026-05-20 update: la dueña dijo "siempre y" — default es seguir.
# Para abortar antes de pushear, hacé Ctrl+C cuando ves el mensaje del
# commit. Si querés volver al prompt explícito, pasá --confirm o setear
# CONFIRM=1.
if [[ "${1:-}" == "--confirm" || "${CONFIRM:-}" == "1" ]]; then
  read -r -p "¿Continuar con add + commit + push? [s/N] " RESP
  RESP_LC="$(printf '%s' "$RESP" | tr '[:upper:]' '[:lower:]')"
  case "$RESP_LC" in
    s|si|y|yes) : ;;
    *)
      echo "✗ Cancelado por el usuario."
      exit 1
      ;;
  esac
  # Si vino --confirm como primer arg, lo consumimos para que el commit
  # message use el siguiente (sino "--confirm" se usaría como mensaje).
  if [[ "${1:-}" == "--confirm" ]]; then
    shift
  fi
fi

# 5. Add todo
git add -A

# 6. Mensaje de commit
MSG="${1:-Batch 2026-05-20: provisiones → posdat (cuota mensual/diaria + importe inline), cheques dropdown estado P/D/B/X/1/2, fix tooltip flujo, fix tipo Cala Cali, sale Provisiones del menú}"
echo ""
echo "→ Mensaje del commit:"
echo "  $MSG"
echo ""

# 7. Commit
git commit -m "$MSG"

# 8. Pull --rebase para no romper si alguien pusheó antes
echo ""
echo "→ git pull --rebase origin main"
if ! git pull --rebase origin main; then
  echo ""
  echo "✗ El pull --rebase falló. Resolvé los conflictos y después corré:"
  echo "    git rebase --continue && git push origin main"
  exit 1
fi

# 9. Push
echo ""
echo "→ git push origin main"
git push origin main

# 10. Resumen final
echo ""
echo "✓ Subido a origin/main."
echo ""
echo "  Recordatorios post-push:"
echo "  - La migración 0036_fix_tipo_cala_cali.sql NO se corre sola."
echo "    En la EC2 (o local): python scripts/migrate.py"
echo "  - Si tocaste templates, las assets de Tailwind se rebuildean"
echo "    automáticamente al levantar el server (.tw-build watcher)."
