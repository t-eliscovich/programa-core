"""Helpers para escribir movimientos en `scintela.caja` con saldo running.

Mismo patrón que `bank_helpers.py` pero contra la tabla `caja`. La caja es
mono-cuenta (no hay no_banco / no_cta), así que es más simple.

Convenciones de signo (heredadas de BANCOS.PRG.PASOCAJA):
    SIGNO = -1 si tipo == 'S'  (salida)
    SIGNO = +1 en cualquier otro caso — 'E' (entrada) y el 'CB' que escribe
            `caja.queries.crear()`, que también suma. Ver `signo_tipo`.

`importe` siempre positivo. El signo vive en `tipo`.

El `saldo` de una fila es el ACUMULADO hasta ahí, así que el ledger es una
CADENA: una fila borrada o insertada en el medio corre el saldo de TODAS las
de abajo. Por eso el módulo tiene las dos piezas que ya tenía el banco:

  · `recompute_saldos_desde()` — re-encadena hacia adelante desde un ancla.
  · `assert_cadena_intacta()`  — el CANDADO: una transacción que dejaría la
    cadena partida no llega a commitear. TMT 2026-08-14.
"""
from __future__ import annotations

from datetime import date

import db


def signo_tipo(tipo: str) -> int:
    """+1 entra plata a la caja, −1 sale. LA regla de signo de la caja.

    ⭐ TMT 2026-08-14 — antes era `+1 sólo si 'E'`, todo lo demás −1. Para lo
    que este módulo ESCRIBE daba igual (`insert_movimiento_caja` rechaza
    cualquier tipo que no sea E o S), pero no da igual para lo que este módulo
    LEE y re-encadena: `caja.queries.crear()` admite además `'CB'` (cobro con
    cheque) y lo estampa SUMANDO — igual que el trigger `trg_caja_set_saldo`
    (mig 0022) y que las dos lecturas de las que cuelga la plata,
    `caja.queries.saldo_actual()` e `informes.queries.salcaj()`, las dos
    `E→+, S→−, resto→+`. Con la regla vieja el candado de más abajo habría
    marcado como quiebre CADA fila 'CB' bien cargada, y el walk-forward las
    habría "corregido" al revés, restando plata que sí entró.

    La caja tenía cuatro definiciones del signo conviviendo; ahora hay una y
    las otras tres coinciden con ella. [[feedback_espejo_clasificador_compartido]]
    """
    return -1 if (tipo or "").upper().strip() == "S" else 1


def _delta_firmado(tipo: str, importe) -> float:
    """Lo que ESTA fila mueve el saldo. Espejo exacto de `_sql_delta_firmado`.

    El `importe` va CRUDO, sin `abs()`: las filas legacy del 17→30/04 llegaron
    del dBase con el importe en NEGATIVO, y las dos lecturas de las que sale la
    plata publicada (`caja.queries.saldo_actual()` y `salcaj()`) las suman tal
    cual. Con `abs()` el re-encadenado las estampaba para el otro lado y el
    running dejaba de cerrar contra esas lecturas — está escrito en el
    docstring de `caja.queries.recomputar_saldos`, que ya había sacado el
    `abs()` por eso mismo. TMT 2026-08-14.
    """
    return signo_tipo(tipo) * float(importe or 0)


def saldo_actual(conn=None) -> float:
    """Saldo más reciente de caja. 0.0 si vacía."""
    row = db.fetch_one(
        """
        SELECT COALESCE(saldo, 0) AS saldo
          FROM scintela.caja
         ORDER BY fecha DESC NULLS LAST, id_caja DESC
         LIMIT 1
        """,
        conn=conn,
    )
    return float(row["saldo"]) if row else 0.0


