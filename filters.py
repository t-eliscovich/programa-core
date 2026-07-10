"""Jinja filters. Spanish/Ecuadorian number formatting to match formulas_app."""
import contextvars as _contextvars
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

from error_messages import humanize as _humanize

# Override ACOTADO del "hoy" — SOLO para simulacros/tests de cierre de mes
# (endpoint /admin/health/simulacro-cierre). Es un contextvar: aplica solo al
# request/tarea actual, nunca al resto de la app ni a otros usuarios.
_TODAY_OVERRIDE: "_contextvars.ContextVar[date | None]" = _contextvars.ContextVar(
    "today_ec_override", default=None
)


def set_today_override(d):
    """Setea el 'hoy' simulado (solo simulacros). Devuelve un token para reset."""
    return _TODAY_OVERRIDE.set(d)


def reset_today_override(token):
    _TODAY_OVERRIDE.reset(token)


def today_ec() -> date:
    """Fecha de HOY en Ecuador (America/Guayaquil = UTC-5, sin horario de verano).

    TMT 2026-06-04 (bug hunt lente 3): el servidor corre en UTC, ~5h adelante
    de Ecuador. `date.today()` del server salta al día siguiente después de
    las ~19h de Ecuador, lo que fecha transacciones (caja, facturas, cheques,
    bancos) con el día de MAÑANA y rompe cálculos de fin de mes. Para fechar
    o comparar fechas de negocio, usar today_ec() en lugar de date.today().
    """
    _ov = _TODAY_OVERRIDE.get()
    if _ov is not None:
        return _ov
    return (datetime.now(UTC) - timedelta(hours=5)).date()


def num_es(value, decimales: int = 2) -> str:
    """Format number with comma decimal and dot thousands (es-EC)."""
    if value is None or value == "":
        return ""
    try:
        d = Decimal(str(value))
    except Exception:
        return str(value)
    # Format with US locale then swap separators
    s = f"{d:,.{decimales}f}"
    # '1,234,567.89' -> '1.234.567,89'
    return s.replace(",", "@").replace(".", ",").replace("@", ".")


def kg_es(value) -> str:
    """Format kilos: 2 decimales."""
    return num_es(value, 2)


def money_es(value) -> str:
    """Money: 2 decimales con símbolo implícito (dólar)."""
    if value is None or value == "":
        return ""
    return num_es(value, 2)


def fecha_es(value) -> str:
    """Render date as dd/mm/yyyy.

    Acepta `date` o `datetime`. Si recibe un datetime, descarta la hora
    (usar `fecha_hora_es` si querés ver el time).
    """
    if value is None or value == "":
        return ""
    if isinstance(value, datetime | date):
        return value.strftime("%d/%m/%Y")
    return str(value)


def dias_desde(value) -> str:
    """Días transcurridos entre HOY (fecha de negocio EC) y `value` (fecha de
    emisión). Devuelve '' si no hay fecha. TMT 2026-07-09 (dueña): columna
    DÍAS del estado de cuenta."""
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        value = value.date()
    if not isinstance(value, date):
        try:
            value = date.fromisoformat(str(value)[:10])
        except (ValueError, TypeError):
            return ""
    return str((today_ec() - value).days)


def fecha_hora_es(value) -> str:
    """Render datetime as dd/mm/yyyy HH:MM:SS.

    Para timestamps de bitácora, audit logs, ejecuciones de tareas, etc.
    Si recibe un `date` (sin hora), devuelve sólo la fecha (compatible
    con `fecha_es` para evitar 00:00:00 inútil).
    """
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%d/%m/%Y")
    return str(value)


def _to_ec(value):
    """Convert UTC datetime → Ecuador local (UTC-5, sin DST).

    Server corre en UTC (memory feedback_yy_display_time_lecciones #4).
    Si el datetime ya es timezone-aware, hacemos astimezone. Si es naive,
    asumimos UTC (que es como Postgres CURRENT_TIMESTAMP llega via psycopg2).
    """
    if value is None or value == "":
        return None
    if not isinstance(value, datetime):
        return value
    from datetime import timedelta
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(timezone(timedelta(hours=-5)))


def fecha_hora_ec(value) -> str:
    """Render datetime as dd/mm/yyyy HH:MM:SS en hora Ecuador (UTC-5)."""
    v = _to_ec(value)
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%d/%m/%Y %H:%M:%S")
    if isinstance(v, date):
        return v.strftime("%d/%m/%Y")
    return str(v)


def hora_ec(value, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Render datetime en hora Ecuador con formato configurable.

    Default: '%Y-%m-%d %H:%M' (compat con strftime que usa el código viejo).
    Para historial: hora_ec(s.abierta_en) en lugar de
    s.abierta_en.strftime('%Y-%m-%d %H:%M').
    """
    v = _to_ec(value)
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime(fmt)
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    return str(v)


def humanizar(value) -> str:
    """Convierte una excepción (u objeto tipo excepción) en mensaje legible.

    Acepta también strings (los devuelve tal cual) para que el filter sea
    seguro aún si el template le pasa un str por error.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, Exception):
        return _humanize(value)
    return str(value)


# Strings que el dump dBase→Postgres dejó como literal "None" cuando
# debería haber sido NULL. Cualquier comparación de string contra estos
# valores debería tratarse como vacío.
_NONE_LIKE = frozenset({"none", "null", "nan"})


def cleanstr(value, fallback: str = "") -> str:
    """Devuelve string sano:

    - None / "" / "None" / "NULL" / "NaN" → fallback (vacío por default)
    - Cualquier otra cosa → str(value).strip()

    El dump del dBase legacy a veces guardó "None" literal (con N mayúscula)
    en vez de NULL — este filtro lo cubre. Aplicar en todos los lugares donde
    el campo no es identificador clave (no tocar codigo_cli o id_*).
    """
    if value is None:
        return fallback
    s = str(value).strip()
    if not s or s.lower() in _NONE_LIKE:
        return fallback
    return s


def register(app):
    app.jinja_env.filters["num_es"] = num_es
    app.jinja_env.filters["kg_es"] = kg_es
    app.jinja_env.filters["money_es"] = money_es
    app.jinja_env.filters["fecha_es"] = fecha_es
    app.jinja_env.filters["dias_desde"] = dias_desde
    app.jinja_env.filters["fecha_hora_es"] = fecha_hora_es
    app.jinja_env.filters["fecha_hora_ec"] = fecha_hora_ec
    app.jinja_env.filters["hora_ec"] = hora_ec
    app.jinja_env.filters["humanizar"] = humanizar
    app.jinja_env.filters["cleanstr"] = cleanstr
