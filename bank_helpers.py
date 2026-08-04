"""Helpers para escribir movimientos en `scintela.transacciones_bancarias` y
mantener el saldo running consistente.

dBase paridad — `transacciones_bancarias.saldo` es **stored running balance**
por (no_banco, no_cta). Cada INSERT al tail computa
`saldo = saldo_previo + signo * importe`. Cualquier INSERT al medio o
DELETE/UPDATE de fila no-tail dispara walk-forward recompute.

Convenciones de signo (heredadas del legacy BANCOS.PRG y del schema):
    SIGNO = +1 si documento ∈ ('DE', 'TR', 'XX', 'NC', 'IN')   — entradas
    SIGNO = -1 caso contrario (CH, ND, GS, PA, etc.)            — salidas

`importe` se almacena SIEMPRE en valor absoluto positivo. El signo vive en el
documento. Si pasás importe negativo se trata como error de carga (raise).

Uso típico — desde un crear/editar/transicionar dentro de un `db.tx()`:

    with db.tx() as conn:
        bank_helpers.insert_movimiento_bancario(
            conn,
            no_banco=1, no_cta=None,
            fecha=date.today(),
            documento='DE',
            importe=ch['importe'],
            concepto=f"Dep. cheque {ch['no_cheque']}",
            prov=ch['codigo_cli'],
            numreferencia=ch['id_cheque'],
            usuario=g.user['username'],
        )

Walk-forward (sólo cuando hay correcciones administrativas que tocan el
medio del ledger; flujo normal append-only no lo necesita):

    bank_helpers.recompute_saldos_desde(
        conn,
        no_banco=1, no_cta=None,
        ancla_id=12345,  # walk forward desde id_transaccion >= ancla
    )
"""
from __future__ import annotations

from datetime import date

import db
from filters import today_ec

# Documentos que SUMAN al saldo (entradas). Cualquier otro RESTA.
#   DE = depósito de cheque
#   TR = transferencia recibida
#   XX = ajuste positivo
#   NC = nota de crédito
#   IN = ingreso varios
DOCS_ENTRADA: tuple[str, ...] = ("DE", "TR", "XX", "NC", "IN")


def signo_documento(documento: str) -> int:
    """+1 si entra plata al banco, -1 si sale. Usado por el running saldo."""
    return 1 if (documento or "").upper().strip() in DOCS_ENTRADA else -1


_LEGACY_USUARIOS = frozenset({
    "", "dbf-import", "asinfo-backfill", "dbase-sync",
})


def _es_fila_legacy(usuario_crea) -> bool:
    """¿La fila viene del DBF/sync o es de un INSERT web nuevo?

    Legacy = importe SIGNED (NDs reversos vienen con +importe legítimo).
    No legacy = bank_helpers convention, importe ABS, sign por doc.
    """
    return (usuario_crea or "").strip().lower() in _LEGACY_USUARIOS


def _signed_delta(documento: str, importe: float, usuario_crea: str = "") -> float:
    """Delta firmado a aplicar al saldo: signo_documento × importe.

    TMT 2026-06-03 audit fix v2: el chain dBase legacy aplica
    `signo_documento × importe` SIN abs, lo que unifica todas las
    convenciones:
      - ND +44091 (egress): -1 * 44091 = -44091 → saldo baja ✓
      - ND -44091 (reverso): -1 * -44091 = +44091 → saldo sube ✓
      - DE +1500 (deposit): +1 * 1500 = +1500 → saldo sube ✓
      - Web ND +50 (importe ABS por convención bank_helpers): -1 * 50 = -50 ✓

    Validado contra el chain real de DBF: pairs 24346/24347 (ND ±44091)
    cancelan correctamente con esta regla.

    `usuario_crea` se mantiene en la firma por compat con callers existentes
    pero no afecta el resultado.
    """
    imp = float(importe or 0)
    return signo_documento(documento) * imp


