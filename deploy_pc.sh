#!/usr/bin/env bash
# deploy_pc.sh — deploya a main SOLO los archivos que le pases, en un commit
# limpio, sin tocar el resto de tu árbol de trabajo (que tiene cambios sin
# commitear). Usa las credenciales de git ya guardadas en tu Mac.
#
# Uso:
#   ./deploy_pc.sh "mensaje del commit" archivo1 [archivo2 ...]
#
# Ejemplo:
#   ./deploy_pc.sh "resultados: fix UT.PROY" modules/informes/queries.py
#
# La primera vez, para que no te pida login cada vez, corré una sola vez:
#   git config --global credential.helper osxkeychain
set -euo pipefail

MSG="${1:-}"
if [ -z "$MSG" ]; then
  echo "Uso: ./deploy_pc.sh \"mensaje\" archivo1 [archivo2 ...]" >&2
  exit 1
fi
shift
if [ "$#" -lt 1 ]; then
  echo "Pasá al menos un archivo a deployar." >&2
  exit 1
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE="$(git -C "$SRC" remote get-url origin)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "→ Clonando main limpio…"
git clone --depth 1 --branch main "$REMOTE" "$TMP/repo" >/dev/null 2>&1

for f in "$@"; do
  if [ ! -f "$SRC/$f" ]; then
    echo "✗ No existe el archivo: $f" >&2
    exit 1
  fi
  mkdir -p "$TMP/repo/$(dirname "$f")"
  cp "$SRC/$f" "$TMP/repo/$f"
done

cd "$TMP/repo"
git add -- "$@"
if git diff --cached --quiet; then
  echo "Nada que deployar (los archivos ya están iguales en main)."
  exit 0
fi

echo "→ Archivos a deployar:"
git diff --cached --name-only | sed 's/^/    /'
git -c user.email="teliscovich@gmail.com" -c user.name="Tamara (deploy_pc)" \
    commit -q -m "$MSG"

echo "→ Pusheando a main…"
git push origin HEAD:main

echo "✅ Deployado a main: $MSG"
echo "   (GitHub Actions deploya al server en ~1-2 min)"
