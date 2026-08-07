"""Consultas de retenciones en la fuente emitidas por clientes.

scintela.retencion: id_retencion, codigo_cli, rete, numf, fecha
Se liga a scintela.factura por (codigo_cli, numf).
"""
import uuid as _uuid
from datetime import date

import db
from filters import money_es as _money
from filters import today_ec
from modules._lib import busqueda

# La fórmula del saldo vive en UN solo lugar: importe − abono − retención.
from modules.facturas import queries as _fact_q
from periodo_guard import asegurar_fecha_abierta


def emitir(
    *,
    codigo_cli: str,
    numf: int,
    rete,
    fecha: date | None = None,
    usuario: str = "web",
) -> dict:
    """Registrar retención en la fuente emitida por el cliente contra una factura.

    Reglas:
      - La factura (codigo_cli, numf) tiene que existir.
      - No debe existir ya una retención para ese (codigo_cli, numf).
      - `rete` no puede superar `factura.importe` (chequeo defensivo).
    """
    if not codigo_cli:
        raise ValueError("codigo_cli requerido.")
    if not numf or numf <= 0:
        raise ValueError("numf de factura requerido.")
    rete_f = float(rete or 0)
    if rete_f <= 0:
        raise ValueError("Valor retenido debe ser mayor que cero.")
    asegurar_fecha_abierta(fecha)

    f = db.fetch_one(
        "SELECT id_factura, importe FROM scintela.factura "
        "WHERE codigo_cli=%s AND numf=%s",
        (codigo_cli, numf),
    )
    if not f:
        raise ValueError(f"Factura {numf} del cliente {codigo_cli!r} no existe.")
    if rete_f > float(f["importe"] or 0) + 0.01:
        raise ValueError(
            f"Retención ({rete_f:.2f}) no puede superar el importe de la factura ({float(f['importe']):.2f})."
        )

    ya = db.fetch_one(
        "SELECT 1 AS x FROM scintela.retencion WHERE codigo_cli=%s AND numf=%s",
        (codigo_cli, numf),
    )
    if ya:
        raise ValueError(
            f"Ya existe una retención para factura {numf} del cliente {codigo_cli}."
        )

    row = db.execute_returning(
        """
        INSERT INTO scintela.retencion (codigo_cli, numf, rete, fecha, usuario_crea)
        VALUES (%s, %s, %s, COALESCE(%s, CURRENT_DATE), %s)
        RETURNING id_retencion
        """,
        (codigo_cli, numf, rete_f, fecha, usuario),
    ) or {}
    return row


def por_id(id_retencion: int) -> dict | None:
    """Lectura puntual — usada por la vista de confirmación."""
    return db.fetch_one(
        """
        SELECT r.id_retencion, r.codigo_cli, r.numf, r.rete, r.fecha,
               COALESCE(c.nombre, '') AS cliente,
               f.importe AS importe_factura,
               f.numf_completo
        FROM scintela.retencion r
        LEFT JOIN scintela.cliente c ON c.codigo_cli = r.codigo_cli
        LEFT JOIN scintela.factura f ON f.codigo_cli = r.codigo_cli AND f.numf = r.numf
        WHERE r.id_retencion = %s
        """,
        (id_retencion,),
    )


def anular(id_retencion: int, usuario: str = "web") -> int:
    """Borrar una retención (sólo con permiso retenciones.anular)."""
    return db.execute(
        "DELETE FROM scintela.retencion WHERE id_retencion = %s",
        (id_retencion,),
    )


def facturas_sin_retencion(codigo_cli: str, limite: int = 100) -> list[dict]:
    """Facturas del cliente que todavía no tienen retención — selector del form."""
    return db.fetch_all(
        """
        SELECT f.id_factura, f.numf, f.numf_completo, f.fecha, f.importe, f.saldo
        FROM scintela.factura f
        LEFT JOIN scintela.retencion r
               ON r.codigo_cli = f.codigo_cli AND r.numf = f.numf
        WHERE f.codigo_cli = %s
          AND r.id_retencion IS NULL
        ORDER BY f.fecha DESC, f.numf DESC
        LIMIT %s
        """,
        (codigo_cli, limite),
    )


def buscar(
    q: str = "",
    desde: str | None = None,
    hasta: str | None = None,
    limite: int = 500,
) -> list[dict]:
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    # TMT 2026-08-04 (dueña "no funciona si solo busco condor"): match por
    # PALABRAS sueltas y sin acentos. Ver modules/_lib/busqueda.py.
    _txt_sql, _txt_params = busqueda.condicion(
        q, ("r.codigo_cli", "c.nombre", "f.numf_completo"), prefijo="bqt")
    return db.fetch_all(
        """
        SELECT r.id_retencion, r.fecha, r.codigo_cli, r.numf, r.rete,
               COALESCE(c.nombre, '')         AS cliente,
               f.id_factura, f.numf_completo, f.importe AS importe_fact,
               CASE WHEN COALESCE(f.importe, 0) > 0
                    THEN ROUND((r.rete / f.importe * 100)::numeric, 2)
                    ELSE NULL END AS pct
        FROM scintela.retencion r
        LEFT JOIN scintela.cliente c ON c.codigo_cli = r.codigo_cli
        LEFT JOIN scintela.factura f ON f.codigo_cli = r.codigo_cli AND f.numf = r.numf
        WHERE (%(q)s IS NULL
               OR CAST(r.numf AS TEXT) LIKE %(like)s
               OR (__TEXTO_MATCH__))
          AND (%(desde)s::date IS NULL OR r.fecha >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR r.fecha <= %(hasta)s::date)
        ORDER BY r.fecha DESC, r.id_retencion DESC
        LIMIT %(limite)s
        """.replace("__TEXTO_MATCH__", _txt_sql or "FALSE"),
        {
            **_txt_params,
            "q": q or None, "like": like,
            "desde": desde or None, "hasta": hasta or None,
            "limite": limite,
        },
    )


