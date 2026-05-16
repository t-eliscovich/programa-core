"""Guard helper: bloquear escrituras sobre períodos cerrados.

Uso en cada query.crear(...):

    from periodo_guard import asegurar_fecha_abierta
    asegurar_fecha_abierta(fecha)  # levanta ValueError si el período está cerrado

Es intencionalmente una función (no un decorador) para que quede explícita
en el call-site — el cierre de período es una regla de negocio central
y no queremos que se oculte tras una indirección.

Failure mode (importante):
- Si el módulo `modules.periodos` no se puede importar (bootstrap temprano,
  test stub, error de sintaxis en periodos), antes era un no-op silente
  — el guard se desactivaba y todas las escrituras pasaban. Eso enmascaraba
  problemas de deploy.
- Ahora logueamos el error con `logger.exception` y dejamos `_periodos_q`
  apuntando al módulo. Si la app está corriendo en producción y la lib
  no carga, vas a ver el traceback en /tmp/programa-core.log.
- En `os.environ.get("PERIODO_GUARD_STRICT") == "1"` el import-fail
  hace que `asegurar_fecha_abierta` levante RuntimeError — útil para CI
  y para producción una vez que confiás que periodos siempre carga.
"""
import logging
import os
from datetime import date

logger = logging.getLogger(__name__)

_periodos_q = None
_import_error: Exception | None = None

try:
    from modules.periodos import queries as _periodos_q  # type: ignore[no-redef]
except Exception as e:  # módulo aún no registrado en un bootstrap temprano
    _import_error = e
    logger.exception(
        "periodo_guard: no pude importar modules.periodos.queries — "
        "el guard de períodos cerrados queda DESACTIVADO. "
        "Si esto pasa en producción, los writes a períodos cerrados "
        "no van a estar bloqueados. Setear PERIODO_GUARD_STRICT=1 "
        "para que tire RuntimeError en lugar de continuar silente."
    )


def asegurar_fecha_abierta(fecha: date | None) -> None:
    if fecha is None:
        return
    if _periodos_q is None:
        if os.environ.get("PERIODO_GUARD_STRICT") == "1":
            raise RuntimeError(
                f"periodo_guard: modules.periodos no cargó ({_import_error!r}). "
                "Configurado en STRICT — abortando."
            )
        return
    bloqueada, mensaje = _periodos_q.fecha_esta_bloqueada(fecha)
    if bloqueada:
        raise ValueError(mensaje or "Período contable cerrado.")
