"""Recode de facturas a su SUCURSAL — el plan, en un solo lugar.

POR QUÉ (TMT 2026-07-30). Asinfo factura a la MATRIZ y entrega en la dirección
de la empresa-sucursal (mismo RUC + un dígito): AJO factura, AJ2 recibe. Desde
`asinfo.service._aplicar_sucursales` eso ya se clasifica solo **para lo que se
carga de ahora en más**. Lo que YA está en PC entró con el código de la matriz.

Dueña: *"si el dbase las tiene en un cliente debe quedar asi. A futuro debemos
respetar las reglas que hace dbase para clasificar estas facturas"*. Y el dBase
las tiene bien: de las 25 facturas AJO→dirección de AJ2 que se cruzaron una por
una contra `FACTURAS.DBF` del 29/07, **las 25 están como AJ2**. Este recode no
inventa un criterio: alinea PC con lo que la persona ya decidió.

Este módulo es la ÚNICA fuente del plan — lo usa la pantalla que lo muestra y la
que lo aplica. Dos implementaciones de una operación que mueve plata entre
cuentas de clientes es la clase de cosa que se desincroniza y termina en un
descuadre.

Reglas del plan, todas conservadoras:

  1. Sólo sucursales EN USO (`asinfo.service.sucursales_en_uso`): si el dBase no
     usa ese código —CL2, CL3, JEC, CJE tienen CERO facturas— no se parte nada.
  2. Se aparea por **N° SRI completo** primero; `numf` sólo como respaldo,
     porque la serie de notas de crédito repite números de facturas viejas.
  3. Se valida **kilos + importe** (tolerancia 0,01) contra lo que dice Asinfo.
     Si no coinciden es otro documento con el mismo número: se descarta.
  4. Si PC tiene el MISMO documento bajo la matriz **y** bajo la sucursal, NO se
     recodea: es una fila de más (duplicado), y crear dos idénticas sería peor.
"""
from __future__ import annotations

import re as _re

import db

_DIAS_DEFAULT = 120
# Marca que deja el recode para poder deshacerlo: 'recode-suc:<código previo>'.
MARCA = "recode-suc"


def _sql_facturas(dias: int) -> str:
    return f"""
        SELECT f.id_factura, f.codigo_cli, f.numf, f.numf_completo, f.fecha,
               f.kg, f.importe, f.saldo, f.stat
          FROM scintela.factura f
         WHERE f.fecha >= CURRENT_DATE - INTERVAL '{int(dias)} days'
           AND TRIM(COALESCE(f.stat, '')) NOT IN ('X', 'Y')
           AND COALESCE(f.numf, 0) > 0
    """


def plan(dias: int = _DIAS_DEFAULT) -> dict:
    """Qué facturas hay que mover a su sucursal. NO escribe nada."""
    out: dict = {"mover": [], "dobles": [], "resumen": {}, "error": ""}
    filas = db.fetch_all(_sql_facturas(dias)) or []
    if not filas:
        out["error"] = "no hay facturas vivas en la ventana"
        return out
    pc_por_completo: dict = {}
    pc_por_numf: dict = {}
    for f in filas:
        pc_por_numf.setdefault(int(f["numf"]), []).append(f)
        nc = (f.get("numf_completo") or "").strip()
        if nc:
            pc_por_completo.setdefault(nc, []).append(f)
    fechas = [f["fecha"] for f in filas if f.get("fecha")]
    desde, hasta = min(fechas), max(fechas)

    try:
        from modules.asinfo import service as _asinfo
        docs = _asinfo.facturas_periodo(desde, hasta) or []
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"Asinfo no contestó: {exc!r}"
        return out

    de_sucursal = [d for d in docs if d.get("cliente_codigo_facturado")]
    r = {"pc": len(filas), "asinfo": len(docs), "de_sucursal": len(de_sucursal),
         "ya_ok": 0, "sin_cargar": 0, "kg_distinto": 0,
         "por_completo": 0, "solo_numf": 0, "otro_codigo": 0,
         "desde": desde, "hasta": hasta}

    for d in de_sucursal:
        num = str(d.get("numero") or "").strip()
        m = _re.findall(r"\d+", num)
        if not m:
            continue
        numf = int(m[-1])
        rows = pc_por_completo.get(num)
        por_completo = bool(rows)
        if not rows:
            rows = pc_por_numf.get(numf)
        if not rows:
            r["sin_cargar"] += 1
            continue
        kg_a = abs(float(d.get("kg") or 0))
        usd_a = abs(float(d.get("usd") or 0))
        coinciden = [
            x for x in rows
            if abs(abs(float(x.get("kg") or 0)) - kg_a) <= 0.01
            and abs(abs(float(x.get("importe") or 0)) - usd_a) <= 0.01
        ]
        if not coinciden:
            r["kg_distinto"] += 1
            continue
        rows = coinciden
        r["por_completo" if por_completo else "solo_numf"] += 1
        suc = str(d.get("cliente_codigo") or "").strip().upper()
        matriz = str(d.get("cliente_codigo_facturado") or "").strip().upper()
        codigos = {(x.get("codigo_cli") or "").strip().upper() for x in rows}
        for x in rows:
            pc_cod = (x.get("codigo_cli") or "").strip().upper()
            if pc_cod == suc:
                r["ya_ok"] += 1
                continue
            if pc_cod != matriz:
                r["otro_codigo"] += 1
                continue
            item = {
                "id_factura": x["id_factura"],
                "numero": num,
                "numf": numf,
                "fecha": x.get("fecha"),
                "de": matriz,
                "a": suc,
                "importe": float(x.get("importe") or 0),
                "saldo": float(x.get("saldo") or 0),
                "abierta": (x.get("stat") or "").strip().upper() in ("Z", "A", ""),
            }
            if suc in codigos:
                out["dobles"].append(item)
            else:
                out["mover"].append(item)
    out["mover"].sort(key=lambda i: (i["de"], i["a"], i["numero"]))
    out["resumen"] = r
    return out