def total_por_mes(anio: int | None = None) -> list[dict]:
    return db.fetch_all(
        """
        SELECT date_trunc('month', fecha)::date AS mes,
               COALESCE(SUM(rete), 0) AS total_retenido,
               COUNT(*)  AS n
        FROM scintela.retencion
        WHERE fecha IS NOT NULL
          AND (%s::int IS NULL OR EXTRACT(YEAR FROM fecha)::int = %s::int)
        GROUP BY 1
        ORDER BY 1 DESC
        """,
        (anio, anio),
    )


# ---------------------------------------------------------------------------
# Retenciones desde Asinfo: registrar + APLICAR (bajar el saldo) por factura.
# TMT 2026-07-09 (dueña): "buscar retenciones en el asinfo para aplicárselas a
# las facturas que traemos — la retención total suma de IVA y Fuente de cada
# factura". Registrar Y bajar el saldo (abono), junto al traer facturas.
# Idempotente: si la factura ya tiene retención, no la vuelve a aplicar.
# Reversible: cada aplicación deja un mov_doble con snapshot.
# ---------------------------------------------------------------------------
import mov_doble as _md  # noqa: E402

# Tope del listado de sin-factura del lote (el CONTADOR es exacto igual).
_MAX_DETALLE = 200


def _numf_sri(numero) -> int | None:
    """N SRI numerico de '001-099-000179468' -> 179468. None si no hay digitos.

    Fuente unica: la usan el aplicador (`_factura_por_numero`) y la pantalla
    (`preview_retenciones_asinfo`), que TIENEN que clasificar igual.
    """
    import re as _re
    m = _re.findall(r"\d+", str(numero or ""))
    if not m:
        return None
    return int(m[-1])


def _factura_por_numero(numero: str, conn):
    """Factura viva de PC (no backfill, no anulada) por numf_completo (SRI).

    TMT 2026-07-21: fallback por numf (N° SRI numérico) cuando el match por
    numf_completo falla. Muchas facturas de PC (origen dBase o cargadas bajo
    otro código de cliente) tienen numf_completo NULL, así que la retención
    nunca las encontraba y no se aplicaba sola. El numf SRI es único, así que
    matchear por numf (cualquier cliente) es seguro.
    """
    f = db.fetch_one(
        """
        SELECT id_factura, codigo_cli, numf, numf_completo, importe, abono,
               retencion, saldo, stat
          FROM scintela.factura
         WHERE numf_completo = %s
           AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
           AND COALESCE(stat, '') <> 'X'
         ORDER BY id_factura
         LIMIT 1
         FOR UPDATE
        """,
        (numero,),
        conn=conn,
    )
    if f:
        return f
    # Fallback: extraer el N° SRI numérico de `numero` y matchear por numf.
    _numf = _numf_sri(numero)
    if _numf is None:
        return None
    return db.fetch_one(
        """
        SELECT id_factura, codigo_cli, numf, numf_completo, importe, abono,
               retencion, saldo, stat
          FROM scintela.factura
         WHERE numf = %s
           AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
           AND COALESCE(stat, '') <> 'X'
         ORDER BY id_factura
         LIMIT 1
         FOR UPDATE
        """,
        (_numf,),
        conn=conn,
    )