def _saldo_previo(
    conn,
    *,
    no_banco: int,
    no_cta: str | None,
    fecha: date,
    excluir_id: int | None = None,
    solo_dias_anteriores: bool = False,
) -> float:
    """Saldo anterior al movimiento que se está por insertar.

    Se ordena por (fecha, id_transaccion) — el id es el desempate cuando hay
    varios movimientos en el mismo día. La fila excluida (si se pasa) se
    saltea — útil cuando estamos haciendo walk-forward y no queremos que la
    fila actual entre dos veces.

    Bug TMT 2026-05-11: si la fila más reciente del banco tenía `saldo=NULL`
    (depósitos hechos con el código viejo antes del fix), volvía 0 y el
    nuevo saldo se computaba desde cero — distinto de la realidad. Fix:
    saltear filas con saldo NULL y, si TODAS las anteriores son NULL,
    fallback a SUM firmado por documento (mismo criterio que `saldo_bancos`
    en `informes/queries.py`). Así los depósitos nuevos quedan ancla­dos al
    saldo real aunque haya filas viejas mal escritas.

    Bug TMT 2026-06-11 (backdated recompute): con `excluir_id=None` la
    condición incluía TODAS las filas de la propia `fecha` — si la llamaba
    `recompute_saldos_desde(ancla_fecha=...)` después de un insert backdated,
    el ancla terminaba siendo la fila recién insertada y la cadena corría un
    día-neto por insert (hero Pichincha llegó a 462.916,76). Fix:
    `solo_dias_anteriores=True` ancla ESTRICTO en `fecha < ancla` (cierre del
    día anterior), que es lo que el walk-forward necesita porque después
    re-aplica todas las filas de la fecha ancla con `_signed_delta`.
    """
    if solo_dias_anteriores:
        cond_fecha = "(fecha < %s)"
        params_fecha: tuple = (fecha,)
    else:
        cond_fecha = (
            "((fecha < %s) OR (fecha = %s AND (%s::int IS NULL "
            "OR id_transaccion < %s::int)))"
        )
        params_fecha = (fecha, fecha, excluir_id, excluir_id)
    row = db.fetch_one(
        f"""
        SELECT saldo
          FROM scintela.transacciones_bancarias
         WHERE no_banco = %s
           AND ((%s)::text IS NULL OR no_cta = (%s)::text OR no_cta IS NULL)
           AND saldo IS NOT NULL
           AND {cond_fecha}
         ORDER BY fecha DESC, id_transaccion DESC
         LIMIT 1
        """,
        (no_banco, no_cta, no_cta, *params_fecha),
        conn=conn,
    )
    if row and row.get("saldo") is not None:
        return float(row["saldo"])

    # No hay ningún saldo running válido antes del ancla → reconstruir
    # con SUM firmado por documento de TODAS las filas anteriores
    # (replica el fallback de `saldo_bancos`).
    fallback = db.fetch_one(
        f"""
        SELECT COALESCE(SUM(
                 CASE WHEN UPPER(TRIM(documento)) IN ('CH','ND','RE','GS','PA')
                      THEN -importe
                      ELSE  importe
                 END
               ), 0) AS saldo
          FROM scintela.transacciones_bancarias
         WHERE no_banco = %s
           AND ((%s)::text IS NULL OR no_cta = (%s)::text OR no_cta IS NULL)
           AND {cond_fecha}
        """,
        (no_banco, no_cta, no_cta, *params_fecha),
        conn=conn,
    )
    return float(fallback["saldo"]) if fallback else 0.0