def _saldo_previo(
    conn, *, fecha: date, excluir_id: int | None = None,
    solo_dias_anteriores: bool = False,
) -> float:
    """Saldo anterior al movimiento que se está por insertar.

    `solo_dias_anteriores=True` ancla ESTRICTO en `fecha < ancla` (cierre del
    día anterior) — lo necesita recompute_saldos_desde(ancla_fecha=...) porque
    el walk re-aplica todas las filas de la fecha ancla. Mismo bug/fix que
    bank_helpers TMT 2026-06-11 (backdated recompute).
    """
    if solo_dias_anteriores:
        cond_fecha = "(fecha < %s)"
        params_fecha: tuple = (fecha,)
    else:
        cond_fecha = (
            "((fecha < %s) OR (fecha = %s AND (%s::int IS NULL "
            "OR id_caja < %s::int)))"
        )
        params_fecha = (fecha, fecha, excluir_id, excluir_id)
    row = db.fetch_one(
        f"""
        SELECT COALESCE(saldo, 0) AS saldo
          FROM scintela.caja
         WHERE {cond_fecha}
         ORDER BY fecha DESC, id_caja DESC
         LIMIT 1
        """,
        params_fecha,
        conn=conn,
    )
    return float(row["saldo"]) if row else 0.0


def insert_movimiento_caja(
    conn,
    *,
    fecha: date,
    tipo: str,
    importe: float,
    concepto: str,
    clave: str | None = None,
    id_cheque: int | None = None,
    usuario: str = "web",
) -> dict:
    """Inserta un movimiento de caja con saldo running calculado.

    Devuelve `{id_caja, saldo_nuevo, saldo_anterior, signo}`.

    `tipo` debe ser 'E' (entrada) o 'S' (salida). Importe siempre positivo.
    El signo se aplica internamente.

    Si la fila cae al MEDIO del ledger (fecha vieja), re-encadena lo que queda
    debajo antes de terminar, y en cualquier caso cierra pasando por el
    candado: si esta operación dejara la cadena partida, revienta y el
    `db.tx()` del caller hace rollback. TMT 2026-08-14.
    """
    tipo_norm = (tipo or "").upper().strip()
    if tipo_norm not in ("E", "S"):
        raise ValueError(f"tipo debe ser 'E' o 'S' (recibido: {tipo!r})")
    importe_f = float(importe or 0)
    if importe_f <= 0:
        raise ValueError(f"importe debe ser > 0 (recibido: {importe!r})")
    importe_abs = importe_f

    signo = signo_tipo(tipo_norm)
    saldo_anterior = _saldo_previo(conn, fecha=fecha)
    saldo_nuevo = round(saldo_anterior + signo * importe_abs, 2)

    row = db.execute_returning(
        """
        INSERT INTO scintela.caja
            (fecha, tipo, importe, concepto, saldo, clave,
             id_cheque, usuario_crea)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id_caja
        """,
        (
            fecha,
            tipo_norm,
            importe_abs,
            (concepto or "").strip()[:100],
            saldo_nuevo,
            (clave or None) and clave[:3].upper(),
            id_cheque,
            usuario[:50],
        ),
        conn=conn,
    ) or {}

    # TMT 2026-08-14 — si este INSERT NO cayó al final del ledger (hay filas
    # con fecha posterior), las de abajo tienen un saldo calculado sobre un
    # estado que ya no existe: quedaron corridas por este importe. Se
    # re-encadenan ACÁ, en la misma transacción, igual que hace el banco. Sin
    # esto el candado de abajo frenaría una carga backdated perfectamente
    # legítima — el candado tiene que proteger de un ledger partido, no de
    # cargar una fecha vieja.
    id_nuevo = row.get("id_caja") or 0
    hay_posteriores = db.fetch_one(
        """
        SELECT 1 AS hay
          FROM scintela.caja
         WHERE (fecha > %s OR (fecha = %s AND id_caja > %s))
         LIMIT 1
        """,
        (fecha, fecha, id_nuevo),
        conn=conn,
    )
    if hay_posteriores:
        recompute_saldos_desde(conn, ancla_fecha=fecha)
        # El saldo que devolvemos tiene que ser el que quedó GUARDADO, no el
        # que calculamos antes de re-encadenar. [[feedback_mostrar_lo_guardado]]
        rel = db.fetch_one(
            "SELECT saldo FROM scintela.caja WHERE id_caja = %s",
            (id_nuevo,), conn=conn,
        )
        if rel and rel.get("saldo") is not None:
            saldo_nuevo = round(float(rel["saldo"]), 2)

    # ⭐ CANDADO DE COMMIT — ver `CadenaCajaRotaError`.
    assert_cadena_intacta(
        conn, desde_fecha=fecha,
        contexto=f"Alta de caja {tipo_norm} {(concepto or '').strip()[:30]}")

    return {
        "id_caja": row.get("id_caja"),
        "saldo_nuevo": saldo_nuevo,
        "saldo_anterior": saldo_anterior,
        "signo": signo,
        "importe": importe_abs,
    }