def _aplicar_una_por_numero(numero: str, rete: float, usuario: str,
                            batch_id: str | None = None,
                            solo_sin_abono: bool = False) -> str:
    """Registra scintela.retencion + baja el saldo de la factura `numero`.

    Devuelve: 'aplicada' | 'ya' (ya tenía retención) | 'sin_factura' |
    'rete_0' | 'rete_gt_importe' | 'rete_gt_saldo' | 'tiene_abono'.

    `solo_sin_abono` es el freno del BARRIDO de períodos viejos: en las facturas
    de la época del dBase la retención puede estar ya sumada adentro del abono
    (RETENCIO.PRG la metía ahí y no dejaba fila en `scintela.retencion`, así que
    el guard `ya` no la ve). Aplicarla otra vez le descontaría el saldo DOS
    veces al cliente, en silencio. Con el freno puesto, cualquier factura que
    tenga algún abono se saltea y sale por la pantalla
    `/facturas/retenciones-en-abono`, que la mira de a una. Si el abono es 0 no
    hay nada adentro que pueda estar duplicado, y aplicar es seguro.

    Nota 2026-08-06: 'registrada' ya no se devuelve — era la rama "el dBase
    ya aplicó el abono" y el dBase se retiró. La retención siempre SUMA.
    """
    rete = round(float(rete or 0), 2)
    if rete <= 0.005:
        return "rete_0"
    with db.tx() as conn:
        f = _factura_por_numero(numero, conn)
        if not f:
            return "sin_factura"
        importe = round(float(f["importe"] or 0), 2)
        if rete > importe + 0.01:
            return "rete_gt_importe"
        ya = db.fetch_one(
            "SELECT 1 AS x FROM scintela.retencion "
            "WHERE codigo_cli = %s AND numf = %s",
            (f["codigo_cli"], f["numf"]),
            conn=conn,
        )
        if ya:
            return "ya"
        abono = round(float(f["abono"] or 0), 2)
        retencion = round(float(f["retencion"] or 0), 2)
        saldo = round(float(f["saldo"] or 0), 2)
        # TMT 2026-08-06 (dueña): freno — si la retención es más grande que lo
        # que queda por cobrar, el saldo quedaría negativo. No se aplica sola;
        # va a n_error para mirarla a mano. (Después del guard `ya`, así una
        # retención ya aplicada nunca cae acá al re-correr el cron.)
        if rete > saldo + 0.01:
            return "rete_gt_saldo"
        if solo_sin_abono and abono > 0.005:
            return "tiene_abono"
        stat_prev = (f["stat"] or "").strip()
        rrow = db.execute_returning(
            "INSERT INTO scintela.retencion "
            "  (codigo_cli, numf, rete, fecha, usuario_crea) "
            "VALUES (%s, %s, %s, CURRENT_DATE, %s) "
            "RETURNING id_retencion",
            (f["codigo_cli"], f["numf"], rete, usuario),
            conn=conn,
        ) or {}
        id_ret = rrow.get("id_retencion")
        # TMT 2026-08-07 (dueña): la retención BAJA el saldo igual que antes,
        # pero YA NO se suma al abono — va a su propia columna
        # `factura.retencion` (migración 0179), así el estado de cuenta puede
        # mostrar "abonado" y "retención" separados, que es lo que el cliente
        # necesita para cuadrar. Matiza (no revierte) la regla del 06/08
        # (commit 8ed70dd): aquella decidió que la retención SIEMPRE cuenta
        # contra la deuda aunque ya hubiera un abono manual — eso sigue igual;
        # lo único que cambia es DÓNDE se guarda. Abono manual 10 + retención
        # 15 sobre una factura de 25 sigue dando saldo 0, pero ahora se lee
        # "abonado 10 · retención 15" en vez de "abonado 25".
        retencion_new = round(retencion + rete, 2)
        saldo_new = _fact_q.saldo_de(importe, abono, retencion_new)
        stat_new = "T" if saldo_new <= 0.005 else "A"
        db.execute(
            "UPDATE scintela.factura "
            "   SET retencion = %s, saldo = %s, stat = %s, "
            "       usuario_modifica = %s "
            " WHERE id_factura = %s",
            (retencion_new, saldo_new, stat_new, usuario, f["id_factura"]),
            conn=conn,
        )
        aplicado = True
        concepto = (
            f"Retención $ {_money(rete)} a la factura {f['numf']} "
            f"{f['codigo_cli']} — saldo $ {_money(saldo)} → $ {_money(saldo_new)}"
        )
        _md.registrar(
            conn=conn,
            tipo="retencion_asinfo_aplicada",
            origen_table="factura", origen_id=f["id_factura"],
            destino_table="factura", destino_id=f["id_factura"],
            importe=rete,
            fecha=today_ec(),
            concepto=concepto[:200],
            usuario=usuario,
            batch_id=batch_id,
            metadata={
                "id_retencion": id_ret, "numero": numero,
                "codigo_cli": f["codigo_cli"], "numf": f["numf"],
                "rete": rete, "abono_previo": abono,
                "retencion_previa": retencion, "saldo_previo": saldo,
                "stat_previo": stat_prev, "aplicado": aplicado,
            },
        )
    return "aplicada" if aplicado else "registrada"


