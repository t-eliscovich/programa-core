"""El programa entra a Metabase con SU usuario, no con el de Tamara.

TMT 2026-09-23. Ese día el mail del usuario administrador de Tamara en
Metabase apareció cambiado y el programa, que entraba con ese usuario, se quedó
sin Asinfo (despachos, stock, precios, sync de clientes). Tamara: *"hacé todo
vos"* — que el programa tenga su propio usuario, `programa@intela.com.ec`
(creado a mano por ella, sin permisos de administrador).

Lo corre el deploy (deploy.yml, paso 4.c) EN el servidor, antes de reiniciar
la app. Idempotente:

  - Si el programa ya entra con `programa@intela.com.ec` y el login anda: no
    hace nada.
  - Si todavía entra con otro usuario (el de administrador): con ese usuario
    le pone al de programa una contraseña NUEVA, generada acá, la prueba, y
    recién entonces la deja en las variables de máquina (y en el .env si ahí
    estaban). La contraseña nace y queda en el servidor: no pasa por ningún
    lado, no se imprime y no se loguea (el log de GitHub Actions es público).
  - Ante cualquier duda (no existe el usuario, es administrador, el login
    nuevo no anda): no toca nada y lo dice en una línea.

Sale siempre 0: un deploy no se corta por esto.
"""
from __future__ import annotations

import json
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path

USUARIO = "programa@intela.com.ec"
ENV_FILE = Path(r"C:\programa-core\.env")
CLAVES = ("METABASE_URL", "METABASE_USERNAME", "METABASE_PASSWORD")
_REG = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"


def leer_dotenv(path: Path) -> dict:
    out: dict = {}
    if not path.exists():
        return out
    for linea in path.read_text(encoding="utf-8-sig").splitlines():
        s = linea.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k] = v
    return out


def reemplazar_dotenv(texto: str, nuevos: dict) -> str:
    """Cambia las claves que ya están y agrega las que faltan, sin tocar el resto."""
    lineas = texto.splitlines()
    vistas = set()
    for i, linea in enumerate(lineas):
        s = linea.strip()
        k = s.split("=", 1)[0].strip() if "=" in s else ""
        if k.startswith("export "):
            k = k[7:].strip()
        if k in nuevos:
            lineas[i] = f"{k}={nuevos[k]}"
            vistas.add(k)
    lineas += [f"{k}={v}" for k, v in nuevos.items() if k not in vistas]
    return "\n".join(lineas) + "\n"


def leer_maquina() -> dict:
    try:
        import winreg
    except ImportError:
        return {}
    out = {}
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _REG) as k:
        for c in CLAVES:
            try:
                out[c] = winreg.QueryValueEx(k, c)[0]
            except OSError:
                pass
    return out


def escribir_maquina(nuevos: dict) -> None:
    import ctypes
    import winreg

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _REG, 0, winreg.KEY_SET_VALUE) as k:
        for c, v in nuevos.items():
            winreg.SetValueEx(k, c, 0, winreg.REG_SZ, v)
    # Avisarle a Windows (y al Programador de tareas) que cambió el entorno.
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None)


def efectivas(maquina: dict, dotenv: dict) -> dict:
    """Lo que ve el programa: la variable de máquina gana (load_dotenv no pisa)."""
    return {c: (maquina.get(c) or dotenv.get(c) or "") for c in CLAVES}


def _http(metodo: str, url: str, cuerpo: dict | None = None, token: str | None = None):
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(url, data=datos, method=metodo)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Metabase-Session", token)
    with urllib.request.urlopen(req, timeout=20) as r:
        cuerpo_r = r.read().decode() or "null"
        return json.loads(cuerpo_r)


def login(url: str, usuario: str, clave: str, http=None) -> str | None:
    try:
        return ((http or _http)("POST", f"{url}/api/session", {"username": usuario, "password": clave}) or {}).get("id")
    except Exception:  # noqa: BLE001
        return None


def clave_nueva() -> str:
    # Letras, números, - y _ (nunca comillas): pasa por .env y PowerShell sin escapar.
    return secrets.token_urlsafe(24) + "a9Z"


def correr(maquina: dict, dotenv: dict, http=None) -> tuple[str, dict | None]:
    """Decide. Devuelve (mensaje, nuevos) — `nuevos` es lo que hay que escribir, o None."""
    h = http or _http
    ef = efectivas(maquina, dotenv)
    url = (ef["METABASE_URL"] or "").rstrip("/")
    if not (url and ef["METABASE_USERNAME"] and ef["METABASE_PASSWORD"]):
        return "sin METABASE_URL/USERNAME/PASSWORD en el servidor: no se toca", None
    actual = ef["METABASE_USERNAME"].strip().lower()
    if actual == USUARIO:
        if login(url, USUARIO, ef["METABASE_PASSWORD"], h):
            return f"ya entra con {USUARIO}", None
        return f"entra con {USUARIO} pero el login NO anda: revisar a mano", None
    token = login(url, ef["METABASE_USERNAME"], ef["METABASE_PASSWORD"], h)
    if not token:
        return f"el usuario actual ({actual}) no pudo entrar: no se toca", None
    try:
        usuarios = h("GET", f"{url}/api/user?status=all", token=token)
    except Exception as e:  # noqa: BLE001
        return f"no se pudo listar usuarios ({type(e).__name__}): el actual no es administrador?", None
    lista = usuarios.get("data", usuarios) if isinstance(usuarios, dict) else usuarios
    u = next((x for x in (lista or []) if (x.get("email") or "").lower() == USUARIO), None)
    if not u:
        return f"no existe {USUARIO} en Metabase: crearlo en Admin > People", None
    if not u.get("is_active", True):
        return f"{USUARIO} está desactivado en Metabase: no se toca", None
    if u.get("is_superuser"):
        return f"{USUARIO} es administrador en Metabase: sacarle eso primero", None
    clave = clave_nueva()
    try:
        h("PUT", f"{url}/api/user/{u['id']}/password", {"password": clave}, token=token)
    except Exception as e:  # noqa: BLE001
        return f"no se pudo poner la contraseña ({type(e).__name__}): no se toca", None
    if not login(url, USUARIO, clave, h):
        return f"la contraseña nueva de {USUARIO} no entra: no se toca", None
    return f"pasa a entrar con {USUARIO} (contraseña nueva, probada)", {
        "METABASE_USERNAME": USUARIO, "METABASE_PASSWORD": clave}


def main() -> int:
    try:
        maquina = leer_maquina()
        dotenv = leer_dotenv(ENV_FILE)
        msg, nuevos = correr(maquina, dotenv)
        if nuevos:
            if maquina.get("METABASE_USERNAME") or not ENV_FILE.exists():
                escribir_maquina(nuevos)
            if ENV_FILE.exists() and ("METABASE_USERNAME" in dotenv or not maquina.get("METABASE_USERNAME")):
                texto = ENV_FILE.read_text(encoding="utf-8-sig")
                ENV_FILE.with_name(".env.antes-usuario-metabase").write_text(texto, encoding="utf-8")
                ENV_FILE.write_text(reemplazar_dotenv(texto, nuevos), encoding="utf-8")
        print(msg)
    except Exception as e:  # noqa: BLE001 -- el deploy no se corta por esto
        print(f"AVISO: {type(e).__name__}: {str(e)[:120]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
