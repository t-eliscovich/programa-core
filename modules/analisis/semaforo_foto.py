"""El bloque "Cobranza" del mail diario: el semáforo contra la foto anterior.

TMT 2026-10-13: *"en el mail diario, quisiera agregar un análisis chiquito de
cobranza, quiénes estaban en rojo y se fueron, quiénes pagaron una suma
importante"*. Quedó en cuatro renglones: salieron del rojo, entraron al rojo,
pagos grandes y el total en rojo contra la foto anterior. (El "cuánto de lo
cobrado era vencido" se propuso y Tamara no lo quiso.)

El semáforo es el MISMO de /analisis/cobranza (`cobranza.colores`) y la foto
es la MISMA que usa la campanita del vendedor (`scintela.semaforo_foto`,
grabada por `mi_cartera.avisos_vend.foto_semaforo`, migs 0261/0262): si el
mail y el vendedor leyeran fotos distintas, uno diría "pasó a rojo" y el
otro no. "Rojo" incluye a los incobrables (`inc`), igual que la tarjeta roja
de la pantalla, y cuenta sólo a los que la pantalla lista (saldo de 1.000 o
más, o un cheque devuelto): un rojo de 300 que pasa a verde es ruido.

La foto la toma la rutina diaria y la PISA el mail del cierre, así que la foto
de un día queda con el estado del cierre. El mail compara el semáforo de
AHORA contra la última foto ANTERIOR al tramo.
"""
from __future__ import annotations

import db
from modules.analisis import cobranza

#: Un pago es "grande" desde este monto (por cliente, en el tramo).
UMBRAL_PAGO_GRANDE = 5000
#: Cuántos nombres por renglón; el resto va como "+N más".
MAX_NOMBRES = 5  # Tamara 13/10: "no puedo ver más" → de 3 a 5
#: Un cheque es "al día" si se puede depositar a menos de estos días.
DIAS_AL_DIA = 3
ROJOS = ("rojo", "inc")