def recompute_saldos_desde(
    conn,
    *,
    ancla_id: int | None = None,
    ancla_fecha: date | None = None,
) -> int:
    """Walk-forward: recalcula `saldo` para toda fila >= ancla.

    Mismo contrato que bank_helpers.recompute_saldos_desde pero contra caja.
    Sólo necesario para correcciones administrativas. Devuelve cantidad de
    filas actualizadas.

    Cierra con el candado (`assert_cadena_intacta`) acotado a la fecha ancla:
    si el walk hacia adelante dejó una costura, no se commitea. TMT 2026-08-14.
    """
    # Blindaje TMT 2026-05-13: NUNCA aceptar ambos None sin un flag explícito.
    # Pasar sin ancla destruye el saldo opening histórico — mismo bug que
    # tuvo bank_helpers (#79, #80).
    if ancla_id is None and ancla_fecha is None:
        raise ValueError(
            "recompute_saldos_desde de caja requiere ancla_id o ancla_fecha. "
            "Sin ancla, se rebobina desde 0 y se pierde el opening histórico."
        )
    if ancla_id is not None:
        # ⭐ TMT 2026-08-03 — EL ANCLA VA POR (fecha, id), NO POR id.
        # Mismo bug que bank_helpers: el saldo de arranque salía por orden de
        # INSERCIÓN y el walk de abajo camina por FECHA, así que una fila
        # BACKDATED partía la cadena en dos segmentos incoherentes. En bancos
        # eso valió 155.187,31 de patrimonio fantasma el 03/08/2026 y rompió
        # la conciliación de Pichincha. [[project_2026_08_03_utilidad_37k]]
        anc = db.fetch_one(
            "SELECT fecha FROM scintela.caja WHERE id_caja = %s",
            (ancla_id,),
            conn=conn,
        )
        if anc and anc.get("fecha"):
            ancla_fecha_del_id = anc["fecha"]
        else:
            # Ancla borrada: ensanchamos el walk, nunca lo achicamos.
            _mn = db.fetch_one(
                "SELECT MIN(fecha) AS fecha FROM scintela.caja WHERE id_caja >= %s",
                (ancla_id,),
                conn=conn,
            )
            if not _mn or not _mn.get("fecha"):
                return 0
            ancla_fecha_del_id = _mn["fecha"]
        row = db.fetch_one(
            """
            SELECT COALESCE(saldo, 0) AS saldo
              FROM scintela.caja
             WHERE (fecha, id_caja) < (%s::date, %s)
             ORDER BY fecha DESC, id_caja DESC
             LIMIT 1
            """,
            (ancla_fecha_del_id, ancla_id),
            conn=conn,
        )
        saldo = float(row["saldo"]) if row else 0.0
        cond_inicio = "(fecha, id_caja) >= (%s::date, %s)"
        params_inicio: tuple = (ancla_fecha_del_id, ancla_id)
        fecha_guarda = ancla_fecha_del_id
    else:  # ancla_fecha
        # TMT 2026-06-11: ancla = cierre del día ANTERIOR (estricto), el walk
        # re-aplica todas las filas de la fecha ancla. Ver bank_helpers.
        saldo = _saldo_previo(conn, fecha=ancla_fecha, solo_dias_anteriores=True)
        cond_inicio = "fecha >= %s::date"
        params_inicio = (ancla_fecha,)
        fecha_guarda = ancla_fecha

    rows = db.fetch_all(
        f"""
        SELECT id_caja, fecha, tipo, importe
          FROM scintela.caja
         WHERE {cond_inicio}
         ORDER BY fecha, id_caja
        """,
        params_inicio,
        conn=conn,
    ) or []

    n = 0
    for r in rows:
        saldo = round(saldo + _delta_firmado(r["tipo"], r["importe"]), 2)
        db.execute(
            "UPDATE scintela.caja "
            "SET saldo = %s, fecha_modifica = CURRENT_TIMESTAMP "
            "WHERE id_caja = %s",
            (saldo, r["id_caja"]),
            conn=conn,
        )
        n += 1

    # ⭐ CANDADO DE COMMIT — ver `CadenaCajaRotaError`. Si el walk hacia
    # adelante dejó una costura (el ancla cayó al medio de un tramo con las
    # fechas fuera de orden), esto lo frena ACÁ en vez de que aparezca dentro
    # de tres semanas en un arqueo que no cierra por una plata sin dueño.
    assert_cadena_intacta(
        conn, desde_fecha=fecha_guarda, contexto="Re-encadenado de caja")
    return n


