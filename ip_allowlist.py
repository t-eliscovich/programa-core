"""IP allowlist por rol.

Filosofía:
    - **Default-allow.** Si no hay env var para un rol, el rol pasa. Evita
      que una migración de infra lockee a todos por omisión.
    - Por rol, no por usuario. Los roles de back-office (Dueño, Administrador,
      Contabilidad) son los candidatos típicos a allowlist — la fábrica
      trabaja desde IP fija en Ecuador y eso es casi gratis.
    - Soporta IPs exactas, rangos CIDR, y la forma legible comma-separated.
    - Respeta X-Forwarded-For si TRUST_PROXY=1 — cuando el app está detrás
      de ELB/CloudFront/Nginx. Sin eso, confía en remote_addr.

Config por .env:
    ROLE_IP_ALLOWLIST_DUENO=190.152.1.100,190.152.1.0/24
    ROLE_IP_ALLOWLIST_ADMINISTRADOR=190.152.1.100
    ROLE_IP_ALLOWLIST_CONTABILIDAD=190.152.1.0/24
    TRUST_PROXY=1        # opcional — si está detrás de ELB/CloudFront

El nombre del rol se normaliza: mayúsculas, sin tilde, espacios a underscore
(Dueño → DUENO). La normalización vive en `_env_key_for_role()`.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import unicodedata
from functools import lru_cache

from flask import g, request

_LOG = logging.getLogger("programa_core.ip_allowlist")

# Paths que NO chequean allowlist (assets, login, healthcheck). Si bloqueás el
# login, el usuario correcto no puede loguearse para decirte que está bloqueado.
_SKIP_PREFIXES = ("/static/", "/favicon", "/healthz", "/login", "/logout")
# Nota: /healthz cubre también /healthz/ready por el startswith.


def _env_key_for_role(nombre_rol: str) -> str:
    """Dueño → ROLE_IP_ALLOWLIST_DUENO; Contabilidad → ROLE_IP_ALLOWLIST_CONTABILIDAD.

    Normaliza: upper + sin tildes + espacios a underscore. El código es el
    nombre que se ve en env vars.
    """
    if not nombre_rol:
        return ""
    normalizado = unicodedata.normalize("NFKD", nombre_rol)
    ascii_only = "".join(c for c in normalizado if not unicodedata.combining(c))
    slug = ascii_only.upper().replace(" ", "_")
    return f"ROLE_IP_ALLOWLIST_{slug}"


@lru_cache(maxsize=64)
def _parse_allowlist(raw: str) -> tuple[ipaddress._BaseNetwork, ...]:
    """Parsea "1.2.3.4,5.6.7.0/24" a lista de ip_network. Ignora entradas inválidas."""
    out: list[ipaddress._BaseNetwork] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            # strict=False permite '5.6.7.1/24' (host bits set) — el usuario
            # suele copiar-pegar la IP y el prefix sin limpiar.
            out.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            _LOG.warning("IP allowlist: token inválido %r ignorado", token)
    return tuple(out)


def allowlist_for_role(nombre_rol: str) -> tuple[ipaddress._BaseNetwork, ...]:
    """Devuelve la lista de networks permitidos para el rol, o () si no configurado."""
    key = _env_key_for_role(nombre_rol)
    raw = os.environ.get(key, "").strip()
    if not raw:
        return ()
    return _parse_allowlist(raw)


def client_ip() -> str:
    """IP real del cliente.

    Si TRUST_PROXY=1, usa el primer valor de X-Forwarded-For (el left-most es el
    original). Si no, usa remote_addr. Devolvemos string; la normalización a
    ipaddress.ip_address se hace en `_ip_en_redes` (para no fallar el app si
    llega basura).
    """
    trust = (os.environ.get("TRUST_PROXY", "0").lower() in ("1", "true", "yes", "on"))
    if trust:
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            # Puede venir "client, proxy1, proxy2" — queremos el primero.
            return xff.split(",")[0].strip()
    return request.remote_addr or ""


def _ip_en_redes(ip_str: str, redes: tuple[ipaddress._BaseNetwork, ...]) -> bool:
    if not ip_str or not redes:
        return False
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(ip in red for red in redes)


def ip_permitida_para_rol(nombre_rol: str, ip_str: str | None = None) -> bool:
    """Devuelve True si la IP del cliente está permitida para el rol.

    Default-allow: si el rol no tiene allowlist configurada, todas las IPs pasan.
    Esto hace que agregar el middleware sea backwards-compatible — los roles
    que no se configuren no cambian su comportamiento.
    """
    redes = allowlist_for_role(nombre_rol)
    if not redes:
        return True  # default-allow
    ip = ip_str if ip_str is not None else client_ip()
    return _ip_en_redes(ip, redes)


def enforce_allowlist():
    """Hook para `before_request`.

    Corre DESPUÉS de `load_logged_in_user`. Si `g.user` es None (no logueado)
    lo deja pasar — la auth decorator se encarga. Si el rol tiene allowlist y
    la IP no matchea, retorna 403 con mensaje claro.

    No bloquea paths de skip (`/static`, `/login`, `/healthz`…).
    """
    # Skip paths que no deben ser bloqueados nunca.
    path = request.path or ""
    if any(path.startswith(p) for p in _SKIP_PREFIXES):
        return None

    user = g.get("user")
    if not user:
        return None  # @requiere_login se encarga de redirigir

    rol = user.get("nombre_rol") or ""
    ip = client_ip()
    if ip_permitida_para_rol(rol, ip):
        return None

    # Bloqueada — log + 403 con mensaje informativo (sin filtrar la IP).
    _LOG.warning(
        "IP allowlist: bloqueado rol=%s user=%s ip=%s path=%s",
        rol, user.get("username"), ip, path,
    )
    from flask import render_template_string
    html = """
    <!doctype html>
    <html lang="es"><head><meta charset="utf-8">
    <title>Acceso bloqueado</title></head>
    <body style="font-family: system-ui; max-width: 600px; margin: 4rem auto; padding: 2rem; color:#334">
    <h1 style="color:#b91c1c">Acceso bloqueado</h1>
    <p>Tu cuenta <strong>{{ user }}</strong> (rol <strong>{{ rol }}</strong>) sólo puede acceder
    desde ubicaciones autorizadas. Si crees que esto es un error, contactá al administrador
    con los siguientes datos:</p>
    <ul>
      <li><strong>IP detectada:</strong> {{ ip }}</li>
      <li><strong>Ruta intentada:</strong> {{ path }}</li>
      <li><strong>Hora:</strong> {{ ts }}</li>
    </ul>
    </body></html>
    """
    from datetime import datetime
    return render_template_string(
        html,
        user=user.get("username"),
        rol=rol,
        ip=ip,
        path=path,
        ts=datetime.now().isoformat(timespec="seconds"),
    ), 403