def insert_movimiento_bancario(
    conn,
    *,
    no_banco: int,
    no_cta: str | None,
    fecha: date,
    documento: str,
    importe: float,
    concepto: str,
    prov: str | None = None,
    numreferencia: int | None = None,
    fechad: date | None = None,
    stat: str = "A",
    clave: str | None = None,
    usuario: str = "web",
    permitir_signed: bool = False,
) -> dict:
    """Inserta un movimiento bancario con saldo running calculado.

    Devuelve el dict {id_transaccion, saldo_nuevo}.

    `importe` siempre positivo. El signo se aplica internamente según
    `documento`. Errores claros si el caller pasa importe negativo o cero.

    `permitir_signed=True` habilita la convención del FoxPro — el MISMO
    documento con el importe en NEGATIVO para deshacer (TMT 2026-07-31, dueña:
    "dejame cargar con signo negativo o positivo en cualquier lado"). El
    negativo se GUARDA tal cual: `_signed_delta` ya lo maneja (`ND −44.091`
    SUBE el saldo) y así están las 38 filas negativas de PICHINCH.DBF de julio.
    Sólo lo usa el alta MANUAL, donde la persona ve el signo que tipeó; el
    resto de los callers mantiene la guarda de abs.

    El `saldo_nuevo` se persiste en la columna `saldo` (paridad dBase). Si
    insertás al medio (fecha pasada) la fila queda con saldo correcto pero
    las posteriores quedarán mal — usa `recompute_saldos_desde()` después.

    Devuelve `dict` para que el caller pueda enlazar (e.g. setear
    `compra.id_transaccion = id_transaccion`).
    """
    if not no_banco:
        raise ValueError("no_banco requerido para insert_movimiento_bancario")
    if not documento:
        raise ValueError("documento requerido")
    importe_f = float(importe or 0)
    if importe_f == 0:
        raise ValueError(f"importe debe ser != 0 (recibido: {importe!r})")
    # TMT 2026-06-03 audit fix: la convención bank_helpers exige importe ABS.
    # Si recibimos negativo el caller está confundido (probablemente está mezclando
    # convenciones legacy DBF). Mejor fallar explícito que dejar saldo corrupto.
    if importe_f < 0 and not permitir_signed:
        raise ValueError(
            f"importe debe ser positivo (abs). Recibido: {importe!r}. "
            f"El signo lo determina el documento ({documento!r}). "
            f"Si necesitás convención DBF legacy (signed), pasá permitir_signed=True."
        )
    # Aceptamos importe signed (legacy) o abs (nuevo). El delta unificado
    # lo computa _signed_delta. El valor que almacenamos en la columna
    # `importe` mantiene el SIGNO del caller para preservar la convención
    # mixta — lo que pasa Programa Core legacy queda signed, lo que pasa
    # bank_helpers nuevo queda abs.
    # Con permitir_signed el negativo se GUARDA (convención FoxPro); sin él,
    # se guarda la magnitud y la dirección la lleva el documento.
    importe_abs = importe_f if permitir_signed else abs(importe_f)
    signo = signo_documento(documento)
    # TMT 2026-06-03 audit fix: advisory lock por banco para serializar
    # inserts concurrentes. Sin esto, dos inserts en paralelo computaban
    # el mismo saldo_anterior y rompían la cadena.
    # Clave: hashtext(banco:no_cta) — un banco por lock, no entre bancos.
    # try/except defensivo: tests pueden usar mocks de cursor sin rowcount.
    lock_key = f"banco_running:{no_banco}:{no_cta or ''}"
    try:
        db.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (lock_key,), conn=conn,
        )
    except (AttributeError, TypeError):
        # Cursor mock sin rowcount (tests) — el lock no aplica.
        pass
    saldo_anterior = _saldo_previo(
        conn, no_banco=no_banco, no_cta=no_cta, fecha=fecha,
    )
    # TMT 2026-06-03: pasar usuario_crea='web' explícito para que _signed_delta
    # use convención bank_helpers (importe abs, sign por doc). Si llamáramos
    # sin usuario_crea, default '' = legacy = respetar importe sign — y los
    # callers web pasan importe ABS, lo que daría signo equivocado para CH/ND.
    saldo_nuevo = round(saldo_anterior + _signed_delta(documento, importe_f, usuario), 2)

    # Auto-extraer prov del concepto si el caller no lo pasó.
    # Cubre el caso típico "1 ch.LTM" → prov="LTM". Mejora cobertura
    # del JOIN con scintela.cliente en la conciliación. Fix Tamara
    # 2026-05-23. Solo cuando prov venga vacío — el caller explícito gana.
    if not prov:
        try:
            import re as _re
            m = _re.search(r"(?:^|\s)(?:\d+\s+)?(?:ch\.?|tr\.?|nc\.?|trf\.?|dep\.?\s*ch\.?)\s*([A-Za-z]{3,5})\b",
                           (concepto or ""), _re.IGNORECASE)
            if m:
                prov = m.group(1).upper().strip()
        except Exception:
            pass  # fail-graceful

    row = db.execute_returning(
        """
        INSERT INTO scintela.transacciones_bancarias
            (fecha, documento, concepto, fechad, importe, saldo, stat,
             no_banco, no_cta, prov, numreferencia, clave, usuario_crea)
        VALUES (%s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s)
        RETURNING id_transaccion
        """,
        (
            fecha,
            (documento or "").upper().strip()[:5],
            (concepto or "").strip()[:50],
            fechad,
            importe_abs,
            saldo_nuevo,
            (stat or "A")[:2],
            no_banco,
            (no_cta or None) and no_cta[:20],
            (prov or None) and prov[:5],
            numreferencia,
            (clave or None) and clave[:3],
            usuario[:50],
        ),
        conn=conn,
    ) or {}

    # TMT 2026-06-03 audit fix: si el insert NO está al tail (hay filas con
    # fecha > este insert), las posteriores tienen saldo basado en un estado
    # anterior. Recompute las posteriores para mantener cadena coherente.
    # Esto blinda contra el caso "deposit insertado, luego sync agrega filas
    # del mismo día pero antes" que dejaba el deposit con saldo stale.
    later_row = db.fetch_one(
        """
        SELECT 1 FROM scintela.transacciones_bancarias
         WHERE no_banco = %s
           AND ((%s)::text IS NULL OR no_cta = (%s)::text OR no_cta IS NULL)
           AND (fecha > %s
                OR (fecha = %s AND id_transaccion > %s))
         LIMIT 1
        """,
        (no_banco, no_cta, no_cta, fecha, fecha, row.get("id_transaccion") or 0),
        conn=conn,
    )
    if later_row:
        recompute_saldos_desde(
            conn, no_banco=no_banco, no_cta=no_cta,
            ancla_fecha=fecha,
        )

    # ⭐ TMT 2026-08-04 — CANDADO DE COMMIT. Ver `CadenaRotaError`.
    # Acotado a `fecha` para adelante: un quiebre histórico que todavía no se
    # limpió no tiene por qué frenarle una carga de hoy a la oficina.
    assert_cadena_intacta(
        conn, no_banco=no_banco, no_cta=no_cta, desde_fecha=fecha,
        contexto=f"Alta de {(documento or '').upper().strip()} "
                 f"{(concepto or '').strip()[:30]}")

    return {
        "id_transaccion": row.get("id_transaccion"),
        "saldo_nuevo": saldo_nuevo,
        "saldo_anterior": saldo_anterior,
        "signo": signo,
        "importe": importe_abs,
    }