# ---------------------------------------------------------------------------
# EL CANDADO DE LA CADENA
# ---------------------------------------------------------------------------


def _sql_delta_firmado(alias: str = "") -> str:
    """`_delta_firmado` escrito en SQL. Hay un test que clava que dan lo mismo.

    Misma regla que `caja.queries.saldo_actual()`, que `salcaj()` y que el
    trigger `trg_caja_set_saldo`: sólo `'S'` resta. Escribirla dos veces
    (Python y SQL) es la única forma de tenerla en el WHERE de una window
    function; que no se separen es lo que cuida el test.
    """
    a = f"{alias}." if alias else ""
    return (f"CASE WHEN UPPER(TRIM({a}tipo)) = 'S' "
            f"THEN -{a}importe ELSE {a}importe END")


def contar_quiebres(
    conn=None,
    *,
    desde_fecha: date | None = None,
    apertura: float | None = None,
) -> list[dict]:
    """Filas donde el `saldo` guardado NO se movió por su delta firmado.

    Camina en el MISMO orden en que lee la caja todo el resto del sistema
    —`(fecha, id_caja)`—, que es el orden de `saldo_actual()` y del arqueo. La
    ventana se calcula sobre TODAS las filas y recién después se filtra por
    `desde_fecha`: si filtráramos antes, la primera fila de la ventana se
    quedaría sin anterior y el quiebre del borde se perdería.

    ⭐ LA PRIMERA FILA — la decisión, que NO es la del banco por herencia.
    `bank_helpers.contar_quiebres` filtra `saldo_prev IS NOT NULL`, o sea
    arranca en la SEGUNDA fila: si la más vieja del banco está mal estampada,
    el candado no la ve nunca (nos costó una sesión con una ND huérfana de
    Pichincha, ver `AperturaDistintaError`). Ahí ese hueco SÍ es un hueco,
    porque el banco tiene una apertura afirmada aparte —`scintela.banco_apertura`,
    de donde `saldo_bancos()` deriva la plata— contra la cual la primera fila
    se podría contrastar.

    **La caja no tiene esa tabla, y por eso acá el hueco es de otra naturaleza:
    en caja la primera fila ES la declaración de la apertura.** Tanto
    `caja.queries.saldo_actual()` como `informes.queries.salcaj()` calculan el
    opening como `saldo(primera fila) − delta(primera fila)`; no hay ningún
    segundo número que diga otra cosa, así que no existe con qué desmentirla.
    Un chequeo de la primera fila contra sí misma pasaría SIEMPRE: sería un
    candado que se ve igual estando puesto o no.
    [[feedback_dato_a_la_vista_mata_al_aviso]]

    Así que se replica el arranque en la segunda fila, pero NO en silencio: el
    día que la caja tenga su apertura afirmada (una `caja_apertura`, o la foto
    de cierre del mes, o simplemente alguien que la mire contra el arqueo
    físico), se pasa por `apertura=` y la primera fila entra al chequeo como
    cualquier otra. El agujero queda tapado por el mismo camino que el banco
    ya usa para su `reencadenar_retro(apertura=...)`: un número que una
    PERSONA afirma, no uno que se lee de la fila que justamente puede estar
    mal. TMT 2026-08-14.
    """
    ap = None if apertura is None else round(float(apertura), 2)
    return db.fetch_all(
        f"""
        WITH w AS (
          SELECT id_caja, fecha, tipo, concepto, importe, saldo,
                 {_sql_delta_firmado('c')} AS sgn,
                 COALESCE(
                   LAG(saldo) OVER (ORDER BY fecha, id_caja),
                   %s::numeric
                 ) AS saldo_prev,
                 LAG(fecha)    OVER (ORDER BY fecha, id_caja) AS fecha_prev,
                 LAG(concepto) OVER (ORDER BY fecha, id_caja) AS concepto_prev
            FROM scintela.caja c
           WHERE saldo IS NOT NULL
        )
        SELECT * FROM w
         WHERE saldo_prev IS NOT NULL
           AND ABS((saldo - saldo_prev) - sgn) > 0.02
           AND (%s::date IS NULL OR fecha >= %s::date)
         ORDER BY fecha, id_caja
        """,
        (ap, desde_fecha, desde_fecha),
        conn=conn,
    ) or []


