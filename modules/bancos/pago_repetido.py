"""La misma factura de proveedor pagada dos veces.

Caso AQ 166 (23/09/2026). El 20/08, en medio de una tanda de ediciones en
/posdat, salió tres veces seguidas el "Registrar banco" del posdatado de la
factura 166 de AQ ($ 6.603,30) y quedó grabada una nota de débito en
Pichincha. El banco nunca la debitó: AQ se paga los viernes en el PAG-CASH
semanal, neto de retención. El 05/09 Andrés cargó el pago real de esa semana
("AQ 165/166/167/168/174/175/177/178", $ 26.438,63) y la 166 salió del banco
por segunda vez en los libros. Nadie lo vio durante un mes: Alex lo encontró
el 23/09 como un pendiente de conciliación sin contraparte.

Dos cosas lo hubieran frenado, y las dos viven acá:

1. **Antes de grabar un pago** (/bancos/emitir-cheque): si el concepto nombra
   un número de factura que ya salió del banco con "Registrar banco" para el
   mismo proveedor, se pregunta antes de grabar. `ya_debitadas()`.

2. **Todos los días** (/admin/health/debito-sin-banco): un "Registrar banco"
   que no aparece en el extracto aunque el extracto ya pasó su fecha, o cuya
   factura aparece también en otro pago del mismo proveedor.
   `debitos_sin_banco()`.

Las partes puras (sin base) están separadas para poder testearlas.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

import db

#: Cuánto para atrás se mira. AQ reinició la numeración en junio (de 6086 a
#: 1): una ventana acotada evita cruzar dos series con el mismo número.
DIAS_VENTANA = 120

#: Días de gracia entre la fecha del débito y el último extracto subido antes
#: de dar por hecho que el banco no lo tiene (el extracto llega con atraso y
#: el banco puede debitar uno o dos días después de lo cargado).
DIAS_GRACIA = 5

_PREFIJO_DEBITO = re.compile(r"^\s*D[ÉE]BITO\s+POSDAT\s+", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Partes puras
# ---------------------------------------------------------------------------


def numeros_de(concepto: str | None) -> set[str]:
    """Los números sueltos de un concepto: 'AQ 165/166 y 177' → {165,166,177}.

    Se normalizan sin ceros a la izquierda ('0166' y '166' son la misma
    factura para quien la tipea).
    """
    return {str(int(n)) for n in re.findall(r"\d+", concepto or "")}


def factura_del_debito(concepto_debito: str | None, prov: str | None) -> str | None:
    """El número de factura de un "Débito posdat <PROV> <factura> <día>".

    El concepto del posdatado es "<factura>  <día>" (así lo arma el puente de
    compras y así venía del dBase). Devuelve None si la primera palabra
    después del proveedor no es un número: sin número no hay con qué
    comparar, y comparar texto suelto ('QUIMSERTEC') da falsos positivos.
    """
    resto = _PREFIJO_DEBITO.sub("", concepto_debito or "").strip()
    p = (prov or "").strip().upper()
    if p and resto.upper().startswith(p + " "):
        resto = resto[len(p):].strip()
    primera = resto.split(" ", 1)[0] if resto else ""
    if not primera.isdigit():
        return None
    return str(int(primera))


def coincidencias(debitos: list[dict], concepto: str | None,
                  provs: set[str]) -> list[dict]:
    """Los débitos de `debitos` cuya factura nombra `concepto` (mismo proveedor).

    `debitos`: filas con prov, concepto (el del débito), fecha, importe,
    id_transaccion. Devuelve las que coinciden, con la `factura` agregada.
    """
    nums = numeros_de(concepto)
    provs_up = {(p or "").strip().upper() for p in provs if p}
    if not nums or not provs_up:
        return []
    out = []
    for d in debitos:
        prov = (d.get("prov") or "").strip().upper()
        if prov not in provs_up:
            continue
        fac = factura_del_debito(d.get("concepto"), prov)
        if fac and fac in nums:
            out.append({**d, "factura": fac})
    return out


def provs_del_pago(concepto: str | None, provs_posdats: set[str],
                   provs_conocidos: set[str]) -> set[str]:
    """De qué proveedor(es) es un pago: los de los posdatados elegidos, más el
    código con que arranca el concepto ('AQ 165/166' → AQ) si es un proveedor
    que tiene débitos para comparar."""
    out = {p.strip().upper() for p in provs_posdats if p}
    primera = ((concepto or "").strip().split(" ", 1) or [""])[0].upper()
    if primera in provs_conocidos:
        out.add(primera)
    return out


def evaluar_sin_banco(filas: list[dict], ultimo_extracto: dict,
                      repetidas: list[dict]) -> tuple[list[dict], dict]:
    """Arma las alertas del health. Sin base.

    `filas`: débitos de "Registrar banco" vivos y sin conciliar, con no_banco y
    fecha. Sólo alerta los que el extracto ya dejó atrás por más de
    DIAS_GRACIA días: antes de eso es normal que falten.
    `ultimo_extracto`: {no_banco: date} — hasta dónde llega el extracto subido.
    `repetidas`: débitos cuya factura aparece también en otro pago.
    """
    alerts: list[dict] = []
    viejos = []
    for f in filas:
        tope = ultimo_extracto.get(f.get("no_banco"))
        fecha = f.get("fecha")
        if tope and fecha and fecha <= tope - timedelta(days=DIAS_GRACIA):
            viejos.append(f)
    if viejos:
        total = round(sum(float(f.get("importe") or 0) for f in viejos), 2)
        alerts.append({
            "severity": "high",
            "category": "debito_posdat_sin_banco",
            "msg": (
                f"{len(viejos)} débito(s) cargados con 'Registrar banco' que el "
                f"banco no tiene (${total:,.2f}): "
                + "; ".join(f"{f.get('concepto')} {f.get('fecha')}" for f in viejos)
                + ". El extracto ya pasó esa fecha y no aparecen. Si el pago "
                "salió por otro lado (PAG-CASH), el débito está de más: se "
                "reversa desde /historial."
            ),
            "filas": viejos,
        })
    if repetidas:
        alerts.append({
            "severity": "high",
            "category": "factura_pagada_dos_veces",
            "msg": (
                f"{len(repetidas)} factura(s) de proveedor que salieron dos veces "
                f"del banco: "
                + "; ".join(
                    f"{r.get('prov')} {r.get('factura')} (Registrar banco "
                    f"{r.get('fecha')} y '{r.get('otro_concepto')}' "
                    f"{r.get('otro_fecha')})" for r in repetidas)
                + "."
            ),
            "filas": repetidas,
        })
    return alerts, {
        "n_sin_conciliar": len(filas),
        "n_sin_banco": len(viejos),
        "n_repetidas": len(repetidas),
    }


# ---------------------------------------------------------------------------
# Lecturas
# ---------------------------------------------------------------------------

_SQL_DEBITOS = """
SELECT t.id_transaccion, t.no_banco, t.fecha, t.importe,
       TRIM(COALESCE(t.concepto, ''))                AS concepto,
       UPPER(TRIM(COALESCE(md.metadata->>'prov', t.prov, ''))) AS prov,
       md.id_mov_doble,
       EXISTS (SELECT 1 FROM scintela.banco_conciliacion_match m
                WHERE m.id_transaccion = t.id_transaccion
                  AND m.deshecho_en IS NULL)          AS conciliado
  FROM scintela.mov_doble md
  JOIN scintela.transacciones_bancarias t ON t.id_transaccion = md.origen_id
 WHERE md.origen_table = 'transacciones_bancarias'
   AND md.metadata ? 'id_posdat_debito'
   AND md.estado = 'activo'
   AND t.fecha >= %s