def recompute_saldos_desde(
    conn,
    *,
    no_banco: int,
    no_cta: str | None = None,
    ancla_id: int | None = None,
    ancla_fecha: date | None = None,
    desde_cero: bool = False,
    dry_run: bool = False,
) -> int | list[dict]:
    """Walk-forward: recalcula `saldo` para toda fila >= ancla.

    Con `dry_run=True` NO escribe: devuelve la lista de filas que tocaría,
    con `saldo_actual` y `saldo_nuevo`, para poder mirarla antes de aplicar.
    Sin `dry_run` devuelve la cantidad de filas actualizadas.

    ⚠️ ATENCIÓN — LEÉ ESTO ANTES DE LLAMAR ESTA FUNCIÓN ⚠️

    Si NO pasás ancla (es decir, `ancla_id=None` Y `ancla_fecha=None`),
    esta función **levanta ValueError** — porque sin ancla, el "saldo
    previo" es 0, y eso DESTRUYE el opening histórico del banco (la plata
    que tenía antes de la primera fila cargada en la DB).

    Bug histórico TMT 2026-05-12: un script de purga llamó la versión
    vieja con ancla=None y Pichincha pasó de 2.280.906 a -917.651,96.
    Tuvimos que escribir scripts/restaurar_saldos_bancos.py para volver
    al estado correcto. NUNCA MÁS.

    Uso correcto:
        # Después de insertar/editar/borrar una fila vieja, walk desde ahí:
        recompute_saldos_desde(conn, no_banco=10, ancla_fecha=date(2026,5,12))
        recompute_saldos_desde(conn, no_banco=10, ancla_id=12345)

    Si REALMENTE necesitás recomputar todo desde 0 (por ejemplo después
    de un re-import del DBF que SÍ trae el opening como primera fila),
    pasá `desde_cero=True` explícitamente. Eso es destructivo de cualquier
    opening implícito; usalo sólo cuando sabés exactamente lo que hacés.

    Devuelve la cantidad de filas actualizadas.
    """
    if not no_banco:
        raise ValueError("no_banco requerido para recompute_saldos_desde")

    # Guarda crítica — TMT 2026-05-12: ver docstring.
    if ancla_id is None and ancla_fecha is None and not desde_cero:
        raise ValueError(
            "recompute_saldos_desde sin ancla destruye el opening histórico "
            "del banco. Pasá ancla_id o ancla_fecha, o si realmente querés "
            "partir de saldo=0 (sólo después de re-importar DBF con opening), "
            "pasá desde_cero=True explícitamente. Ver bug TMT 2026-05-12."
        )

    # Saldo previo al ancla — punto de partida del walk.
    if ancla_id is not None:
        # ⭐ TMT 2026-08-03 — EL ANCLA VA POR (fecha, id), NO POR id.
        #
        # Antes se tomaba el saldo de arranque con `id_transaccion <` (orden
        # de INSERCIÓN) y después se caminaba `ORDER BY fecha, id_transaccion`
        # (orden de FECHA). Mezclar los dos órdenes ROMPE LA CADENA cada vez
        # que entra una fila BACKDATED (id alto, fecha vieja) — que es justo
        # el caso que los llamadores dicen cubrir ("si la fila quedó al
        # medio", matcher_banco.py). Dos daños a la vez:
        #   1) el saldo de arranque salía de la ÚLTIMA fila INSERTADA, que
        #      puede ser de una fecha mucho posterior → la fila vieja quedaba
        #      estampada con un saldo del futuro;
        #   2) `id_transaccion >= ancla_id` dejaba AFUERA del walk las filas
        #      de fecha posterior con id menor → la cadena quedaba partida en
        #      dos segmentos incoherentes.
        #
        # Daño real medido en Pichincha el 03/08/2026: una fila
        # `ND Comisiones e impuestos 17/06-30/07` de **$2,96** creada por la
        # conciliación movió el saldo **+155.187,31**. Como `saldo_bancos()`
        # lee el running GUARDADO de la última fila, el Balance mostró el
        # patrimonio inflado en esa plata (utilidad 37.658 → 193.749 sin que
        # se moviera un peso) y la conciliación de la sesión #60 marcó esa
        # misma diferencia contra el extracto. La conciliación se rompía a
        # sí misma. [[project_2026_08_03_utilidad_37k]]
        anc = db.fetch_one(
            """
            SELECT fecha
              FROM scintela.transacciones_bancarias
             WHERE no_banco = %s
               AND ((%s)::text IS NULL OR no_cta = (%s)::text OR no_cta IS NULL)
               AND id_transaccion = %s
            """,
            (no_banco, no_cta, no_cta, ancla_id),
            conn=conn,
        )
        if anc and anc.get("fecha"):
            ancla_fecha_del_id = anc["fecha"]
        else:
            # El ancla ya no existe (la borraron entre medio). Arrancamos
            # desde la fecha más vieja de lo que sí quedó con id >= ancla_id:
            # ENSANCHA el walk, nunca lo achica. Si no quedó nada, no hay
            # nada que recomputar.
            _mn = db.fetch_one(
                """
                SELECT MIN(fecha) AS fecha
                  FROM scintela.transacciones_bancarias
                 WHERE no_banco = %s
                   AND ((%s)::text IS NULL OR no_cta = (%s)::text OR no_cta IS NULL)
                   AND id_transaccion >= %s
                """,
                (no_banco, no_cta, no_cta, ancla_id),
                conn=conn,
            )
            if not _mn or not _mn.get("fecha"):
                return [] if dry_run else 0
            ancla_fecha_del_id = _mn["fecha"]
        # Saldo de arranque = última fila ESTRICTAMENTE anterior en (fecha, id)
        # — el mismo orden en que camina el walk de abajo.
        row = db.fetch_one(
            """
            SELECT COALESCE(saldo, 0) AS saldo
              FROM scintela.transacciones_bancarias
             WHERE no_banco = %s
               AND ((%s)::text IS NULL OR no_cta = (%s)::text OR no_cta IS NULL)
               AND (fecha, id_transaccion) < (%s::date, %s)
             ORDER BY fecha DESC, id_transaccion DESC
             LIMIT 1
            """,
            (no_banco, no_cta, no_cta, ancla_fecha_del_id, ancla_id),
            conn=conn,
        )
        saldo = float(row["saldo"]) if row else 0.0
        cond_inicio = "(fecha, id_transaccion) >= (%s::date, %s)"
        params_inicio: tuple = (ancla_fecha_del_id, ancla_id)
        fecha_guarda = ancla_fecha_del_id
    elif ancla_fecha is not None:
        # TMT 2026-06-11 fix: el ancla es el saldo al CIERRE del día ANTERIOR
        # a ancla_fecha (fecha < ancla, ESTRICTO), porque el walk de abajo
        # re-aplica TODAS las filas con fecha >= ancla_fecha. Antes esto
        # llamaba _saldo_previo sin excluir la fecha ancla (excluir_id=None
        # ⇒ sin filtro de id en la misma fecha): después de un insert
        # backdated, el ancla era la fila recién insertada y la cadena
        # entera corría un día-neto por insert (hero Pichincha mostró
        # 462.916,76 hasta la mig 0093). Misma convención que _signed_delta.
        saldo = _saldo_previo(
            conn, no_banco=no_banco, no_cta=no_cta, fecha=ancla_fecha,
            solo_dias_anteriores=True,
        )
        cond_inicio = "fecha >= %s::date"
        params_inicio = (ancla_fecha,)
        fecha_guarda = ancla_fecha
    else:
        saldo = 0.0
        cond_inicio = "1=1"
        params_inicio = ()
        fecha_guarda = None

    rows = db.fetch_all(
        f"""
        SELECT id_transaccion, fecha, documento, concepto, importe, saldo,
               COALESCE(usuario_crea, '') AS usuario_crea
          FROM scintela.transacciones_bancarias
         WHERE no_banco = %s
           AND ((%s)::text IS NULL OR no_cta = (%s)::text OR no_cta IS NULL)
           AND {cond_inicio}
         ORDER BY fecha, id_transaccion
        """,
        (no_banco, no_cta, no_cta, *params_inicio),
        conn=conn,
    ) or []

    # ⭐ TMT 2026-08-03 — el plan se arma ANTES de escribir. `dry_run=True`
    # devuelve exactamente lo que escribiría, sin tocar nada: es la única
    # forma de mirar un recompute sobre plata de producción antes de
    # aplicarlo (un recompute mal anclado dejó Pichincha en −917.651,96 el
    # 2026-05-12). Ver /bancos/recompute-saldos?dry_run=1.
    plan: list[dict] = []
    for r in rows:
        # TMT 2026-06-03 audit fix: pasamos usuario_crea para distinguir
        # convención legacy DBF (importe signed, NDs reverso = +imp legítimo)
        # de nueva web (importe abs, sign por doc). Sin esto, los NDs reverso
        # del DBF se "corregían" al sign equivocado en cada recompute.
        saldo = round(saldo + _signed_delta(r["documento"], r["importe"], r.get("usuario_crea") or ""), 2)
        plan.append({
            "id_transaccion": r["id_transaccion"],
            "fecha": r.get("fecha"),
            "documento": (r.get("documento") or "").strip(),
            "concepto": (r.get("concepto") or ""),
            "importe": float(r["importe"] or 0),
            "saldo_actual": (None if r.get("saldo") is None else float(r["saldo"])),
            "saldo_nuevo": saldo,
        })
    if dry_run:
        return plan
    for f in plan:
        db.execute(
            "UPDATE scintela.transacciones_bancarias "
            "SET saldo = %s, fecha_modifica = CURRENT_TIMESTAMP "
            "WHERE id_transaccion = %s",
            (f["saldo_nuevo"], f["id_transaccion"]),
            conn=conn,
        )
    # ⭐ TMT 2026-08-04 — CANDADO DE COMMIT. Ver `CadenaRotaError`.
    # Si el walk hacia adelante dejó una costura (típico: el ancla cayó al
    # medio de un tramo con fechas fuera de orden), esto lo frena ACÁ en vez
    # de que aparezca mañana en el health con el balance ya publicado.
    assert_cadena_intacta(
        conn, no_banco=no_banco, no_cta=no_cta, desde_fecha=fecha_guarda,
        contexto="Re-encadenado hacia adelante")
    return len(plan)