class CadenaCajaRotaError(RuntimeError):
    """Un write dejó el running `saldo` de la caja partido. Aborta la tx.

    ⭐ TMT 2026-08-14. Es el mismo candado que el banco tiene desde el 04/08
    (`bank_helpers.CadenaRotaError`), traído a la caja mientras todavía no
    muerde: hoy ninguna pantalla publica el saldo de caja leyendo la columna
    —`salcaj()` lo deriva sumando— así que una cadena partida no mueve el
    patrimonio. Pero es la MISMA trampa esperando: una fila borrada o
    insertada en el medio corre el saldo de todas las de abajo, y el arqueo de
    la pantalla de caja empieza a mostrar una plata que no está, sin que nadie
    se entere hasta que alguien cuenta los billetes.

    Se levanta desde `assert_cadena_intacta`; como todos los writes de caja
    viven adentro de un `db.tx()`, hace ROLLBACK de la operación entera.
    Preferimos que la persona vea "no pude guardar esto" a que la caja mienta
    en silencio.

    Se llama distinto que la del banco a propósito: son dos cadenas
    independientes y quien la catchea tiene que saber cuál de las dos se
    rompió.
    """


def assert_cadena_intacta(
    conn=None,
    *,
    desde_fecha: date | None = None,
    apertura: float | None = None,
    contexto: str = "",
) -> None:
    """Candado de commit: si el write dejó un quiebre, revienta y rollback.

    `desde_fecha` acota a lo que ESTE write pudo tocar. Sin ese recorte, un
    quiebre histórico que todavía no se limpió bloquearía una carga de hoy —
    o sea el candado no se podría prender hasta terminar de planchar la
    historia, y mientras tanto la oficina no podría trabajar. Mismo criterio
    que el del banco.
    """
    rotas = contar_quiebres(conn, desde_fecha=desde_fecha, apertura=apertura)
    if not rotas:
        return
    r = rotas[0]
    detalle = (
        f"id {r['id_caja']} {r['fecha']} "
        f"{(r.get('tipo') or '').strip()} "
        f"{(r.get('concepto') or '')[:30]}: el saldo saltó "
        f"{float(r['saldo']) - float(r['saldo_prev']):,.2f} "
        f"cuando el movimiento vale {float(r['sgn']):,.2f}"
    )
    raise CadenaCajaRotaError(
        f"{contexto or 'Movimiento de caja'}: la operación habría dejado el "
        f"saldo de la caja partido en {len(rotas)} punto(s) — {detalle}. "
        f"No se guardó nada. El arqueo lee ese saldo corrido, así que "
        f"guardarlo habría mostrado en pantalla una plata que no está."
    )