def resumen_por_par(mover: list[dict]) -> list[dict]:
    """Agrupa el plan por par matriz→sucursal, para mostrarlo."""
    acc: dict = {}
    for i in mover:
        a = acc.setdefault((i["de"], i["a"]),
                           {"de": i["de"], "a": i["a"], "n": 0,
                            "importe": 0.0, "saldo": 0.0})
        a["n"] += 1
        a["importe"] += i["importe"]
        if i["abierta"]:
            a["saldo"] += i["saldo"]
    return sorted(acc.values(), key=lambda a: -a["n"])


def aplicar(mover: list[dict]) -> int:
    """Mueve las facturas del plan a su sucursal. Devuelve cuántas movió.

    Deja `usuario_modifica = 'recode-suc:<código previo>'`, que es lo que hace
    reversible el paso (ver `deshacer`) — el mismo mecanismo con el que la
    migración 0132 dejó rastro de su propio recode.

    IDEMPOTENTE: el UPDATE exige que la fila siga bajo el código ANTERIOR, así
    que aplicarlo dos veces no hace nada la segunda vez.
    """
    n = 0
    for i in mover:
        n += int(db.execute(
            """
            UPDATE scintela.factura
               SET codigo_cli = %s,
                   usuario_modifica = %s
             WHERE id_factura = %s
               AND UPPER(TRIM(codigo_cli)) = %s
            """,
            (i["a"], f"{MARCA}:{i['de']}"[:50], i["id_factura"], i["de"]),
        ) or 0)
    return n


def pendientes_de_deshacer() -> list[dict]:
    """Las facturas que este recode movió y todavía no se deshicieron."""
    return db.fetch_all(
        """
        SELECT id_factura, numf, numf_completo, codigo_cli, usuario_modifica,
               importe, saldo
          FROM scintela.factura
         WHERE usuario_modifica LIKE %s
         ORDER BY numf
        """,
        (f"{MARCA}:%",),
    ) or []


def deshacer() -> int:
    """Devuelve a la matriz todo lo que este recode movió.

    El código previo sale de la propia marca `recode-suc:<código>`, así que no
    hace falta guardar un snapshot aparte.
    """
    n = 0
    for f in pendientes_de_deshacer():
        previo = str(f.get("usuario_modifica") or "").split(":", 1)[-1].strip().upper()
        if not previo:
            continue
        n += int(db.execute(
            """
            UPDATE scintela.factura
               SET codigo_cli = %s,
                   usuario_modifica = %s
             WHERE id_factura = %s
            """,
            (previo, f"deshizo-{MARCA}"[:50], f["id_factura"]),
        ) or 0)
    return n
