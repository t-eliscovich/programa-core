"""Consultas de retenciones en la fuente emitidas por clientes.

scintela.retencion: id_retencion, codigo_cli, rete, numf, fecha
Se liga a scintela.factura por (codigo_cli, numf).
"""
from datetime import date

import db
from filters import today_ec
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
               OR UPPER(r.codigo_cli) LIKE UPPER(%(like)s)
               OR UPPER(COALESCE(c.nombre,'')) LIKE UPPER(%(like)s)
               OR CAST(r.numf AS TEXT) LIKE %(like)s
               OR UPPER(COALESCE(f.numf_completo,'')) LIKE UPPER(%(like)s))
          AND (%(desde)s::date IS NULL OR r.fecha >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR r.fecha <= %(hasta)s::date)
        ORDER BY r.fecha DESC, r.id_retencion DESC
        LIMIT %(limite)s
        """,
        {
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
               saldo, stat
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
    import re as _re
    _m = _re.findall(r"\d+", str(numero or ""))
    if not _m:
        return None
    try:
        _numf = int(_m[-1])
    except (ValueError, TypeError):
        return None
    return db.fetch_one(
        """
        SELECT id_factura, codigo_cli, numf, numf_completo, importe, abono,
               saldo, stat
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
                            batch_id: str | None = None) -> str:
    """Registra scintela.retencion + baja el saldo de la factura `numero`.

    Devuelve: 'aplicada' | 'ya' (ya tenía retención) | 'sin_factura' |
    'rete_0' | 'rete_gt_importe'.
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
        saldo = round(float(f["saldo"] or 0), 2)
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
        # TMT 2026-07-23 (dueña): NO DUPLICAR. El dBase legacy (RETENCIO.PRG)
        # ya aplica la retención del cliente como abono (abono+=rete,
        # saldo-=rete, stat 'A') y ese abono entra a PC por el sync. Si la
        # factura YA tiene abono, la retención ya está reflejada en el saldo
        # → sólo REGISTRAMOS scintela.retencion (para el informe/SRI y el
        # guard anti-reaplicación) y NO tocamos abono/saldo. Sólo bajamos el
        # saldo cuando la factura no tiene abono (fresca de Asinfo, el dBase
        # todavía no la aplicó). Antes se sumaba siempre → doble conteo del
        # abono en toda factura que venía del dBase con la retención adentro.
        if abono <= 0.005:
            abono_new = round(abono + rete, 2)
            saldo_new = round(importe - abono_new, 2)
            stat_new = "T" if saldo_new <= 0.005 else "A"
            db.execute(
                "UPDATE scintela.factura "
                "   SET abono = %s, saldo = %s, stat = %s, usuario_modifica = %s "
                " WHERE id_factura = %s",
                (abono_new, saldo_new, stat_new, usuario, f["id_factura"]),
                conn=conn,
            )
            aplicado = True
            concepto = (
                f"RETENCIÓN Asinfo {rete:.2f} aplicada a "
                f"{numero} {f['codigo_cli']} — saldo {saldo:.2f}→{saldo_new:.2f}"
            )
        else:
            # dBase ya aplicó el abono → sólo dejamos registrada la retención.
            aplicado = False
            concepto = (
                f"RETENCIÓN Asinfo {rete:.2f} REGISTRADA (el dBase ya aplicó "
                f"el abono) — {numero} {f['codigo_cli']}, abono {abono:.2f}"
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
                "rete": rete, "abono_previo": abono, "saldo_previo": saldo,
                "stat_previo": stat_prev, "aplicado": aplicado,
            },
        )
    return "aplicada" if aplicado else "registrada"


def aplicar_retenciones_asinfo(desde, hasta, usuario: str = "web") -> dict:
    """Trae las retenciones de Asinfo del período y las aplica a las facturas
    de PC (registra scintela.retencion + baja el saldo). Idempotente.

    Devuelve {n_aplicadas, n_ya, n_sin_factura, n_error, total_aplicado,
    n_retenciones_asinfo}.
    """
    from modules.asinfo import service as asinfo_service
    ret_map = asinfo_service.retenciones_periodo(desde, hasta) or {}
    res = {
        "n_aplicadas": 0, "n_registradas": 0, "n_ya": 0, "n_sin_factura": 0,
        "n_error": 0, "total_aplicado": 0.0, "n_retenciones_asinfo": len(ret_map),
    }
    batch_id = None  # cada factura es su propia tx; sin batch atómico
    for numero, r in ret_map.items():
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
        elif estado in ("rete_0", "rete_gt_importe"):
            res["n_error"] += 1
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
    batch_id = None
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
        elif estado in ("rete_0", "rete_gt_importe"):
            res["n_error"] += 1
    return res


def _desaplicar_una_por_numero(numero: str, usuario: str) -> str:
    """Revierte la retención Asinfo aplicada a la factura `numero`: restaura
    saldo/abono/stat del snapshot, borra la scintela.retencion y marca el
    mov_doble reversado. Devuelve 'revertida' | 'sin_aplicacion'.
    """
    with db.tx() as conn:
        f = db.fetch_one(
            "SELECT id_factura, codigo_cli, numf, importe FROM scintela.factura "
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
        # Restaurar factura
        db.execute(
            "UPDATE scintela.factura "
            "   SET abono = %s, saldo = %s, stat = %s, usuario_modifica = %s "
            " WHERE id_factura = %s",
            (round(float(meta.get("abono_previo") or 0), 2),
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


def corregir_doble_retenciones(usuario: str = "web", dry_run: bool = False) -> dict:
    """Desduplica las retenciones aplicadas sobre un abono preexistente del dBase.

    Devuelve {n_doble, n_corregidas, total_corregido, n_skip_no_doble, n_error}.
    Con dry_run=True no muta nada: sólo cuenta los dobles (para el preview).
    """
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
        if dry_run:
            continue
        try:
            with db.tx() as conn:
                f = db.fetch_one(
                    "SELECT id_factura, importe, abono, saldo "
                    "  FROM scintela.factura WHERE id_factura = %s FOR UPDATE",
                    (mv["origen_id"],), conn=conn,
                )
                if not f:
                    res["n_error"] += 1
                    continue
                importe = round(float(f["importe"] or 0), 2)
                abono = round(float(f["abono"] or 0), 2)
                abono_new = round(abono - rete, 2)
                if abono_new < 0:
                    abono_new = 0.0  # guard defensivo (no bajar de 0)
                saldo_new = round(importe - abono_new, 2)
                stat_new = "T" if saldo_new <= 0.005 else "A"
                db.execute(
                    "UPDATE scintela.factura "
                    "   SET abono = %s, saldo = %s, stat = %s, "
                    "       usuario_modifica = %s "
                    " WHERE id_factura = %s",
                    (abono_new, saldo_new, stat_new, usuario, f["id_factura"]),
                    conn=conn,
                )
                _md.registrar(
                    conn=conn,
                    tipo="retencion_doble_corregida",
                    origen_table="factura", origen_id=f["id_factura"],
                    destino_table="factura", destino_id=f["id_factura"],
                    importe=rete,
                    fecha=today_ec(),
                    concepto=(
                        f"CORRECCIÓN doble retención −{rete:.2f} — "
                        f"abono {abono:.2f}→{abono_new:.2f}, "
                        f"saldo →{saldo_new:.2f}"
                    )[:200],
                    usuario=usuario,
                    id_original=mv["id_mov_doble"],
                    metadata={
                        "rete": rete, "abono_antes": abono,
                        "abono_despues": abono_new, "saldo_nuevo": saldo_new,
                        "numf": meta.get("numf"), "codigo_cli": meta.get("codigo_cli"),
                    },
                )
                res["n_corregidas"] += 1
        except Exception:
            res["n_error"] += 1
    return res


def preview_retenciones_asinfo(desde, hasta) -> dict:
    """Read-only: por cada retención de Asinfo del período, dice A QUÉ factura de
    PC iría y QUÉ pasaría, SIN mutar nada. Espeja la clasificación de
    `_aplicar_una_por_numero` pero en batch (una query por cosa).

    Devuelve {"filas": [...], "resumen": {...}}. Cada fila:
      numero, ret_fuente, ret_iva, ret_total, estado, codigo_cli, cliente,
      numf, importe, saldo_actual, saldo_nuevo, stat_nuevo.
    estado ∈ {se_aplica, ya, sin_factura, rete_gt_importe, rete_0}.
    """
    from modules.asinfo import service as asinfo_service
    ret_map = asinfo_service.retenciones_periodo(desde, hasta) or {}
    resumen = {
        "n_total": len(ret_map), "se_aplica": 0, "ya": 0, "sin_factura": 0,
        "rete_gt_importe": 0, "rete_0": 0, "total_a_aplicar": 0.0,
        "total_periodo": round(
            sum(float((v or {}).get("ret_total") or 0) for v in ret_map.values()), 2),
    }
    if not ret_map:
        return {"filas": [], "resumen": resumen}

    numeros = list(ret_map.keys())
    # Facturas vivas de PC por numf_completo (mismo filtro que _factura_por_numero:
    # no backfill, no anulada 'X'). ORDER BY id_factura → nos quedamos con la 1ra.
    fac_rows = db.fetch_all(
        """
        SELECT id_factura, codigo_cli, numf, numf_completo, importe, abono, saldo, stat
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
        }
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
            else:
                saldo_new = round(importe - round(abono + rete, 2), 2)
                fila["saldo_nuevo"] = saldo_new
                fila["stat_nuevo"] = "T" if saldo_new <= 0.005 else "A"
                fila["estado"] = "se_aplica"
                resumen["se_aplica"] += 1
                resumen["total_a_aplicar"] = round(resumen["total_a_aplicar"] + rete, 2)
        filas.append(fila)

    # Orden: primero lo que se aplica, después ya, después los que no entran.
    orden = {"se_aplica": 0, "ya": 1, "rete_gt_importe": 2, "sin_factura": 3, "rete_0": 4}
    filas.sort(key=lambda x: (orden.get(x["estado"], 9), -(x["ret_total"] or 0)))
    return {"filas": filas, "resumen": resumen}
