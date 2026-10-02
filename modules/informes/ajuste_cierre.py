"""Ajuste a la FOTO DE CIERRE de un mes ya cerrado (scintela.historia).

Tamara 02/10/2026. A las 13:50 Asinfo corrigió el saldo de las bodegas: se
fueron 51.775 kg de hilo y 7.139 kg de tela cruda que figuraban en stock
desde julio (salidas de material que no habían bajado el saldo). La utilidad
de octubre bajó 191.504 de golpe, pero esos kilos ya no estaban al 30/09.
Decisión: *"vamos a absorberlo en septiembre aunque haya cosas de julio"* y
*"poné en historia un ajuste así no afecta octubre"*.

QUÉ HACE. Baja (o sube) el STOCK de la foto de cierre en los kilos que se
corrigieron, valuados al $/kg con que cerró el mes, y con él todo lo que
depende del stock en esa foto — juntos, para que el cierre siga cerrando:

    ustock     += importe        (stock MP+Prod en $)
    stock      += kg             (kilos de hilado + tejido + terminado)
    patrimonio += importe        (el PATANT del mes siguiente)
    usuti      += importe        (la utilidad del mes cerrado)

`utilidad del mes en curso = patr − PATANT`: como el stock de HOY ya no tiene
esos kilos y el PATANT tampoco, el mes en curso no los paga. El mes cerrado sí.

🚨 Se toca el ACTIVO (ustock) y el patrimonio a la vez. El ajuste del 03/09
(retirado) bajaba sólo el patrimonio, y eso rompe `historia_balance_cierra`
(activo = deuda + patrimonio). Acá no.

POR DÓNDE PASA. Sólo el cierre que hoy es PATANT (el del mes anterior): es el
único que se puede tocar sin arrastrar un mes intermedio. Queda una fila en
`scintela.ajuste_cierre` con las líneas y la foto antes/después (para
deshacerlo y para volver a aplicarlo si alguien regraba la foto de cierre:
`crear_snapshot_historia(forzar=True)` borra la fila y la inserta de nuevo,
ver `reaplicar`), y una `mov_doble` `ajuste_cierre` para el Historial.

La traza nombra el salto que produce en el mes en curso (ver
`traza.registrar` → `movimientos_de_ajustes`): sin eso sería un Δ de PATANT
sin documento.
"""
from __future__ import annotations

import json
import logging

import db
from filters import today_ec

_LOG = logging.getLogger("programa_core.ajuste_cierre")

#: Etapas del stock que se pueden ajustar, con su columna de la traza.
ETAPAS = {
    "hilado": "Hilo",
    "tejido": "Tela cruda",
    "terminado": "Terminado",
}

_MESES = ("", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
          "agosto", "septiembre", "octubre", "noviembre", "diciembre")


def nombre_mes(mes: int) -> str:
    return _MESES[mes] if 1 <= int(mes) <= 12 else str(mes)


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def cierre_vigente() -> dict | None:
    """La foto de cierre que hoy es PATANT (último día del mes anterior)."""
    from modules.informes import queries as _q
    return _q.historia_ultimo_mes()


def tarifas_del_cierre(fecha_cierre) -> dict:
    """$/kg de cada etapa con que CERRÓ el mes: la última foto de la traza
    de ese día (hora Ecuador). Es la misma valuación que tiene `ustock`."""
    row = db.fetch_one(
        """
        SELECT hilado_ukg, tejido_ukg, terminado_ukg,
               hilado_kg, tejido_kg, terminado_kg, id_traza
          FROM scintela.traza_utilidad
         WHERE (creado_en AT TIME ZONE 'America/Guayaquil')::date = %s
           AND hilado_ukg IS NOT NULL
         ORDER BY creado_en DESC, id_traza DESC
         LIMIT 1
        """,
        (fecha_cierre,),
    ) or {}
    return {e: _f(row.get(f"{e}_ukg")) for e in ETAPAS} | {
        "kg": {e: _f(row.get(f"{e}_kg")) for e in ETAPAS},
        "id_traza": row.get("id_traza"),
    }