class CadenaRotaError(RuntimeError):
    """Un write dejó el running `saldo` partido. Aborta la transacción.

    ⭐ TMT 2026-08-04 (dueña: *"ayer hubo otro quiebre, no nos debería pasar
    más"*). Tenía razón en no conformarse con "hace un mes que no aparece
    uno nuevo": el quiebre del 03/08 apareció y se reparó el mismo día, y el
    04/08 no se cargó nada. O sea la evidencia de que el motor estaba sano
    era la ausencia de uso, no la ausencia de bug.

    La respuesta no es mirar el panel a la mañana siguiente: es que **una
    transacción que deja la cadena partida no llegue a commitear**. Este
    error lo levanta `assert_cadena_intacta` y, como todos los writes de
    banco viven adentro de un `db.tx()`, hace ROLLBACK de la operación
    entera. Preferimos que el usuario vea "no pude guardar esto" a que el
    balance mienta en silencio hasta el health de la mañana.
    """


class AperturaDistintaError(ValueError):
    """El re-encadenado hacia atrás no aterriza en el saldo de la primera fila.

    ⭐ TMT 2026-08-04 — LA PREMISA QUE FALLÓ. `reencadenar_retro` nació
    asumiendo que la primera fila del banco era confiable, y que con el
    cierre anclado moverla significaba un faltante. En Pichincha resultó que
    **la primera fila ES una de las corruptas**: en orden (fecha, id) el
    ledger arranca con las ND del 29/06, que se cargaron ÚLTIMAS (id 44051+)
    y quedaron estampadas con un saldo de nivel 03/07.

    Tres caminos independientes dijeron que la apertura real es
    **2.962.335,77** y no los 3.209.797,81 que declara esa primera fila:
      · la apertura implícita de la primera fila CARGADA (id 43947);
      · cierre validado − suma de todos los movimientos;
      · la foto de cierre de junio — `historia.banco`(30/06) 3.083.974 menos
        caja 116.174,67 menos Internacional 3.761,19 = 2.964.038,14 contra
        los 2.964.038,04 que cierra el ledger el 30/06. Δ 0,10, y
        `historia.banco` está redondeado a entero.

    Así que la guarda no se afloja: se le **pasa la apertura**. Los dos
    extremos siguen clavados, sólo que el de abajo pasa a ser un número que
    alguien afirma explícitamente, en vez de uno que se lee de una fila que
    puede estar rota. Es más fuerte, no más débil.
    """

    def __init__(self, msg, *, actual, propuesta):
        super().__init__(msg)
        self.actual = actual
        self.propuesta = propuesta