def aplicar_retenciones_asinfo(desde, hasta, usuario: str = "web",
                               solo_sin_abono: bool = False) -> dict:
    """Trae las retenciones de Asinfo del período y las aplica a las facturas
    de PC (registra scintela.retencion + baja el saldo). Idempotente.

    Devuelve {n_aplicadas, n_ya, n_sin_factura, n_error, total_aplicado,
    n_retenciones_asinfo}.
    """
    from modules.asinfo import service as asinfo_service
    ret_map = asinfo_service.retenciones_periodo(desde, hasta) or {}
    res = {
        "n_aplicadas": 0, "n_registradas": 0, "n_ya": 0, "n_sin_factura": 0,
        "n_error": 0, "n_tiene_abono": 0,
        "total_aplicado": 0.0, "n_retenciones_asinfo": len(ret_map),
        # TMT 2026-08-03: un CONTADOR no se puede investigar. Las que no
        # encuentran factura salían por un `return "sin_factura"` que no dejaba
        # log, ni mov_doble, ni fila: eran 45 invisibles. Ahora se listan.
        "sin_factura": [],
    }
    # TMT 2026-08-07 (dueña: *"poner en el historial cuando se cargan x
    # retenciones por x valor"*). Cada factura sigue siendo su propia
    # transacción — si una falla, las demás entran igual —, pero TODAS las de
    # una corrida comparten `batch_id`. El historial ya sabe agrupar por ahí:
    # con esto la corrida diaria del cron deja UNA tarjeta "12 movimientos ·
    # $3.450,20" en vez de doce renglones sueltos que tapan el resto del día.
    batch_id = str(_uuid.uuid4())
    for numero, r in ret_map.items():
        rete = round(float((r or {}).get("ret_total") or 0), 2)
        try:
            estado = _aplicar_una_por_numero(
                numero, rete, usuario, batch_id, solo_sin_abono=solo_sin_abono)
        except Exception:
            res["n_error"] += 1
            continue
        if estado == "aplicada":
            res["n_aplicadas"] += 1
            res["total_aplicado"] = round(res["total_aplicado"] + rete, 2)
        elif estado == "registrada":
            res["n_registradas"] += 1
        elif estado == "ya":
            res["n_ya"] += 1
        elif estado == "tiene_abono":
            # No es un error: es el freno del barrido. Se mira por pantalla.
            res["n_tiene_abono"] += 1
        elif estado == "sin_factura":
            res["n_sin_factura"] += 1
            if len(res["sin_factura"]) < _MAX_DETALLE:
                res["sin_factura"].append({
                    "numero": numero, "numf": _numf_sri(numero), "rete": rete,
                })
        elif estado in ("rete_0", "rete_gt_importe", "rete_gt_saldo"):
            res["n_error"] += 1
    res["total_sin_factura"] = round(
        sum(float(x["rete"] or 0) for x in res["sin_factura"]), 2)
    # ⭐ La campanita. TMT 2026-08-07: *"hagamos una campanita para eso"*. Una
    # retención baja el saldo de la factura SIN que entre un peso: mueve la
    # utilidad y no la mueve nadie desde una pantalla, así que si no avisa, no
    # se entera.
    #
    # La `clave` cuelga del `batch_id` de ESTA corrida, no del día: el cron
    # pasa dos veces (11:00 y 16:00 EC) y con una clave diaria el segundo
    # aviso se lo comía el ON CONFLICT — las retenciones de la tarde no
    # avisaban nunca. Una corrida que no aplicó nada no deja aviso (el `if`),
    # así que repetir no ensucia.
    if res.get("n_aplicadas"):
        try:
            # TMT 2026-08-07 (dueña): *"cortito y al pie, se cargaron x
            # retenciones por x monto y ya"*. Sin el renglón de explicación
            # que tenía antes: el aviso es un titular, no una clase.
            # El formato de número sale de `filters.money_es` (punto = miles,
            # coma = decimales) en vez del reemplazo a mano que había acá, que
            # además se aplicaba sobre TODO el título.
            from filters import today_ec as _hoy
            from modules.avisos import queries as _av

            _n = res["n_aplicadas"]
            _av.avisar(
                fuente="retenciones",
                titulo=(f"Se cargaron {_n} retencion{'es' if _n != 1 else ''} "
                        f"por $ {_money(res['total_aplicado'])}"),
                importe=res["total_aplicado"], cantidad=_n,
                url="/facturas/retenciones-asinfo",
                clave=f"retenciones:{_hoy()}:{batch_id}",
            )
        except Exception:  # noqa: BLE001 -- avisar nunca rompe al que avisa
            pass
    return res


def aplicar_retenciones_asinfo_seleccion(
    desde, hasta, numeros, usuario: str = "web",
) -> dict:
    """Como aplicar_retenciones_asinfo pero SOLO para los `numeros` (facturas SRI)
    elegidos por el usuario. Los importes salen igual de Asinfo (fuente de verdad),
    no del form. Idempotente. Devuelve el mismo shape que aplicar_retenciones_asinfo.
    """
    from modules.asinfo import service as asinfo_service
    seleccion = {str(n).strip() for n in (numeros or []) if str(n).strip()}
    ret_map = asinfo_service.retenciones_periodo(desde, hasta) or {}
    res = {
        "n_aplicadas": 0, "n_registradas": 0, "n_ya": 0, "n_sin_factura": 0,
        "n_error": 0, "total_aplicado": 0.0, "n_retenciones_asinfo": len(seleccion),
    }
    if not seleccion:
        return res
    # Mismo criterio que la corrida completa: una tarjeta por tanda tildada.
    batch_id = str(_uuid.uuid4())
    for numero in seleccion:
        r = ret_map.get(numero)
        if r is None:
            # El número elegido ya no está en Asinfo para el período → lo ignoramos.
            res["n_error"] += 1
            continue
        rete = round(float((r or {}).get("ret_total") or 0), 2)
        try:
            estado = _aplicar_una_por_numero(numero, rete, usuario, batch_id)
        except Exception:
            res["n_error"] += 1
            continue
        if estado == "aplicada":
            res["n_aplicadas"] += 1
            res["total_aplicado"] = round(res["total_aplicado"] + rete, 2)
        elif estado == "registrada":
            res["n_registradas"] += 1
        elif estado == "ya":
            res["n_ya"] += 1
        elif estado == "sin_factura":
            res["n_sin_factura"] += 1
        elif estado in ("rete_0", "rete_gt_importe", "rete_gt_saldo"):
            res["n_error"] += 1
    return res


