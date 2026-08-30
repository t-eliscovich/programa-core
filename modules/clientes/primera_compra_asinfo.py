"""Desde cuándo es cliente — la PRIMERA factura que le hicimos, según Asinfo.

⭐ POR QUÉ (TMT 2026-08-30). Jaime (oficina) pidió ver en la ficha del cliente
"desde cuándo es cliente y qué monto promedio de compra tiene" — les sirve
como dato informativo y para emitir certificados. En el dBase eso vivía en la
opción 4V (MODIFICA.PRG PROCEDURE VENTAS).

El problema es el ARRANQUE de cada registro: las facturas de Programa Core
empiezan en ene 2025 (el backfill de Asinfo) y las del dBase que se migraron
son la cartera viva, no la historia. Un cliente de toda la vida aparecería
como "cliente desde 2025", que para un certificado es mentirle al cliente
para peor. Asinfo (el ERP fiscal) factura desde AGO 2019: es la memoria más
larga que hay, así que la fecha de la primera factura se le pregunta a él.

## Por qué una tabla espejo y no consulta en vivo

Mismo motivo (y mismo patrón, copiado a propósito) que
`modules/clientes/mail_asinfo.py`: Asinfo se alcanza sólo por Metabase, que
tarda segundos y a veces no está. La ficha del cliente se abre todo el día.
Se copia el dato a `scintela.cliente_primera_compra_asinfo` y la pantalla lee
local con un LEFT JOIN. Lo refresca el cron diario de `/admin/health/all`.
Bootstrap `CREATE TABLE IF NOT EXISTS` en caliente porque **el deploy no
corre migraciones**.

La clave de cruce es `ruc10` (los 10 primeros dígitos del RUC), la misma que
ya usa el espejo de mails — el código de 3 letras de PC no existe en Asinfo.
"""
from __future__ import annotations

import logging
from datetime import date

import db as _db
from modules.clientes.mail_asinfo import ruc10

_LOG = logging.getLogger("programa_core.clientes.primera_compra_asinfo")