def _sql_signed_delta(alias: str = "") -> str:
    """El `_signed_delta` de arriba, escrito en SQL. UNA sola regla.

    Había TRES conviviendo (`DOCS_ENTRADA` acá, `IN ('CH','ND')` en
    `saldo_bancos`, `IN ('CH','ND','RE','GS','PA')` en el fallback de
    `_saldo_previo`). Hoy la tabla sólo tiene DE/ND/CH/NC y las tres
    coinciden, así que la divergencia es invisible — hasta que aparezca un
    documento nuevo. Hay un test que clava que ésta y la de Python dan lo
    mismo. [[feedback_coherencia_numeros_una_fuente]]
    """
    a = f"{alias}." if alias else ""
    docs = ", ".join(f"'{d}'" for d in DOCS_ENTRADA)
    return (f"CASE WHEN UPPER(TRIM({a}documento)) IN ({docs}) "
            f"THEN {a}importe ELSE -{a}importe END")


def contar_quiebres(
    conn=None,
    *,
    no_banco: int,
    no_cta: str | None = None,
    desde_fecha: date | None = None,
) -> list[dict]:
    """Filas donde el `saldo` guardado NO se movió por su delta firmado.

    Camina en el MISMO orden que lee todo el sistema — `(fecha,
    id_transaccion)` — porque ese es el orden en que `saldo_bancos()` elige
    "la última fila". La ventana se calcula sobre TODAS las filas del banco
    y recién después se filtra por `desde_fecha`: si filtráramos antes, la
    primera fila de la ventana no tendría anterior y el quiebre del borde se
    perdería.

    ⭐ Criterio FIRMADO, no `ABS`. Se creía que la convención de signos de
    las filas viejas del DBF no era legible — por eso el health nació con
    criterio ABS. Verificado el 04/08/2026 sobre las 1.333 filas de
    Pichincha: `documento` predice el signo en **1.326**, y las 7 excepciones
    son exactamente los 7 quiebres reales. Lo que estaba roto era el ORDEN
    (saldo estampado por `id`, leído por `(fecha, id)`), no los signos.
    Firmado además caza un caso que ABS deja pasar: la fila que se mueve el
    importe correcto para el lado equivocado.
    """
    return db.fetch_all(
        f"""
        WITH w AS (
          SELECT id_transaccion, fecha, documento, concepto, importe, saldo,
                 {_sql_signed_delta('t')} AS sgn,
                 LAG(saldo)    OVER (ORDER BY fecha, id_transaccion) AS saldo_prev,
                 LAG(fecha)    OVER (ORDER BY fecha, id_transaccion) AS fecha_prev,
                 LAG(concepto) OVER (ORDER BY fecha, id_transaccion) AS concepto_prev
            FROM scintela.transacciones_bancarias t
           WHERE no_banco = %s
             AND ((%s)::text IS NULL OR no_cta = (%s)::text OR no_cta IS NULL)
             AND saldo IS NOT NULL
        )
        SELECT * FROM w
         WHERE saldo_prev IS NOT NULL
           AND ABS((saldo - saldo_prev) - sgn) > 0.02
           AND (%s::date IS NULL OR fecha >= %s::date)
         ORDER BY fecha, id_transaccion
        """,
        (no_banco, no_cta, no_cta, desde_fecha, desde_fecha),
        conn=conn,
    ) or []