def _desaplicar_una_por_numero(numero: str, usuario: str) -> str:
    """Revierte la retención Asinfo aplicada a la factura `numero`: restaura
    saldo/abono/stat del snapshot, borra la scintela.retencion y marca el
    mov_doble reversado. Devuelve 'revertida' | 'sin_aplicacion'.
    """
    with db.tx() as conn:
        f = db.fetch_one(
            "SELECT id_factura, codigo_cli, numf, importe, retencion "
            "  FROM scintela.factura "
            "WHERE numf_completo = %s ORDER BY id_factura LIMIT 1 FOR UPDATE",
            (numero,), conn=conn,
        )
        if not f:
            return "sin_aplicacion"
        mv = db.fetch_one(
            "SELECT id_mov_doble, metadata FROM scintela.mov_doble "
            "WHERE tipo = 'retencion_asinfo_aplicada' AND origen_id = %s "
            "  AND estado = 'activo' "
            "ORDER BY id_mov_doble DESC LIMIT 1",
            (f["id_factura"],), conn=conn,
        )
        if not mv:
            return "sin_aplicacion"
        meta = mv.get("metadata") or {}
        if isinstance(meta, str):
            import json as _json
            try:
                meta = _json.loads(meta)
            except Exception:
                meta = {}
        # Restaurar factura. TMT 2026-08-07: desde la mig 0179 la retención
        # tiene columna propia, así que el reverso también la tiene que
        # devolver. Los snapshots VIEJOS no traen `retencion_previa` — para
        # esos, "lo de antes" es la retención actual menos la que estamos
        # sacando; nunca 0, porque una factura puede tener más de una encima.
        _rete = round(float(meta.get("rete") or 0), 2)
        _ret_actual = round(float(f.get("retencion") or 0), 2)
        _ret_previa = meta.get("retencion_previa")
        _ret_previa = (
            round(float(_ret_previa), 2) if _ret_previa is not None
            else max(round(_ret_actual - _rete, 2), 0.0)
        )
        db.execute(
            "UPDATE scintela.factura "
            "   SET abono = %s, retencion = %s, saldo = %s, stat = %s, "
            "       usuario_modifica = %s "
            " WHERE id_factura = %s",
            (round(float(meta.get("abono_previo") or 0), 2),
             _ret_previa,
             round(float(meta.get("saldo_previo") or 0), 2),
             (meta.get("stat_previo") or "Z"), usuario, f["id_factura"]),
            conn=conn,
        )
        # Borrar la retención registrada
        id_ret = meta.get("id_retencion")
        if id_ret:
            db.execute(
                "DELETE FROM scintela.retencion WHERE id_retencion = %s",
                (id_ret,), conn=conn,
            )
        else:
            db.execute(
                "DELETE FROM scintela.retencion WHERE codigo_cli = %s AND numf = %s",
                (f["codigo_cli"], f["numf"]), conn=conn,
            )
        # Marcar el mov_doble como reversado (reverso administrativo)
        _md.registrar(
            conn=conn,
            tipo="retencion_asinfo_desaplicada",
            origen_table="factura", origen_id=f["id_factura"],
            destino_table="factura", destino_id=f["id_factura"],
            importe=round(float(meta.get("rete") or 0), 2) or 1.0,
            fecha=today_ec(),
            concepto=f"REVERSO retención Asinfo {numero} {f['codigo_cli']}"[:200],
            usuario=usuario,
            id_original=mv["id_mov_doble"],
            metadata={"numero": numero, "codigo_cli": f["codigo_cli"]},
        )
    return "revertida"


def desaplicar_retenciones_asinfo(desde, hasta, usuario: str = "web") -> dict:
    """Deshace las retenciones Asinfo aplicadas en el período (restaura saldos
    y borra las scintela.retencion). Idempotente. {n_revertidas, n_sin}."""
    from modules.asinfo import service as asinfo_service
    ret_map = asinfo_service.retenciones_periodo(desde, hasta) or {}
    res = {"n_revertidas": 0, "n_sin": 0}
    for numero in ret_map:
        try:
            estado = _desaplicar_una_por_numero(numero, usuario)
        except Exception:
            res["n_sin"] += 1
            continue
        if estado == "revertida":
            res["n_revertidas"] += 1
        else:
            res["n_sin"] += 1
    return res


# ---------------------------------------------------------------------------
# Corrección de la DOBLE aplicación de retenciones (TMT 2026-07-23, dueña).
# Antes del fix, PC sumaba la retención al abono aunque el dBase legacy ya la
# hubiese aplicado (abono_previo>0). Esto duplicó el abono en toda factura que
# venía del dBase con la retención adentro. Esta corrección:
#   - toma los mov_doble 'retencion_asinfo_aplicada' ACTIVOS con abono_previo>0
#     (= los que se aplicaron sobre un abono que ya existía = los dobles),
#   - RESTA la retención del abono (preservando cheques/cobranzas posteriores),
#     sube el saldo y recomputa stat,
#   - MANTIENE scintela.retencion (para el informe/SRI y el guard),
#   - marca el mov_doble original reversado → idempotente (no corrige 2 veces).
# dry_run=True sólo cuenta (para el preview de la pantalla, sin mutar nada).
# ---------------------------------------------------------------------------
def _movs_doble_retencion(limite: int = 20000) -> list[dict]:
    return db.fetch_all(
        "SELECT id_mov_doble, origen_id, importe, metadata "
        "  FROM scintela.mov_doble "
        " WHERE tipo = 'retencion_asinfo_aplicada' AND estado = 'activo' "
        " ORDER BY id_mov_doble "
        " LIMIT %s",
        (limite,),
    ) or []


def _meta_dict(mv: dict) -> dict:
    meta = mv.get("metadata") or {}
    if isinstance(meta, str):
        import json as _json
        try:
            meta = _json.loads(meta)
        except Exception:
            meta = {}
    return meta