"""


def debitos_registrar_banco(desde: date) -> list[dict]:
    """Los débitos vivos hechos con "Registrar banco" desde `desde`."""
    return [dict(r) for r in (db.fetch_all(_SQL_DEBITOS, (desde,)) or [])]


def provs_de_posdats(ids: list[int] | None) -> set[str]:
    if not ids:
        return set()
    rows = db.fetch_all(
        "SELECT DISTINCT UPPER(TRIM(COALESCE(prov, ''))) AS prov "
        "  FROM scintela.posdat WHERE id_posdat = ANY(%s)",
        (list(ids),),
    ) or []
    return {r["prov"] for r in rows if r.get("prov")}


def ya_debitadas(*, concepto: str, id_posdats: list[int] | None,
                 hoy: date) -> list[dict]:
    """Facturas que nombra el concepto de un pago y que ya salieron del banco
    con "Registrar banco" (mismo proveedor, últimos DIAS_VENTANA días)."""
    debitos = debitos_registrar_banco(hoy - timedelta(days=DIAS_VENTANA))
    if not debitos:
        return []
    conocidos = {d["prov"] for d in debitos if d.get("prov")}
    provs = provs_del_pago(concepto, provs_de_posdats(id_posdats), conocidos)
    return coincidencias(debitos, concepto, provs)


def _ultimo_extracto() -> dict:
    """{no_banco: fecha} — la última fecha del extracto subido, por banco."""
    rows = db.fetch_all(
        """
        SELECT no_banco, MAX(f) AS hasta FROM (
            SELECT no_banco, real_fecha AS f
              FROM scintela.banco_conciliacion_match
             WHERE real_fecha IS NOT NULL AND deshecho_en IS NULL
            UNION ALL
            SELECT no_banco, fecha FROM scintela.banco_historicos_pendientes
             WHERE fecha IS NOT NULL
        ) x GROUP BY no_banco
        """,
    ) or []
    return {r["no_banco"]: r["hasta"] for r in rows if r.get("hasta")}


def _repetidas(debitos: list[dict]) -> list[dict]:
    """Débitos cuya factura aparece en OTRO pago posterior del mismo proveedor."""
    out = []
    for d in debitos:
        fac = factura_del_debito(d.get("concepto"), d.get("prov"))
        if not fac or not d.get("prov"):
            continue
        otros = db.fetch_all(
            """
            SELECT id_transaccion, fecha, TRIM(COALESCE(concepto, '')) AS concepto
              FROM scintela.transacciones_bancarias
             WHERE no_banco = %s AND id_transaccion <> %s
               AND UPPER(TRIM(COALESCE(documento, ''))) IN ('CH', 'ND')
               AND fecha >= %s
               AND concepto !~* '^\\s*d[ée]bito\\s+posdat'
               AND concepto !~* '^\\s*reverso'
               AND (UPPER(TRIM(COALESCE(prov, ''))) = %s
                    OR UPPER(TRIM(concepto)) LIKE %s)
               AND concepto ~ %s
            """,
            (d["no_banco"], d["id_transaccion"], d["fecha"], d["prov"],
             d["prov"] + " %", r"(^|[^0-9])0*" + fac + r"([^0-9]|$)"),
        ) or []
        for o in otros:
            out.append({
                "prov": d["prov"], "factura": fac, "fecha": d["fecha"],
                "importe": d.get("importe"),
                "id_transaccion": d["id_transaccion"],
                "otro_id": o["id_transaccion"], "otro_fecha": o["fecha"],
                "otro_concepto": o["concepto"],
            })
    return out


def debitos_sin_banco(hoy: date) -> tuple[list[dict], dict]:
    """Para el health: (alerts, stats)."""
    debitos = debitos_registrar_banco(hoy - timedelta(days=DIAS_VENTANA))
    sin_conc = [d for d in debitos if not d.get("conciliado")]
    return evaluar_sin_banco(sin_conc, _ultimo_extracto(), _repetidas(debitos))