def assert_cadena_intacta(
    conn=None,
    *,
    no_banco: int,
    no_cta: str | None = None,
    desde_fecha: date | None = None,
    contexto: str = "",
) -> None:
    """Candado de commit: si el write dejó un quiebre, revienta y rollback.

    `desde_fecha` acota a lo que ESTE write pudo tocar — no queremos que un
    quiebre histórico que todavía no se limpió bloquee una carga de hoy. Sin
    ese recorte el candado no se podría prender hasta terminar de planchar
    la historia, y mientras tanto la oficina no podría trabajar.
    """
    rotas = contar_quiebres(
        conn, no_banco=no_banco, no_cta=no_cta, desde_fecha=desde_fecha)
    if not rotas:
        return
    r = rotas[0]
    detalle = (
        f"id {r['id_transaccion']} {r['fecha']} "
        f"{(r.get('documento') or '').strip()} "
        f"{(r.get('concepto') or '')[:30]}: el saldo saltó "
        f"{float(r['saldo']) - float(r['saldo_prev']):,.2f} "
        f"cuando el movimiento vale {float(r['sgn']):,.2f}"
    )
    raise CadenaRotaError(
        f"{contexto or 'Movimiento bancario'}: la operación habría dejado el "
        f"saldo del banco {no_banco} partido en {len(rotas)} punto(s) — "
        f"{detalle}. No se guardó nada. El balance lee ese saldo, así que "
        f"guardarlo habría corrido el patrimonio y la utilidad."
    )


def reencadenar_retro(
    conn,
    *,
    no_banco: int,
    no_cta: str | None = None,
    apertura: float | None = None,
    dry_run: bool = False,
) -> int | list[dict]:
    """Re-encadena HACIA ATRÁS: ancla en la ÚLTIMA fila y camina al pasado.

    ⭐ POR QUÉ EXISTE (TMT 2026-08-04). `recompute_saldos_desde` ancla en un
    punto y camina para ADELANTE: preserva el opening y **mueve el cierre**.
    Pero el cierre es justo lo que no se puede mover — es el número que el
    Balance publica como BANCOS (vía `saldo_bancos()`, que lee el running
    guardado de la última fila) y el que la conciliación ya validó contra el
    EXTRACTO (sesión #60 cerrada en +2,00 el 03/08). Por eso el dry-run hacia
    adelante desde el 29/06 se abortó: iba a mover el cierre.

    Este camina al revés. El ancla es la última fila en `(fecha,
    id_transaccion)` — el nivel que el extracto ya dio por bueno — y cada
    fila anterior se estampa restándole el delta firmado de la que le sigue.
    Consecuencia: **el cierre queda invariante por construcción**, así que
    patrimonio y utilidad no se mueven ni un centavo. Es la herramienta para
    limpiar costuras viejas sin tocar el presente, que es exactamente lo que
    pide la regla de la dueña: *"si tocás algo de antes de agosto tenés que
    dejarlo sin que la utilidad se mueva en absoluto"*.

    GUARDA ESPEJO del bug del 2026-05-12 (un walk sin ancla dejó Pichincha en
    −917.651,96 destruyendo el opening): éste podría destruirlo por el otro
    lado. Con los dos extremos clavados, un re-encadenado no puede inventar
    plata. Si el walk no aterriza donde dice la primera fila, levanta
    `AperturaDistintaError` con la apertura que piden los movimientos — y
    recién se aplica pasando esa apertura por `apertura=`, que es alguien
    afirmándola. Ver `AperturaDistintaError` para por qué el saldo de la
    primera fila no alcanza como referencia.

    Con `dry_run=True` devuelve el plan (`saldo_actual` / `saldo_nuevo`),
    mismo formato que `recompute_saldos_desde`. Sin él, devuelve cuántas
    filas escribió.
    """
    if not no_banco:
        raise ValueError("no_banco requerido para reencadenar_retro")

    rows = db.fetch_all(
        """
        SELECT id_transaccion, fecha, documento, concepto, importe, saldo,
               COALESCE(usuario_crea, '') AS usuario_crea
          FROM scintela.transacciones_bancarias
         WHERE no_banco = %s
           AND ((%s)::text IS NULL OR no_cta = (%s)::text OR no_cta IS NULL)
         ORDER BY fecha, id_transaccion
        """,
        (no_banco, no_cta, no_cta),
        conn=conn,
    ) or []
    if len(rows) < 2:
        return [] if dry_run else 0
    if rows[-1].get("saldo") is None:
        raise ValueError(
            "La última fila del banco no tiene saldo guardado: no hay ancla "
            "que preservar. Arreglá esa fila antes de re-encadenar."
        )

    # Camino al revés: saldo(anterior) = saldo(actual) - delta(actual).
    saldos: dict[int, float] = {rows[-1]["id_transaccion"]: float(rows[-1]["saldo"])}
    corriente = float(rows[-1]["saldo"])
    for i in range(len(rows) - 1, 0, -1):
        r = rows[i]
        corriente = round(
            corriente - _signed_delta(
                r["documento"], r["importe"], r.get("usuario_crea") or ""), 2)
        saldos[rows[i - 1]["id_transaccion"]] = corriente

    plan = [{
        "id_transaccion": r["id_transaccion"],
        "fecha": r.get("fecha"),
        "documento": (r.get("documento") or "").strip(),
        "concepto": (r.get("concepto") or ""),
        "importe": float(r["importe"] or 0),
        "saldo_actual": (None if r.get("saldo") is None else float(r["saldo"])),
        "saldo_nuevo": saldos[r["id_transaccion"]],
    } for r in rows]

    primera = plan[0]
    # La APERTURA es la plata que el banco tenía antes de la primera fila:
    # el saldo que le queda a esa fila, menos lo que la fila misma movió.
    _d0 = _signed_delta(rows[0]["documento"], rows[0]["importe"],
                        rows[0].get("usuario_crea") or "")
    ap_nueva = round(primera["saldo_nuevo"] - _d0, 2)
    ap_guardada = (None if primera["saldo_actual"] is None
                   else round(primera["saldo_actual"] - _d0, 2))

    if apertura is not None:
        # Alguien AFIRMA la apertura. Los dos extremos quedan clavados: el
        # cierre por construcción, la apertura por este chequeo. Es más
        # fuerte que leerla de una fila que puede estar rota.
        if abs(ap_nueva - float(apertura)) > 0.02:
            raise ValueError(
                f"Abortado: con el cierre anclado, los movimientos dan una "
                f"apertura de {ap_nueva:,.2f} y vos afirmaste "
                f"{float(apertura):,.2f} — difieren "
                f"{ap_nueva - float(apertura):,.2f}. Eso no es una costura de "
                f"orden, es un faltante: mirá esas filas de a una contra el "
                f"extracto."
            )
    elif ap_guardada is not None and abs(ap_nueva - ap_guardada) > 0.02:
        raise AperturaDistintaError(
            f"El re-encadenado no aterriza donde dice la primera fila: ella "
            f"declara una apertura de {ap_guardada:,.2f} y los movimientos, "
            f"contados desde el cierre, piden {ap_nueva:,.2f} "
            f"({ap_nueva - ap_guardada:+,.2f}). O falta plata, o la primera "
            f"fila es una de las que están mal estampadas. Confirmá cuál es "
            f"la apertura antes de aplicar.",
            actual=ap_guardada, propuesta=ap_nueva,
        )

    cambios = [f for f in plan
               if f["saldo_actual"] is None
               or abs(f["saldo_nuevo"] - f["saldo_actual"]) > 0.005]
    if dry_run:
        return plan
    for f in cambios:
        db.execute(
            "UPDATE scintela.transacciones_bancarias "
            "SET saldo = %s, fecha_modifica = CURRENT_TIMESTAMP "
            "WHERE id_transaccion = %s",
            (f["saldo_nuevo"], f["id_transaccion"]),
            conn=conn,
        )
    assert_cadena_intacta(
        conn, no_banco=no_banco, no_cta=no_cta,
        contexto="Re-encadenado hacia atrás")
    return len(cambios)