def corregir_doble_retenciones(usuario: str = "web", dry_run: bool = True) -> dict:
    """Cuenta (ya NO corrige) las retenciones que el dBase había duplicado.

    Fue una limpieza de una sola vez: mientras el dBase sincronizaba, una
    retención podía quedar sumada dos veces al abono, y esto restaba la
    diferencia DEL ABONO. Desde la migración 0179 (2026-08-07) la retención no
    vive más adentro del abono, así que aquella resta le sacaría a la factura
    plata que ahí ya no está — abono corto, saldo inflado, y en silencio. El
    bloque que escribía se borró en vez de dejarlo detrás de un `if`: una
    fórmula vieja guardada "por las dudas" es exactamente lo que después
    alguien vuelve a llamar.

    Queda el CONTEO, que sirve para mirar el histórico. No tiene pantalla ni
    ruta (nunca la tuvo). Borrar junto con el resto del código dBase en
    septiembre.

    Devuelve {n_doble, n_corregidas (siempre 0), total_corregido,
    n_skip_no_doble, n_error}.
    """
    if not dry_run:
        raise RuntimeError(
            "corregir_doble_retenciones ya no corrige nada: con la migración "
            "0179 la retención dejó de guardarse adentro del abono, así que "
            "esta corrección restaría de donde no corresponde. Sólo cuenta "
            "(dry_run=True)."
        )
    res = {
        "n_doble": 0, "n_corregidas": 0, "total_corregido": 0.0,
        "n_skip_no_doble": 0, "n_error": 0,
    }
    for mv in _movs_doble_retencion():
        meta = _meta_dict(mv)
        abono_previo = round(float(meta.get("abono_previo") or 0), 2)
        rete = round(float(meta.get("rete") if meta.get("rete") is not None
                           else mv.get("importe") or 0), 2)
        # Un doble = se aplicó sobre un abono que ya existía (dBase ya lo tenía).
        if abono_previo <= 0.005 or rete <= 0.005:
            res["n_skip_no_doble"] += 1
            continue
        res["n_doble"] += 1
        res["total_corregido"] = round(res["total_corregido"] + rete, 2)
    return res


def _match_asinfo_a_facturas(numeros: list[str]) -> tuple[dict, set, set, dict]:
    """Para una lista de N° SRI de Asinfo, devuelve a qué factura de PC va cada uno.

    Es el MISMO match que hace `_factura_por_numero` de a una, pero en batch: por
    `numf_completo` y, si no aparece, por el último grupo de dígitos del SRI (las
    facturas de origen dBase tienen `numf_completo` NULL). Vive en un solo lugar
    porque ya se pagó el precio de tenerlo duplicado: cuando el aplicador ganó la
    segunda rama y la pantalla no, el preview daba 896 "sin factura en PC" falsas
    contra 45 reales (03/08/2026).

    Devuelve `(fac_by_num, via_numf, ya_set, nombres)`:
      · `fac_by_num` — {numero_sri: fila de factura}
      · `via_numf`   — los que matchearon por el fallback (para el badge)
      · `ya_set`     — {"codigo_cli|numf"} que YA tienen fila en scintela.retencion
      · `nombres`    — {codigo_cli: nombre del cliente}
    """
    # Facturas vivas de PC por numf_completo (mismo filtro que _factura_por_numero:
    # no backfill, no anulada 'X'). ORDER BY id_factura → nos quedamos con la 1ra.
    fac_rows = db.fetch_all(
        """
        SELECT id_factura, codigo_cli, numf, numf_completo, fecha,
               importe, abono, retencion, saldo, stat
          FROM scintela.factura
         WHERE numf_completo = ANY(%s)
           AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
           AND COALESCE(stat, '') <> 'X'
         ORDER BY id_factura
        """,
        (numeros,),
    )
    fac_by_num: dict = {}
    for f in fac_rows:
        fac_by_num.setdefault(f["numf_completo"], f)

    # TMT 2026-08-03 (dueña: "por qué no tiene aplicada?"): ESPEJO del fallback
    # por numf de `_factura_por_numero`. Las facturas de origen dBase tienen
    # numf_completo NULL (import_dbf no lo escribe: el DBF sólo trae el entero),
    # así que el match por SRI completo falla y esta pantalla las daba TODAS por
    # "sin factura en PC" — 896 falsas contra 45 reales el 03/08. El aplicador
    # sí las encontraba, o sea la pantalla mentía y el botón no.
    via_numf: set = set()
    faltan = {n: _numf_sri(n) for n in numeros if n not in fac_by_num}
    faltan = {n: v for n, v in faltan.items() if v is not None}
    if faltan:
        por_numf: dict = {}
        for f in db.fetch_all(
            """
            SELECT id_factura, codigo_cli, numf, numf_completo, fecha,
                   importe, abono, retencion, saldo, stat
              FROM scintela.factura
             WHERE numf = ANY(%s)
               AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
               AND COALESCE(stat, '') <> 'X'
             ORDER BY id_factura
            """,
            (sorted(set(faltan.values())),),
        ):
            por_numf.setdefault(f["numf"], f)
        for numero_, numf_ in faltan.items():
            f = por_numf.get(numf_)
            if f is not None:
                fac_by_num[numero_] = f
                via_numf.add(numero_)

    # Retenciones ya registradas para esas facturas (set de pares codigo_cli|numf).
    codigos = list({f["codigo_cli"] for f in fac_by_num.values()})
    numfs = list({f["numf"] for f in fac_by_num.values()})
    ya_set: set = set()
    nombres: dict = {}
    if codigos:
        for rr in db.fetch_all(
            "SELECT codigo_cli, numf FROM scintela.retencion "
            "WHERE codigo_cli = ANY(%s) AND numf = ANY(%s)",
            (codigos, numfs or [0]),
        ):
            ya_set.add(f"{rr['codigo_cli']}|{rr['numf']}")
        for cr in db.fetch_all(
            "SELECT codigo_cli, COALESCE(nombre, '') AS nombre "
            "FROM scintela.cliente WHERE codigo_cli = ANY(%s)",
            (codigos,),
        ):
            nombres[cr["codigo_cli"]] = cr["nombre"]

    return fac_by_num, via_numf, ya_set, nombres


