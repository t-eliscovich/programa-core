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


def _signed_delta(documento: str, importe: float) -> float:
    """Delta firmado a aplicar al saldo, manejando ambas convenciones.

    Bug TMT 2026-05-11: importe en la DB tiene convención MIXTA —
      - filas DBF legacy: importe SIGNED (-N para egresos)
      - filas bank_helpers nuevas: importe ABS (+N siempre)

    Esta función unifica:
      - Si importe < 0 → ya está firmado, usar como-es (legacy)
      - Si importe ≥ 0 → aplicar `signo_documento(doc) * importe`
    """
    imp = float(importe or 0)
    if imp < 0:
        return imp
    return imp if signo_documento(documento) > 0 else -imp


def _saldo_previo(
    conn,
    *,
    no_banco: int,
    no_cta: str | None,
    fecha: date,
    excluir_id: int | None = None,
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
    """
    row = db.fetch_one(
        """
        SELECT saldo
          FROM scintela.transacciones_bancarias
         WHERE no_banco = %s
           AND ((%s)::text IS NULL OR no_cta = (%s)::text OR no_cta IS NULL)
           AND saldo IS NOT NULL
           AND ((fecha < %s)
                OR (fecha = %s AND (%s::int IS NULL
                                    OR id_transaccion < %s::int)))
         ORDER BY fecha DESC, id_transaccion DESC
         LIMIT 1
        """,
        (
            no_banco,
            no_cta, no_cta,
            fecha,
            fecha, excluir_id, excluir_id,
        ),
        conn=conn,
    )
    if row and row.get("saldo") is not None:
        return float(row["saldo"])

    # No hay ningún saldo running válido antes del ancla → reconstruir
    # con SUM firmado por documento de TODAS las filas anteriores
    # (replica el fallback de `saldo_bancos`).
    fallback = db.fetch_one(
        """
        SELECT COALESCE(SUM(
                 CASE WHEN UPPER(TRIM(documento)) IN ('CH','ND','RE','GS','PA')
                      THEN -importe
                      ELSE  importe
                 END
               ), 0) AS saldo
          FROM scintela.transacciones_bancarias
         WHERE no_banco = %s
           AND ((%s)::text IS NULL OR no_cta = (%s)::text OR no_cta IS NULL)
           AND ((fecha < %s)
                OR (fecha = %s AND (%s::int IS NULL
                                    OR id_transaccion < %s::int)))
        """,
        (
            no_banco,
            no_cta, no_cta,
            fecha,
            fecha, excluir_id, excluir_id,
        ),
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
) -> dict:
    """Inserta un movimiento bancario con saldo running calculado.

    Devuelve el dict {id_transaccion, saldo_nuevo}.

    `importe` siempre positivo. El signo se aplica internamente según
    `documento`. Errores claros si el caller pasa importe negativo o cero.

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
    # Aceptamos importe signed (legacy) o abs (nuevo). El delta unificado
    # lo computa _signed_delta. El valor que almacenamos en la columna
    # `importe` mantiene el SIGNO del caller para preservar la convención
    # mixta — lo que pasa Programa Core legacy queda signed, lo que pasa
    # bank_helpers nuevo queda abs.
    importe_abs = abs(importe_f)
    signo = signo_documento(documento)
    saldo_anterior = _saldo_previo(
        conn, no_banco=no_banco, no_cta=no_cta, fecha=fecha,
    )
    saldo_nuevo = round(saldo_anterior + _signed_delta(documento, importe_f), 2)

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
) -> int:
    """Walk-forward: recalcula `saldo` para toda fila >= ancla.

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
        saldo = _saldo_previo(
            conn, no_banco=no_banco, no_cta=no_cta,
            fecha=date(1900, 1, 1),  # ignorado porque excluir_id manda
            excluir_id=ancla_id,
        )
        # Re-buscamos saldo pre-ancla por id (más preciso que por fecha).
        row = db.fetch_one(
            """
            SELECT COALESCE(saldo, 0) AS saldo
              FROM scintela.transacciones_bancarias
             WHERE no_banco = %s
               AND ((%s)::text IS NULL OR no_cta = (%s)::text OR no_cta IS NULL)
               AND id_transaccion < %s
             ORDER BY id_transaccion DESC
             LIMIT 1
            """,
            (no_banco, no_cta, no_cta, ancla_id),
            conn=conn,
        )
        saldo = float(row["saldo"]) if row else 0.0
        cond_inicio = "id_transaccion >= %s"
        params_inicio: tuple = (ancla_id,)
    elif ancla_fecha is not None:
        saldo = _saldo_previo(
            conn, no_banco=no_banco, no_cta=no_cta, fecha=ancla_fecha,
        )
        cond_inicio = "fecha >= %s::date"
        params_inicio = (ancla_fecha,)
    else:
        saldo = 0.0
        cond_inicio = "1=1"
        params_inicio = ()

    rows = db.fetch_all(
        f"""
        SELECT id_transaccion, documento, importe
          FROM scintela.transacciones_bancarias
         WHERE no_banco = %s
           AND ((%s)::text IS NULL OR no_cta = (%s)::text OR no_cta IS NULL)
           AND {cond_inicio}
         ORDER BY fecha, id_transaccion
        """,
        (no_banco, no_cta, no_cta, *params_inicio),
        conn=conn,
    ) or []

    n = 0
    for r in rows:
        # Smart delta (igual que trigger y _signed_delta): respeta importe
        # signed legacy y signa los abs nuevos. Antes usaba siempre
        # signo*abs(importe) que da vuelta los signos de las filas legacy
        # — TMT 2026-05-11.
        saldo = round(saldo + _signed_delta(r["documento"], r["importe"]), 2)
        db.execute(
            "UPDATE scintela.transacciones_bancarias "
            "SET saldo = %s, fecha_modifica = CURRENT_TIMESTAMP "
            "WHERE id_transaccion = %s",
            (saldo, r["id_transaccion"]),
            conn=conn,
        )
        n += 1
    return n


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
        fecha=date.today(),
        documento=doc_comp,
        importe=orig["importe"],
        concepto=f"Comp. tx#{transaccion_origen_id}: {motivo[:30]}"[:50],
        prov=orig["prov"],
        numreferencia=orig["numreferencia"],
        usuario=usuario,
    )
