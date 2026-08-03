"""/admin/debug-kg-por-mes — ¿los kilos del hilado se duplican, o faltan?

TMT 2026-08-03. Una sesión paralela propuso "una importación = una compra POR
MES", con este diagnóstico: *"la conversión repite los kilos por diseño, la haga
quien la haga"*, y que eso explicaba un gap contra el dBase de **+97.673 kg** y
después **+20.396**.

La primera medición no lo confirmó: las dos compras de AI 15 tienen `kg = 0` y
`convertir_a_compra` recibe `kg=None` de los dos caminos. Pero la dueña marcó lo
que faltaba: *"pero los kg, ¿no vienen de otro lado?"*. Y sí — el kg de una
compra BAP se **reconstruye** desde la importación (`kg_hilado_faltantes_mes`),
así que la duplicación, si existe, está ahí y no en `compra.kg`.

Este endpoint mide eso, mes por mes, y sin tocar nada:

    SUM(compra.kg)      lo que traen las compras por su cuenta
  + reconstruido        lo que agrega kg_hilado_faltantes_mes (dedup por im)
  = kcom                lo que usa el balance
  vs asinfo_recibido    los kilos que Asinfo dice que entraron ese mes
  vs grupo              lo que daría kg_hilado_mes (una vez por GRUPO)

`kcom − asinfo_recibido` es el gap, **con signo**: positivo = el balance cuenta
kilos de más (la tesis de la otra sesión), negativo = cuenta de menos.

Y para el detalle: por cada (prov, nº) del mes con MÁS DE UNA compra, a qué
importación fue a parar cada una. Si dos compras del mismo código caen en
importaciones distintas que **no** son mitades de la misma factura, ahí está la
duplicación — cada una aporta sus kilos y nadie las dedupea.

SOLO LECTURA. No escribe, no cachea, no cambia ningún número.

    ?meses=12
"""
from __future__ import annotations

import json

from flask import Blueprint, Response, request

import db
from auth import requiere_login, requiere_permiso
from filters import today_ec

bp = Blueprint("admin_debug_kg_por_mes", __name__,
               url_prefix="/admin/debug-kg-por-mes")

_SQL = """
    SELECT codigo_prov AS prov,
           NULLIF(regexp_replace(COALESCE(concepto,''),'[^0-9]','','g'),'')::bigint AS ref,
           id_compra, fecha,
           COALESCE(kg, 0)      AS kg,
           COALESCE(importe, 0) AS importe
      FROM scintela.compra
     WHERE UPPER(TRIM(COALESCE(tipo,''))) = 'H'
       AND COALESCE(stat, '') NOT IN ('X', 'Y')
       AND EXTRACT(YEAR  FROM fecha) = %s
       AND EXTRACT(MONTH FROM fecha) = %s
       AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
     ORDER BY fecha, id_compra
"""


@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def run():
    from modules.asinfo import service as asvc
    from modules.importaciones import service as isvc

    try:
        meses = max(1, min(36, int(request.args.get("meses") or 12)))
    except (TypeError, ValueError):
        return Response(json.dumps({"ok": False, "error": "meses invalido"}),
                        status=400, mimetype="application/json")

    hoy = today_ec()
    periodos = []
    y, m = hoy.year, hoy.month
    for _ in range(meses):
        periodos.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12

    try:
        index = isvc._index_importaciones_por_codigo(limite=1000)
    except Exception as e:  # noqa: BLE001
        return Response(json.dumps({"ok": False, "error": f"Asinfo: {e}"}),
                        status=502, mimetype="application/json")

    salida = []
    for (yy, mm) in periodos:
        try:
            rows = db.fetch_all(_SQL, (yy, mm)) or []
        except Exception as e:  # noqa: BLE001
            salida.append({"mes": f"{yy}-{mm:02d}", "error": str(e)[:150]})
            continue
        sum_kg = round(sum(float(r.get("kg") or 0) for r in rows), 2)
        try:
            recon = float(isvc.kg_hilado_faltantes_mes(rows).get("kg") or 0)
        except Exception:  # noqa: BLE001
            recon = 0.0
        try:
            grupo = float(isvc.kg_hilado_mes(rows).get("kg") or 0)
        except Exception:  # noqa: BLE001
            grupo = 0.0
        try:
            asinfo = float(asvc.hilado_recibido_mes(yy, mm, limite=1000) or 0)
        except Exception:  # noqa: BLE001
            asinfo = 0.0
        kcom = round(sum_kg + recon, 2)

        # Detalle: (prov, nº) con más de una compra → a qué importación fue cada una.
        por_ref: dict = {}
        for r in rows:
            prov = str(r.get("prov") or "").strip().upper()
            ref = r.get("ref")
            if not prov or ref is None:
                continue
            cands = index.get((prov, int(ref))) or []
            im = isvc._nearest_import(cands, r.get("fecha"))
            por_ref.setdefault(f"{prov} {ref}", []).append({
                "id_compra": r.get("id_compra"),
                "fecha": str(r.get("fecha"))[:10],
                "kg_compra": float(r.get("kg") or 0),
                "importe": float(r.get("importe") or 0),
                "im": (im or {}).get("im_numero"),
                "im_kg": (im or {}).get("kg"),
                "grupo_id": (im or {}).get("grupo_id"),
            })
        multi = {}
        for k, v in por_ref.items():
            if len(v) < 2:
                continue
            ims = {x["im"] for x in v if x["im"]}
            gids = {x["grupo_id"] for x in v if x["grupo_id"]}
            multi[k] = {
                "compras": v,
                "n_importaciones_distintas": len(ims),
                "n_grupos_distintos": len(gids),
                # ⚠️ ACÁ estaría la duplicación: dos compras del mismo código
                # atribuidas a GRUPOS distintos, cada una aportando sus kilos.
                "duplica_kilos": len(gids) > 1,
            }

        salida.append({
            "mes": f"{yy}-{mm:02d}",
            "n_compras": len(rows),
            "sum_compra_kg": sum_kg,
            "reconstruido": round(recon, 2),
            "kcom_que_usa_el_balance": kcom,
            "asinfo_recibido": round(asinfo, 2),
            "gap_kcom_menos_asinfo": round(kcom - asinfo, 2),
            "kg_hilado_mes_por_grupo": round(grupo, 2),
            "gap_grupo_menos_asinfo": round(grupo - asinfo, 2),
            "refs_con_varias_compras": multi,
        })

    con_dup = [s["mes"] for s in salida
               if any(v.get("duplica_kilos")
                      for v in (s.get("refs_con_varias_compras") or {}).values())]
    return Response(json.dumps({
        "ok": True,
        "lectura": (
            "gap_kcom_menos_asinfo POSITIVO = el balance cuenta kilos de MAS "
            "(la tesis de 'la conversion repite los kilos'). NEGATIVO = cuenta "
            "de menos. 'duplica_kilos' marca los codigos con dos compras "
            "atribuidas a GRUPOS de importacion distintos, que es la unica via "
            "por la que la reconstruccion puede sumar dos veces."
        ),
        "meses_con_duplicacion": con_dup,
        "meses": salida,
    }, indent=2, default=str, ensure_ascii=False), mimetype="application/json")