def preview_retencion_en_abono(desde, hasta) -> dict:
    """DRY RUN, no escribe nada: las retenciones VIEJAS que quedaron adentro del
    abono y cuánto se movería a la columna Retención.

    El problema (dueña, 07/08/2026: *"todas las retenciones están en abono"*):
    mientras el dBase fue la fuente de verdad, RETENCIO.PRG sumaba la retención
    al abono de la factura y Programa Core nunca se enteró de que ese pedazo del
    abono era una retención — no hay fila en `scintela.retencion`. La migración
    0179 sólo pudo separar las que sí tenían fila (1.461). El resto sigue
    diciendo "Abonado" cuando en realidad el cliente retuvo esa plata.

    ⚠️ Estas NO se pueden "aplicar" como una retención nueva: el aplicador
    DESCUENTA del saldo, y acá el saldo ya está descontado (el abono lo bajó en
    su momento). Lo único correcto es MOVER: `abono -= rete`, `retencion +=
    rete`, y el saldo queda EXACTAMENTE igual — la misma operación que hizo el
    backfill de la 0179. Por eso esta pantalla es su propio preview y no reusa
    el de `/facturas/retenciones-asinfo`.

    Quién dice cuánto es: **Asinfo**, no un porcentaje. Se cruza el N° SRI y se
    usa el importe exacto que retuvo el cliente. Nada se deduce de que el abono
    "parezca" un 1,74%.

    Devuelve {"filas": [...], "resumen": {...}}. Estados:
      · `se_mueve`     — hay abono suficiente: se mueve `rete` de abono a
                         retención y el saldo no cambia.
      · `cerrada`      — la factura ya está totalizada o cancelada (stat T, o
                         saldo 0). **No se toca** (dueña, 07/08/2026), y no
                         hay nada que ganar: las T no se listan en el estado de
                         cuenta, y con saldo 0 la resta da igual antes y
                         después — el movimiento no se vería en ningún lado.
      · `ya_separada`  — ya tiene su fila / su columna: no hay nada que hacer.
      · `abono_corto`  — el abono es menor que la retención de Asinfo: mover
                         dejaría el abono negativo. Va a mano (¿pago parcial?
                         ¿la retención nunca entró?).
      · `sin_factura`  — no está en PC.
    """
    from modules.asinfo import service as asinfo_service

    ret_map = asinfo_service.retenciones_periodo(desde, hasta) or {}
    resumen = {
        "n_total": len(ret_map), "se_mueve": 0, "ya_separada": 0,
        "abono_corto": 0, "sin_factura": 0, "via_numf": 0, "cerrada": 0,
        "total_a_mover": 0.0, "total_cerradas": 0.0,
        "total_periodo": round(
            sum(float((v or {}).get("ret_total") or 0) for v in ret_map.values()), 2),
    }
    if not ret_map:
        return {"filas": [], "resumen": resumen}

    fac_by_num, via_numf, ya_set, nombres = _match_asinfo_a_facturas(
        list(ret_map.keys()))

    filas: list = []
    for numero, r in ret_map.items():
        rete = round(float((r or {}).get("ret_total") or 0), 2)
        f = fac_by_num.get(numero)
        fila = {
            "numero": numero, "ret_total": rete, "via_numf": numero in via_numf,
            "codigo_cli": None, "cliente": None, "numf": None, "fecha": None,
            "importe": None, "abono_actual": None, "abono_nuevo": None,
            "retencion_nueva": None, "saldo": None, "estado": None,
        }
        if fila["via_numf"]:
            resumen["via_numf"] += 1
        if rete <= 0.005 or not f:
            fila["estado"] = "sin_factura"
            resumen["sin_factura"] += 1
            filas.append(fila)
            continue
        importe = round(float(f["importe"] or 0), 2)
        abono = round(float(f["abono"] or 0), 2)
        retencion = round(float(f["retencion"] or 0), 2)
        saldo = round(float(f["saldo"] or 0), 2)
        fila.update({
            "codigo_cli": f["codigo_cli"],
            "cliente": nombres.get(f["codigo_cli"], ""),
            "numf": f["numf"], "fecha": f.get("fecha"),
            "importe": importe, "abono_actual": abono, "saldo": saldo,
        })
        if retencion > 0.005 or f"{f['codigo_cli']}|{f['numf']}" in ya_set:
            fila["estado"] = "ya_separada"
            resumen["ya_separada"] += 1
        elif (f.get("stat") or "").strip().upper() == "T" or abs(saldo) <= 0.005:
            # TMT 2026-08-07 (dueña): *"si la factura ya fue totalizada o
            # cancelada no la toques"*, y tiene razón por partida doble: las T
            # ni siquiera se listan en el estado de cuenta (filtro del
            # 11/06), y con saldo 0 la cuenta cierra igual antes y después
            # — mover ahí es tocar una factura cerrada sin que nadie vea la
            # diferencia.
            fila["estado"] = "cerrada"
            resumen["cerrada"] += 1
            resumen["total_cerradas"] = round(resumen["total_cerradas"] + rete, 2)
        elif abono + 0.01 < rete:
            fila["estado"] = "abono_corto"
            resumen["abono_corto"] += 1
        else:
            fila["estado"] = "se_mueve"
            fila["abono_nuevo"] = round(abono - rete, 2)
            fila["retencion_nueva"] = rete
            resumen["se_mueve"] += 1
            resumen["total_a_mover"] = round(resumen["total_a_mover"] + rete, 2)
        filas.append(fila)

    orden = {"se_mueve": 0, "abono_corto": 1, "cerrada": 2, "ya_separada": 3,
             "sin_factura": 4}
    filas.sort(key=lambda x: (orden.get(x["estado"], 9), -(x["ret_total"] or 0)))
    return {"filas": filas, "resumen": resumen}