def propuesta_desde_traza(id_traza: int) -> list[dict]:
    """Los kilos que se movieron en UNA ventana de la traza (contra la foto
    anterior), por etapa — para precargar el ajuste con la corrección que
    hizo Asinfo. Sólo propone; lo que se aplica es lo que quede en la
    pantalla."""
    filas = db.fetch_all(
        """
        SELECT id_traza, hilado_kg, tejido_kg, terminado_kg
          FROM scintela.traza_utilidad
         WHERE id_traza <= %s
         ORDER BY id_traza DESC
         LIMIT 2
        """,
        (int(id_traza),),
    ) or []
    if len(filas) < 2 or int(filas[0]["id_traza"]) != int(id_traza):
        return []
    nueva, vieja = filas[0], filas[1]
    out = []
    for e in ETAPAS:
        d = round(_f(nueva.get(f"{e}_kg")) - _f(vieja.get(f"{e}_kg")), 2)
        if abs(d) >= 1:
            out.append({"etapa": e, "kg": d})
    return out


def _normalizar(lineas, tarifas: dict) -> list[dict]:
    out = []
    for ln in lineas or []:
        e = (ln.get("etapa") or "").strip()
        if e not in ETAPAS:
            continue
        kg = round(_f(ln.get("kg")), 2)
        if not kg:
            continue
        ukg = _f(ln.get("ukg")) or _f(tarifas.get(e))
        if ukg <= 0:
            raise ValueError(f"No hay $/kg de {ETAPAS[e].lower()} para valuar los kilos.")
        out.append({"etapa": e, "kg": kg, "ukg": round(ukg, 6),
                    "importe": round(kg * ukg, 2)})
    return out


def calcular(lineas) -> dict:
    """Lo que haría el ajuste, sin escribir nada: para la vista previa."""
    hist = cierre_vigente()
    if not hist:
        raise ValueError("No hay foto de cierre del mes anterior en historia.")
    tar = tarifas_del_cierre(hist.get("fecha"))
    lns = _normalizar(lineas, tar)
    imp = round(sum(x["importe"] for x in lns), 2)
    kg = round(sum(x["kg"] for x in lns), 2)
    antes = _foto(hist)
    despues = {
        "ustock": antes["ustock"] + imp,
        "stock": antes["stock"] + kg,
        "patrimonio": antes["patrimonio"] + imp,
        "usuti": antes["usuti"] + imp,
    }
    kvent = _f(hist.get("kvent"))
    return {
        "id_historia": int(hist["id_historia"]),
        "fecha": hist.get("fecha"),
        "anio": hist["fecha"].year, "mes": hist["fecha"].month,
        "lineas": lns, "importe": imp, "kg": kg,
        "antes": antes, "despues": despues, "tarifas": tar,
        "ukg_antes": (antes["usuti"] / kvent) if kvent else None,
        "ukg_despues": (despues["usuti"] / kvent) if kvent else None,
    }


def _foto(h: dict) -> dict:
    return {k: _f(h.get(k)) for k in ("ustock", "stock", "patrimonio", "usuti")}


def aplicar(*, lineas, motivo: str, usuario: str = "web") -> dict:
    """Aplica el ajuste a la foto de cierre vigente. Devuelve lo de `calcular`
    más `id_ajuste`."""
    motivo = (motivo or "").strip()
    if not motivo:
        raise ValueError("Escribí el motivo: es lo que va a leer quien mire el cierre.")
    calc = calcular(lineas)
    if not calc["lineas"] or not calc["importe"]:
        raise ValueError("El ajuste no tiene kilos.")
    hoy = today_ec()
    fecha = calc["fecha"]
    # Sólo el cierre que hoy es PATANT: el del mes inmediatamente anterior.
    prev = (hoy.replace(day=1).toordinal() - 1)
    from datetime import date as _date
    if fecha != _date.fromordinal(prev):
        raise ValueError(
            f"La última foto de cierre es del {fecha:%d/%m/%Y}: sólo se ajusta "
            "la del último día del mes anterior.")
    with db.tx() as conn:
        db.execute("SELECT pg_advisory_xact_lock(hashtext('snapshot_historia'))",
                   conn=conn)
        row = db.fetch_one(
            "SELECT * FROM scintela.historia WHERE id_historia = %s FOR UPDATE",
            (calc["id_historia"],), conn=conn)
        if not row:
            raise ValueError("La foto de cierre cambió mientras tanto. Volvé a cargar.")
        antes = _foto(row)
        _sumar(conn, calc["id_historia"], calc["importe"], calc["kg"], usuario)
        despues = {
            "ustock": antes["ustock"] + calc["importe"],
            "stock": antes["stock"] + calc["kg"],
            "patrimonio": antes["patrimonio"] + calc["importe"],
            "usuti": antes["usuti"] + calc["importe"],
        }
        r = db.execute_returning(
            """
            INSERT INTO scintela.ajuste_cierre
                (anio, mes, id_historia, motivo, importe, kg, lineas,
                 antes, despues, creado_por)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s)
            RETURNING id_ajuste
            """,
            (fecha.year, fecha.month, calc["id_historia"], motivo,
             calc["importe"], calc["kg"], json.dumps(calc["lineas"]),
             json.dumps(antes), json.dumps(despues), (usuario or "web")[:50]),
            conn=conn,
        )
        id_ajuste = int(r["id_ajuste"])
        import mov_doble as _md
        _md.registrar(
            conn=conn, tipo="ajuste_cierre",
            origen_table="ajuste_cierre", origen_id=id_ajuste,
            destino_table="historia", destino_id=calc["id_historia"],
            importe=calc["importe"], fecha=hoy,
            concepto=(f"Ajuste al cierre de {nombre_mes(fecha.month)} "
                      f"· {motivo}")[:200],
            usuario=usuario,
            metadata={"id_ajuste": id_ajuste, "fecha_cierre": str(fecha),
                      "lineas": calc["lineas"], "antes": antes,
                      "despues": despues},
        )
    calc.update(id_ajuste=id_ajuste, antes=antes, despues=despues)
    return calc