_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS scintela.cliente_primera_compra_asinfo (
    ruc10       TEXT PRIMARY KEY,
    primera     DATE NOT NULL,
    actualizado TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

#: La primera factura que Asinfo tiene de CUALQUIERA: 28/08/2019 (medido el
#: 30/08/2026 contra `factura_cliente`). Un cliente cuya primera factura cae
#: en los primeros meses de ese registro seguramente es MÁS viejo — el
#: registro arranca ahí, no el cliente. Para esos la ficha dice "o antes".
ARRANQUE_ASINFO = date(2019, 8, 28)

#: `estado = 0` es factura ANULADA en Asinfo — mismo filtro que usa todo el
#: repo (ver modules/admin_dbase/clientes_asinfo_detalle_view.sql_facturas).
_SQL_ASINFO = """
SELECT LEFT(LTRIM(RTRIM(e.identificacion)), 10) AS ruc10,
       MIN(fc.fecha)                            AS primera
  FROM factura_cliente fc
  JOIN empresa e ON e.id_empresa = fc.id_empresa
 WHERE fc.estado <> 0
   AND LEN(LTRIM(RTRIM(ISNULL(e.identificacion, '')))) >= 10
 GROUP BY LEFT(LTRIM(RTRIM(e.identificacion)), 10)
"""

_bootstrapped = False


def asegurar_tabla() -> None:
    """Crea la tabla espejo si falta. Idempotente y a prueba de fallos.

    La llama la consulta de la ficha ANTES de su LEFT JOIN: si la tabla no
    existiera, la pantalla entera se caería con "relation does not exist" —
    y el deploy no corre migraciones.
    """
    global _bootstrapped
    if _bootstrapped:
        return
    try:
        _db.execute(_BOOTSTRAP_SQL)
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("bootstrap de cliente_primera_compra_asinfo falló: %s", exc)
    _bootstrapped = True


def refrescar() -> dict:
    """Copia la primera factura por RUC de Asinfo a la tabla espejo.

    Fail-soft: si Metabase no está configurado o se cayó, NO borra ni pisa lo
    que ya está guardado — deja la última foto buena y avisa. La fecha de la
    primera factura sólo puede moverse para ATRÁS (si Asinfo cargara historia
    vieja), así que una foto de ayer nunca es peligrosa, sólo conservadora.
    """
    from modules._lib import metabase_client as mc

    asegurar_tabla()
    if not mc.disponible():
        return {"ok": False, "error": "Metabase no configurado", "filas": 0}

    filas, contesto = mc.fetch_dataset_estado(2, _SQL_ASINFO, max_results=20000)
    if not contesto:
        return {"ok": False, "error": "Metabase no contestó", "filas": 0}

    valores: list[tuple[str, str]] = []
    vistos: set[str] = set()
    for f in filas or []:
        clave = ruc10(f.get("ruc10"))
        primera = str(f.get("primera") or "")[:10]  # Metabase manda ISO texto
        if not clave or len(primera) != 10 or clave in vistos:
            continue
        try:
            date.fromisoformat(primera)
        except ValueError:
            continue
        vistos.add(clave)
        valores.append((clave, primera))

    if not valores:
        return {"ok": True, "filas": 0, "leidas_de_asinfo": len(filas or [])}

    # UN solo INSERT — la lección medida del espejo de mails: uno por fila son
    # miles de viajes a RDS dentro del request del cron y un 502 del proxy.
    marcadores = ",".join(["(%s, %s::date)"] * len(valores))
    planos: list[str] = [v for fila in valores for v in fila]
    _db.execute(
        f"""
        INSERT INTO scintela.cliente_primera_compra_asinfo (ruc10, primera)
             VALUES {marcadores}
        ON CONFLICT (ruc10) DO UPDATE
                SET primera = LEAST(cliente_primera_compra_asinfo.primera,
                                    EXCLUDED.primera),
                    actualizado = CURRENT_TIMESTAMP
        """,
        tuple(planos),
    )
    return {"ok": True, "filas": len(valores),
            "leidas_de_asinfo": len(filas or [])}


#: La primera factura de un cliente cambia una vez en la vida (cuando se
#: vuelve cliente). Refrescar una vez al día ya es generoso.
HORAS_FRESCO = 20


def esta_fresco() -> bool:
    try:
        asegurar_tabla()
        r = _db.fetch_one(
            """
            SELECT MAX(actualizado) AS ult, COUNT(*) AS n
              FROM scintela.cliente_primera_compra_asinfo
            """
        )
    except Exception:  # noqa: BLE001
        return False
    if not r or not r.get("ult") or not r.get("n"):
        return False
    from datetime import datetime, timedelta
    return r["ult"] > datetime.now() - timedelta(hours=HORAS_FRESCO)


def refrescar_cron() -> dict:
    """Envoltorio para /admin/health/all — nunca levanta y no repite trabajo."""
    try:
        if esta_fresco():
            return {"ok": True, "salteado": "ya está fresco", "filas": 0}
        return refrescar()
    except Exception as e:  # noqa: BLE001
        _LOG.warning("refresco de primera compra de Asinfo falló: %s", e)
        return {"ok": False, "error": str(e)[:200], "filas": 0}


# ---------------------------------------------------------------------------
# Qué se muestra — funciones PURAS, testeables sin Postgres
# ---------------------------------------------------------------------------

#: Meses en castellano, abreviados como los escribe la oficina.
_MESES = ["ene", "feb", "mar", "abr", "may", "jun",
          "jul", "ago", "sep", "oct", "nov", "dic"]


def cliente_desde(primera_pc, primera_asinfo):
    """La fecha desde la que es cliente: la más VIEJA de las dos memorias.

    `primera_pc` = MIN(fecha) de sus facturas en Programa Core (arranca en
    ene 2025 para casi todos). `primera_asinfo` = el espejo (arranca ago
    2019). Cualquiera puede faltar (cliente nuevo sin facturas, espejo sin
    refrescar): se usa la que haya.
    """
    fechas = [f for f in (primera_pc, primera_asinfo) if f]
    return min(fechas) if fechas else None


def etiqueta_desde(desde, hoy) -> str:
    """El texto de la ficha: "jun 2022 · hace 4 años".

    Si la primera factura cae en el año en que ARRANCA el registro de Asinfo,
    el cliente casi seguro es más viejo que el registro: se dice "2019 o
    antes" en vez de inventar una precisión que el dato no tiene.
    """
    if not desde:
        return ""
    if desde <= date(ARRANQUE_ASINFO.year, 12, 31):
        return f"{ARRANQUE_ASINFO.year} o antes"
    texto = f"{_MESES[desde.month - 1]} {desde.year}"
    anios = (hoy - desde).days // 365
    if anios >= 1:
        texto += f" · hace {anios} año{'s' if anios > 1 else ''}"
    return texto
