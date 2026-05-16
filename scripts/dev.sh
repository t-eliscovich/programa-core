#!/usr/bin/env bash
# Matar el server (si está corriendo), relanzarlo limpio, y abrir Chrome.
# Uso:
#   bash scripts/dev.sh           # mata + levanta en puerto 5000 + abre Chrome
#   bash scripts/dev.sh 5050      # mismo en otro puerto
#   bash scripts/dev.sh --no-open # sin abrir Chrome (solo mata + levanta)

set -e

# Parsear flag --no-open primero (puede venir antes o después del puerto).
OPEN_BROWSER=1
ARGS=()
for arg in "$@"; do
    case "$arg" in
        --no-open|--no-browser) OPEN_BROWSER=0 ;;
        *) ARGS+=("$arg") ;;
    esac
done
set -- "${ARGS[@]}"

# Validar puerto: si no es número entero, ignorar y usar 5000.
PORT="${1:-5000}"
if ! [[ "$PORT" =~ ^[0-9]+$ ]]; then
    echo "⚠ Puerto inválido '$PORT', usando 5000."
    PORT=5000
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "→ Buscando procesos viejos del server…"

# (1) Mata cualquier `python run.py` corriendo, en cualquier puerto.
PIDS_RUN=$(pgrep -f "python.*run\.py" 2>/dev/null || true)
if [ -n "$PIDS_RUN" ]; then
    echo "  Encontré python run.py viejo (pid: $PIDS_RUN), matando…"
    kill -9 $PIDS_RUN 2>/dev/null || true
fi

# (2) Mata cualquier `flask run` (por si quedó de una sesión anterior).
PIDS_FLASK=$(pgrep -f "flask.*run" 2>/dev/null || true)
if [ -n "$PIDS_FLASK" ]; then
    echo "  Encontré flask run viejo (pid: $PIDS_FLASK), matando…"
    kill -9 $PIDS_FLASK 2>/dev/null || true
fi

# (3) Mata cualquier proceso escuchando en el puerto target (último resort).
PIDS_PORT=$(lsof -ti :"$PORT" 2>/dev/null || true)
if [ -n "$PIDS_PORT" ]; then
    echo "  Encontré algo escuchando en puerto $PORT (pid: $PIDS_PORT), matando…"
    kill -9 $PIDS_PORT 2>/dev/null || true
fi

sleep 1

echo "→ Activando venv y levantando server en puerto $PORT…"

# Activar venv si existe y no está ya activado.
if [ -z "${VIRTUAL_ENV:-}" ] && [ -f ".venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

URL="http://127.0.0.1:$PORT/"

# ─── Auto-open Chrome ───
# Lanza un proceso en background que espera hasta que el server responda,
# después abre Chrome. Si Chrome no está, cae a 'open' default del sistema.
if [ "$OPEN_BROWSER" = "1" ]; then
    (
        # Esperar hasta 15 segundos a que el server responda.
        for i in $(seq 1 30); do
            if curl -s -o /dev/null -w "%{http_code}" "$URL" 2>/dev/null | grep -qE "^(200|302|303|401|405)$"; then
                break
            fi
            sleep 0.5
        done
        # macOS: usar `open -a "Google Chrome"`. Si Chrome no está, fallback.
        if [ "$(uname)" = "Darwin" ]; then
            if [ -d "/Applications/Google Chrome.app" ]; then
                open -a "Google Chrome" "$URL"
            else
                open "$URL"
            fi
        else
            # Linux / otros — google-chrome o xdg-open.
            if command -v google-chrome >/dev/null 2>&1; then
                google-chrome "$URL" >/dev/null 2>&1 &
            elif command -v xdg-open >/dev/null 2>&1; then
                xdg-open "$URL" >/dev/null 2>&1 &
            fi
        fi
    ) &
fi

# Lanzar `python run.py` (default — más silencioso que `flask run --debug`).
# Solo usamos `flask run` si pediste un puerto distinto del default (5000),
# porque run.py tiene el puerto hardcoded.
if [ "$PORT" = "5000" ]; then
    exec python run.py
elif command -v flask >/dev/null 2>&1; then
    # Puerto custom — usar flask run, pero sin --debug para evitar logs verbosos.
    # Si necesitás debug, exportá FLASK_DEBUG=1 antes de correr este script.
    exec flask --app run run --host 127.0.0.1 --port "$PORT"
else
    echo "⚠ Querés puerto $PORT pero flask CLI no está instalado."
    echo "  Usando puerto 5000 con python run.py."
    exec python run.py
fi