def _sumar(conn, id_historia: int, importe: float, kg: float, usuario: str) -> None:
    db.execute(
        """
        UPDATE scintela.historia
           SET ustock = COALESCE(ustock, 0) + %(imp)s,
               stock = COALESCE(stock, 0) + %(kg)s,
               patrimonio = COALESCE(patrimonio, 0) + %(imp)s,
               usuti = COALESCE(usuti, 0) + %(imp)s,
               fecha_modifica = CURRENT_TIMESTAMP,
               usuario_modifica = %(usr)s
         WHERE id_historia = %(id)s
        """,
        {"imp": importe, "kg": kg, "usr": (usuario or "web")[:50],
         "id": int(id_historia)},
        conn=conn,
    )


def deshacer(id_ajuste: int, usuario: str = "web") -> dict:
    """Devuelve la foto de cierre a como estaba antes de ESTE ajuste."""
    with db.tx() as conn:
        db.execute("SELECT pg_advisory_xact_lock(hashtext('snapshot_historia'))",
                   conn=conn)
        a = db.fetch_one(
            "SELECT * FROM scintela.ajuste_cierre WHERE id_ajuste = %s FOR UPDATE",
            (int(id_ajuste),), conn=conn)
        if not a or a.get("anulado_en"):
            raise ValueError("Ese ajuste no existe o ya se deshizo.")
        hist = cierre_vigente()
        if not hist or int(hist["id_historia"]) != int(a["id_historia"] or 0):
            raise ValueError("La foto de cierre ya no es la que se ajustó: "
                             "sólo se deshace mientras sigue siendo el cierre vigente.")
        _sumar(conn, int(a["id_historia"]), -_f(a["importe"]), -_f(a["kg"]), usuario)
        db.execute(
            "UPDATE scintela.ajuste_cierre SET anulado_en = now(), anulado_por = %s "
            " WHERE id_ajuste = %s", ((usuario or "web")[:50], int(id_ajuste)),
            conn=conn)
        import mov_doble as _md
        _md.registrar(
            conn=conn, tipo="ajuste_cierre_deshecho",
            origen_table="ajuste_cierre", origen_id=int(id_ajuste),
            destino_table="historia", destino_id=int(a["id_historia"]),
            importe=-_f(a["importe"]), fecha=today_ec(),
            concepto=f"Se deshizo el ajuste al cierre #{id_ajuste}"[:200],
            usuario=usuario, metadata={"id_ajuste": int(id_ajuste)},
        )
    return {"id_ajuste": int(id_ajuste), "importe": -_f(a["importe"])}