def preview_retenciones_asinfo(desde, hasta) -> dict:
    """Read-only: por cada retención de Asinfo del período, dice A QUÉ factura de
    PC iría y QUÉ pasaría, SIN mutar nada. Espeja la clasificación de
    `_aplicar_una_por_numero` pero en batch (una query por cosa).

    Devuelve {"filas": [...], "resumen": {...}}. Cada fila:
      numero, ret_fuente, ret_iva, ret_total, estado, codigo_cli, cliente,
      numf, importe, saldo_actual, saldo_nuevo, stat_nuevo.
    estado ∈ {se_aplica, ya, sin_factura, rete_gt_importe, rete_gt_saldo, rete_0}.
    """
    from modules.asinfo import service as asinfo_service
    ret_map = asinfo_service.retenciones_periodo(desde, hasta) or {}
    resumen = {
        "n_total": len(ret_map), "se_aplica": 0, "ya": 0, "sin_factura": 0,
        "rete_gt_importe": 0, "rete_gt_saldo": 0, "rete_0": 0,
        "total_a_aplicar": 0.0, "via_numf": 0,
        "total_periodo": round(
            sum(float((v or {}).get("ret_total") or 0) for v in ret_map.values()), 2),
    }
    if not ret_map:
        return {"filas": [], "resumen": resumen}

    # `via_numf` lo cuenta el bucle de abajo, fila por fila.
    fac_by_num, via_numf, ya_set, nombres = _match_asinfo_a_facturas(
        list(ret_map.keys()))

    filas: list = []
    for numero, r in ret_map.items():
        rf = float((r or {}).get("ret_fuente") or 0)
        ri = float((r or {}).get("ret_iva") or 0)
        rete = round(float((r or {}).get("ret_total") or 0), 2)
        fila = {
            "numero": numero, "ret_fuente": rf, "ret_iva": ri, "ret_total": rete,
            "codigo_cli": None, "cliente": None, "numf": None,
            "importe": None, "saldo_actual": None, "saldo_nuevo": None,
            "stat_nuevo": None, "estado": None,
            "via_numf": numero in via_numf,
        }
        if fila["via_numf"]:
            resumen["via_numf"] += 1
        f = fac_by_num.get(numero)
        if rete <= 0.005:
            fila["estado"] = "rete_0"
            resumen["rete_0"] += 1
        elif not f:
            fila["estado"] = "sin_factura"
            resumen["sin_factura"] += 1
        else:
            importe = round(float(f["importe"] or 0), 2)
            abono = round(float(f["abono"] or 0), 2)
            retencion = round(float(f["retencion"] or 0), 2)
            saldo = round(float(f["saldo"] or 0), 2)
            fila.update({
                "codigo_cli": f["codigo_cli"],
                "cliente": nombres.get(f["codigo_cli"], ""),
                "numf": f["numf"], "importe": importe, "saldo_actual": saldo,
            })
            if rete > importe + 0.01:
                fila["estado"] = "rete_gt_importe"
                resumen["rete_gt_importe"] += 1
            elif f"{f['codigo_cli']}|{f['numf']}" in ya_set:
                fila["estado"] = "ya"
                resumen["ya"] += 1
            elif rete > saldo + 0.01:
                # Espejo del freno del aplicador (2026-08-06): dejaría el
                # saldo negativo → no se aplica sola.
                fila["estado"] = "rete_gt_saldo"
                resumen["rete_gt_saldo"] += 1
            else:
                # Espejo EXACTO del aplicador (misma función, no una copia
                # de la cuenta): la retención baja el saldo sin tocar el abono.
                saldo_new = _fact_q.saldo_de(importe, abono, rete + retencion)
                fila["saldo_nuevo"] = saldo_new
                fila["stat_nuevo"] = "T" if saldo_new <= 0.005 else "A"
                fila["estado"] = "se_aplica"
                resumen["se_aplica"] += 1
                resumen["total_a_aplicar"] = round(resumen["total_a_aplicar"] + rete, 2)
        filas.append(fila)

    # Orden: primero lo que se aplica, después ya, después los que no entran.
    orden = {"se_aplica": 0, "ya": 1, "rete_gt_importe": 2, "rete_gt_saldo": 2,
             "sin_factura": 3, "rete_0": 4}
    filas.sort(key=lambda x: (orden.get(x["estado"], 9), -(x["ret_total"] or 0)))
    return {"filas": filas, "resumen": resumen}