def saldo_actual(no_banco: int, no_cta: str | None = None, conn=None) -> float:
    """Saldo running más reciente del banco/cuenta. 0.0 si no hay movs."""
    row = db.fetch_one(
        """
        SELECT COALESCE(saldo, 0) AS saldo
          FROM scintela.transacciones_bancarias
         WHERE no_banco = %s
           AND ((%s)::text IS NULL OR no_cta = (%s)::text OR no_cta IS NULL)
         ORDER BY fecha DESC, id_transaccion DESC
         LIMIT 1
        """,
        (no_banco, no_cta, no_cta),
        conn=conn,
    )
    return float(row["saldo"]) if row else 0.0


def insertar_compensacion(
    conn,
    *,
    transaccion_origen_id: int,
    motivo: str,
    usuario: str = "web",
) -> dict:
    """Crea una fila de compensación que invierte un movimiento existente.

    Útil para anular un cheque depositado: en vez de DELETE de la fila
    original (que rompería auditoría), insertamos una compensación con
    documento opuesto al original (DE → ND, CH → DE, etc.) y saldo running
    actualizado. La fila original queda sin tocar — paper trail completo.

    Reglas:
      - DE → ND  (depósito → nota de débito por anulación)
      - CH → NC  (cheque emitido → nota de crédito reingresa la plata)
      - cualquier otro → "XX" inverso (ajuste compensatorio).

    SKILL.md "Reverso bancario — documento de signo opuesto":
        CH (egreso, signo −) compensa con NC (ingreso, signo +).
    Antes era CH → DE, que también es signo +, pero NC matchea el
    patrón canónico de reverso (DE es para depósitos de cheques de
    terceros, no para reingresos por reverso). TMT 2026-05-14.

    Devuelve `{id_transaccion, saldo_nuevo}`.
    """
    orig = db.fetch_one(
        """
        SELECT id_transaccion, fecha, documento, importe, concepto,
               no_banco, no_cta, prov, numreferencia
          FROM scintela.transacciones_bancarias
         WHERE id_transaccion = %s
        """,
        (transaccion_origen_id,),
        conn=conn,
    )
    if not orig:
        raise ValueError(f"Transacción origen {transaccion_origen_id} no existe.")

    doc_orig = (orig["documento"] or "").upper().strip()
    if doc_orig == "DE":
        doc_comp = "ND"
    elif doc_orig == "CH":
        doc_comp = "NC"
    else:
        doc_comp = "XX"

    return insert_movimiento_bancario(
        conn,
        no_banco=orig["no_banco"],
        no_cta=orig["no_cta"],
        # TMT 2026-06-05 (bug hunt lente 3): today_ec() para que la compensación
        # quede fechada en Ecuador, no en UTC del server (de noche fechaba mañana).
        fecha=today_ec(),
        documento=doc_comp,
        importe=orig["importe"],
        concepto=f"Comp. tx#{transaccion_origen_id}: {motivo[:30]}"[:50],
        prov=orig["prov"],
        numreferencia=orig["numreferencia"],
        usuario=usuario,
    )
