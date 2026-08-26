"""El día de un movimiento de banco cargado a la tarde.

🚨 TMT 2026-08-26, sobre el renglón *"BC · 2 × GS. cheq., ch.devuelto 98655 ADI,
ch.devuelto 99048 JUT"* de la traza: *"el link de BC no anda"*. No daba 404:
llevaba al Historial filtrado por el **27/08** cuando los movimientos son del
**26/08**, y la pantalla contestaba "Sin movimientos en este filtro" — roto sin
dar 404, como el del 12/08.

Medido en producción con la consola SQL, sobre esas dos mismas notas de débito:

    fecha_crea                    2026-08-26T20:26:45   (= 15:26 de Ecuador)
    (… AT TIME ZONE 'America/Guayaquil')::date          2026-08-27   ✗
    ((… AT TIME ZONE 'UTC') AT TIME ZONE '…')::date     2026-08-26   ✓
    fecha (la que filtra el Historial)                  2026-08-26   ✓

**Por qué.** `scintela.transacciones_bancarias.fecha_crea` es `timestamp`
SIN zona (la tabla viene del dBase) y guarda hora UTC. `AT TIME ZONE` sobre un
timestamp sin zona hace lo contrario de lo que parece: no CONVIERTE a esa zona,
INTERPRETA el valor como si ya estuviera en ella y devuelve un `timestamptz`;
al castear a `date` se lo mira en la zona de la sesión (UTC), así que el
resultado quedaba 5 horas ADELANTE. Cualquier movimiento cargado después de las
14:00 de Ecuador se iba al día siguiente — por eso el link andaba a la mañana y
no a la tarde.

La distinción importa: en `mov_doble.fecha_creacion`, que es TIMESTAMPTZ, la
conversión simple es la CORRECTA. La misma línea está bien en un lado y mal en
el otro según el tipo de la columna.
"""
from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.informes import eventos  # noqa: E402


def _sql_de(func) -> str:
    return " ".join(inspect.getsource(func).split())


def test_el_dia_del_banco_se_convierte_desde_utc():
    """`fecha_crea` es naive-UTC: primero se declara UTC y después se pasa a
    Ecuador. Sin la primera conversión el día sale +1."""
    sql = _sql_de(eventos.transacciones)
    assert "((fecha_crea AT TIME ZONE 'UTC') AT TIME ZONE 'America/Guayaquil')" in sql


def test_no_queda_ninguna_conversion_suelta_sobre_fecha_crea():
    """El candado: cualquier `fecha_crea AT TIME ZONE 'America/Guayaquil'` que
    no venga precedido de la conversión desde UTC es el mismo bug de vuelta."""
    sql = _sql_de(eventos.transacciones)
    for m in re.finditer(r"fecha_crea AT TIME ZONE '([^']+)'", sql):
        assert m.group(1) == "UTC", (
            "fecha_crea es timestamp SIN zona: la primera conversión tiene que "
            "ser AT TIME ZONE 'UTC'")


def test_la_columna_con_zona_no_se_convierte_dos_veces():
    """`mov_doble.fecha_creacion` SÍ es TIMESTAMPTZ. Ahí la conversión simple
    es la correcta, y agregarle un 'UTC' la rompería al revés."""
    sql = _sql_de(eventos.movimientos_doble) if hasattr(
        eventos, "movimientos_doble") else " ".join(
        inspect.getsource(eventos).split())
    assert ("(fecha_creacion AT TIME ZONE 'America/Guayaquil')::date" in sql
            or "fecha_creacion" not in sql)
