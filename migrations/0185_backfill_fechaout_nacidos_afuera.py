"""Completa `fechaout` en los cheques que NACIERON fuera de cartera.

TMT 2026-08-10 (dueña: *"ok dale"*). Cierra el tema abierto por `00d11d9`.

QUÉ PASÓ. El 05/08 el depósito pasó a escribir la fecha de salida en
`fechaout` en las dos rutas que SACAN un cheque de cartera. La tercera
—`cheques.queries.crear()`, el cheque que nace ya depositado (90/91 → 'B') o
ya cobrado en caja (99 → 'C')— siguió escribiendo sólo `fechaing` hasta el fix
del 10/08. Estas son las filas que quedaron en el medio: entraron y salieron
el mismo día, y les falta la fecha de salida.

MEDIDO EN PRODUCCIÓN EL 10/08/2026:

    stat  banco   n     total        fecha a escribir
    B     90      104   116.459,12   05→08/08   (= fechaing)
    C     99       13    17.351,96   06→07/08   (= fecha, no tienen fechaing)

En las 117 filas `fecha`, `fechaing` (cuando existe) y `fecha_crea::date` son
EL MISMO DÍA — no hay ninguna decisión que tomar sobre qué fecha va. Ninguna
futura, ninguna sin fecha de dónde sacarla.

NO MUEVE NINGÚN NÚMERO, y no sólo porque se midió: es estructural. El TOTC
as-of exige `fecha_crea::date <= as_of`, y la `fechaout` que se escribe es la
del día de `fecha_crea` ⇒ para que una fila cambiara de lado necesitaría
`fechaout > as_of`, o sea `fecha_crea > as_of`, o sea estar excluida.
`balance_components_as_of` (la de los snapshots y el cierre) ni siquiera mira
`fechaout`. Lo único que cambia a la VISTA: la columna "Depositado" del estado
de cuenta de 12 clientes se llena en las 13 filas de efectivo (hoy vacía); el
orden no cambia porque en las 13 `fechad = fecha`.

NO TOCA `fechaing`: el resumen de cobranza del día se imprime para
contabilidad. (Y no le hace falta: `SQL_DIA_INGRESO` no lee `fechaing` en las
filas de PC — el día de ingreso sale de `fecha_recibido`.)

POR QUÉ NO FILTRA POR `usuario_modifica IS NULL`, que es como se midió: esa
condición pregunta por el CAMINO ("nadie lo tocó desde que nació") y la
lección de este tema es preguntar por el ESTADO. Que alguien le haya editado
el concepto en el medio no cambia que el cheque está fuera de cartera y le
falta la fecha de salida. Medido el 10/08: da las mismas 117 con y sin el
filtro.

FRENO. Una fila sin ninguna fecha de dónde sacar la salida NO se escribe: se
saltea y se cuenta. A diferencia del script que esto reemplaza, acá no se
aborta —una migración que falla deja el deploy sin reiniciar—, y saltearla no
la esconde: el vigía `/admin/health/deposito-sin-fechaout` la sigue marcando
en HIGH hasta que alguien la mire. Medido: hoy son 0.

Idempotente: sólo toca filas con `fechaout IS NULL`.
"""

from __future__ import annotations

import os

CORTE = "2026-08-05"
USUARIO = "mig-0185"

#: Estados en los que el cheque TODAVÍA está en cartera. Espejo de
#: `modules.cheques.queries.STATS_EN_CARTERA` — se repite acá a propósito: una
#: migración es una foto de lo que valía el día que corrió y no puede cambiar
#: de significado porque alguien edite una constante seis meses después.
EN_CARTERA = ("Z", "P", "D", "1", "2", "3")

SELECT_OBJETIVO = """
    SELECT id_cheque, stat, no_banco, importe,
           COALESCE(fechaing, fecha) AS fechaout_nueva
      FROM scintela.cheque
     WHERE fecha_crea::date >= %s
       AND UPPER(COALESCE(stat, '')) <> ALL (%s)
       AND fechaout IS NULL
     ORDER BY stat, id_cheque
"""


def run(conn) -> None:
    cur = conn.cursor()
    cur.execute(SELECT_OBJETIVO, (CORTE, list(EN_CARTERA)))
    filas = cur.fetchall()  # (id_cheque, stat, no_banco, importe, fechaout_nueva)

    con_fecha = [f for f in filas if f[4] is not None]
    sin_fecha = [f for f in filas if f[4] is None]

    for id_cheque, _stat, _banco, _importe, nueva in con_fecha:
        cur.execute(
            """
            UPDATE scintela.cheque
               SET fechaout = %s,
                   usuario_modifica = %s,
                   fecha_modifica = CURRENT_TIMESTAMP
             WHERE id_cheque = %s AND fechaout IS NULL
            """,
            (nueva, USUARIO, id_cheque),
        )
    cur.close()

    if os.environ.get("MIGRATE_VERBOSE"):
        por_grupo: dict[tuple, list] = {}
        for f in con_fecha:
            por_grupo.setdefault((f[1], f[2]), []).append(f)
        for (stat, banco), fs in sorted(por_grupo.items()):
            total = sum(float(f[3] or 0) for f in fs)
            print(f"    {stat} · banco {banco}: {len(fs)} fila(s), {total:,.2f}")
        print(f"    total completadas: {len(con_fecha)}")
        if sin_fecha:
            print(f"    ⚠ SALTEADAS (sin fecha de dónde sacar la salida): "
                  f"{[f[0] for f in sin_fecha][:10]}")