def _f(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def es_rojo(c: dict | None) -> bool:
    return (c or {}).get("color") in ROJOS


def listado(c: dict) -> bool:
    """Lo que /analisis/cobranza lista: saldo de 1.000 o más, o un devuelto.
    Una foto vieja sin saldo (antes de la 0262) cuenta como listada."""
    if c.get("saldo") is None:
        return True
    return _f(c.get("saldo")) >= cobranza.SALDO_MINIMO or _f(c.get("reb")) > 0


def _primer_motivo(m) -> str:
    if isinstance(m, list):
        return m[0] if m else ""
    return (m or "").split(" · ")[0]


def foto_anterior(antes_de) -> tuple[object, dict[str, dict]]:
    """La última foto guardada ANTES de `antes_de`: (fecha, {cod: fila})."""
    r = db.fetch_one("SELECT MAX(fecha) AS f FROM scintela.semaforo_foto "
                     " WHERE fecha < %s", (antes_de,))
    f = (r or {}).get("f")
    if not f:
        return None, {}
    filas = db.fetch_all(
        "SELECT codigo_cli, color, motivos, nombre, saldo, reb "
        "  FROM scintela.semaforo_foto WHERE fecha = %s", (f,)) or []
    return f, {(x["codigo_cli"] or "").strip().upper(): dict(x) for x in filas}


def _total(filas) -> dict | None:
    filas = list(filas)
    if any(c.get("saldo") is None for c in filas):
        return None   # foto sin saldo: no se puede contar
    rojos = [c for c in filas if es_rojo(c) and listado(c)]
    return {"n": len(rojos), "saldo": round(sum(_f(c.get("saldo")) for c in rojos))}


def comparar(antes: dict[str, dict], ahora: dict[str, dict],
             pagado: dict[str, float] | None = None) -> dict:
    """Quién salió y quién entró al rojo, y el total contra la foto. Pura.

    `antes` y `ahora`: {cod: {color, saldo, reb, nombre, motivos}}."""
    pagado = pagado or {}
    salieron = []
    for cod, a in antes.items():
        if not (es_rojo(a) and listado(a)):
            continue
        h = ahora.get(cod)
        if es_rojo(h):
            continue
        salieron.append({"cod": cod,
                         "nombre": (h or {}).get("nombre") or a.get("nombre") or "",
                         "color": h["color"] if h is not None else None,
                         "antes": _f(a.get("saldo")),
                         "pagado": _f(pagado.get(cod))})
    entraron = []
    for cod, h in ahora.items():
        if not (es_rojo(h) and listado(h)) or es_rojo(antes.get(cod)):
            continue
        entraron.append({"cod": cod, "nombre": h.get("nombre") or "",
                         "saldo": _f(h.get("saldo")),
                         "motivo": _primer_motivo(h.get("motivos"))})
    salieron.sort(key=lambda x: (-x["pagado"], -x["antes"]))
    entraron.sort(key=lambda x: -x["saldo"])
    return {"salieron": salieron, "entraron": entraron,
            "total": _total(ahora.values()),
            "total_antes": _total(antes.values()) if antes else None}


def _forma(r: dict) -> str:
    nb = int(r.get("no_banco") or 0)
    if nb in (90, 91):
        return "depósito"
    if nb == 99:
        return "efectivo"
    if nb == 97:
        return "anticipo"
    dias = r.get("dias")
    if dias is None or dias < DIAS_AL_DIA:
        return "cheque al día"
    return f"cheque a {int(dias)} días"


def cobros_del_tramo(desde, hasta) -> list[dict]:
    """Lo ingresado por Cobranza en el tramo, cheque por cheque, con el MISMO
    criterio que "Cobranzas del día" del mail (`dia.cobranza_entre`)."""
    from modules.cheques.queries import SQL_DIA_INGRESO

    return db.fetch_all(
        f"""
        SELECT UPPER(TRIM(c.codigo_cli)) AS cod,
               COALESCE(cl.nombre, '') AS nombre,
               c.importe, c.no_banco,
               c.fechad - ({SQL_DIA_INGRESO}) AS dias
          FROM scintela.cheque c
          LEFT JOIN scintela.cliente cl ON cl.codigo_cli = c.codigo_cli
         WHERE {SQL_DIA_INGRESO} BETWEEN %s AND %s
           AND COALESCE(c.stat, '') NOT IN ('X', 'Y')
           AND COALESCE(c.no_banco, 0) <> 98
           AND c.codigo_cli IS NOT NULL
        """, (desde, hasta)) or []


def por_cliente(cobros: list[dict]) -> dict[str, dict]:
    """Agrupa los cobros por cliente: total, cuántos y las formas. Pura."""
    out: dict[str, dict] = {}
    for r in cobros:
        g = out.setdefault(r["cod"], {"cod": r["cod"], "nombre": r.get("nombre") or "",
                                      "us": 0.0, "n": 0, "formas": []})
        g["us"] += _f(r.get("importe"))
        g["n"] += 1
        f = _forma(r)
        if f not in g["formas"]:
            g["formas"].append(f)
    return out


def pagos_grandes(pc: dict[str, dict], umbral: float = UMBRAL_PAGO_GRANDE) -> list[dict]:
    return sorted((g for g in pc.values() if g["us"] >= umbral), key=lambda g: -g["us"])


def para_la_nota(desde, hasta) -> dict:
    """Todo lo que el mail necesita.

    Compara contra la última foto ANTERIOR a `desde` (para el día, la de ayer;
    para el fin de semana, la del viernes). Sin foto anterior van sólo los
    pagos grandes y el total."""
    ahora = {k.strip().upper(): v for k, v in cobranza.colores().items()}
    pc = por_cliente(cobros_del_tramo(desde, hasta))
    f_antes, antes = foto_anterior(desde)
    cmp = comparar(antes, ahora, {k: v["us"] for k, v in pc.items()})
    if f_antes is None:
        # Sin foto anterior todos los rojos parecerían "nuevos" (13/10, el
        # primer día salieron los 22 como "entraron al rojo").
        cmp["salieron"], cmp["entraron"] = [], []
    cmp["fecha_antes"] = f_antes
    cmp["pagos"] = pagos_grandes(pc)
    cmp["umbral"] = UMBRAL_PAGO_GRANDE
    return cmp