def reaplicar(conn, id_historia: int, anio: int, mes: int) -> list[int]:
    """La foto de cierre de (anio, mes) se acaba de regrabar (DELETE +
    INSERT): los ajustes vivos de ese mes se le vuelven a sumar a la fila
    nueva, para que regrabar no los borre en silencio. Devuelve los ids.

    Va dentro de la transacción de la foto: sin la tabla (migración sin
    correr) no puede tirar un error que aborte la foto, por eso se pregunta
    primero si existe."""
    hay = db.fetch_one("SELECT to_regclass('scintela.ajuste_cierre') AS t",
                       conn=conn)
    if not (hay or {}).get("t"):
        return []
    filas = db.fetch_all(
        "SELECT id_ajuste, importe, kg FROM scintela.ajuste_cierre "
        " WHERE anio = %s AND mes = %s AND anulado_en IS NULL ORDER BY id_ajuste",
        (int(anio), int(mes)), conn=conn) or []
    for a in filas:
        _sumar(conn, id_historia, _f(a["importe"]), _f(a["kg"]), "reaplica-ajuste")
        db.execute("UPDATE scintela.ajuste_cierre SET id_historia = %s "
                   " WHERE id_ajuste = %s", (int(id_historia), int(a["id_ajuste"])),
                   conn=conn)
    return [int(a["id_ajuste"]) for a in filas]


def listar(anio: int | None = None, mes: int | None = None) -> list[dict]:
    where, params = [], []
    if anio:
        where.append("anio = %s")
        params.append(int(anio))
    if mes:
        where.append("mes = %s")
        params.append(int(mes))
    try:
        filas = db.fetch_all(
            f"""
            SELECT id_ajuste, anio, mes, id_historia, motivo, importe, kg, lineas,
                   antes, despues, creado_por, anulado_en, anulado_por,
                   TO_CHAR(creado_en AT TIME ZONE 'America/Guayaquil',
                           'DD/MM/YYYY HH24:MI') AS cuando
              FROM scintela.ajuste_cierre
             {("WHERE " + " AND ".join(where)) if where else ""}
             ORDER BY id_ajuste DESC
            """, tuple(params)) or []
    except Exception as e:  # noqa: BLE001 -- sin la migración, lista vacía
        _LOG.warning("ajuste_cierre.listar: %s", e)
        return []
    for f in filas:
        f["mes_nombre"] = nombre_mes(f["mes"])
        for k in ("lineas", "antes", "despues"):
            if isinstance(f.get(k), str):
                try:
                    f[k] = json.loads(f[k])
                except ValueError:
                    f[k] = None
    return filas


def vivos_del_mes(anio: int, mes: int) -> list[dict]:
    return [a for a in listar(anio, mes) if not a.get("anulado_en")]


def movimientos_de_ajustes(desde, hasta) -> list[dict]:
    """Los ajustes (y deshacer) hechos entre dos fotos de la traza, como
    movimientos de la foto: así el salto de PATANT que producen en el mes en
    curso queda con nombre en vez de "sin explicar".

    Aporte = −Δ PATANT: si el cierre anterior baja 191.045, la utilidad del
    mes en curso sube 191.045.
    """
    try:
        filas = db.fetch_all(
            """
            SELECT id_ajuste, anio, mes, motivo, importe,
                   (anulado_en IS NOT NULL AND anulado_en > %(d)s
                    AND anulado_en <= %(h)s) AS deshecho_ahora,
                   (creado_en > %(d)s AND creado_en <= %(h)s) AS creado_ahora
              FROM scintela.ajuste_cierre
             WHERE (creado_en > %(d)s AND creado_en <= %(h)s)
                OR (anulado_en > %(d)s AND anulado_en <= %(h)s)
            """, {"d": desde, "h": hasta}) or []
    except Exception:  # noqa: BLE001 -- sin la tabla no hay nada que nombrar
        return []
    out = []
    for a in filas:
        imp = _f(a["importe"])
        base = f"Ajuste al cierre de {nombre_mes(a['mes'])} · {a['motivo']}"
        if a.get("creado_ahora"):
            out.append({"componente": "patant", "tipo": "alta",
                        "doc_id": f"aj{a['id_ajuste']}", "etiqueta": base[:200],
                        "importe_antes": None, "importe_despues": imp,
                        "delta": imp, "aporte": round(-imp, 2),
                        "regla": "ajuste_cierre", "familia": "utilidad"})
        if a.get("deshecho_ahora"):
            out.append({"componente": "patant", "tipo": "baja",
                        "doc_id": f"aj{a['id_ajuste']}",
                        "etiqueta": ("Se deshizo: " + base)[:200],
                        "importe_antes": imp, "importe_despues": 0.0,
                        "delta": -imp, "aporte": round(imp, 2),
                        "regla": "ajuste_cierre", "familia": "utilidad"})
    return out
