"""La explicación del día: POR QUÉ subió (o bajó) la utilidad.

TMT 2026-08-04 (dueña): *"quiero agregar un check diario, quiero que cada día
puedas darme una explicación de por qué subió la utilidad. comparás balance de
mañana y a fin de día"*. Y sobre el residuo, sin lugar a interpretación:
*"no debería haber residuo.. y tenemos que ir entrenando esto"*.

## De qué se hace cargo esto que la grabadora no

`traza_utilidad` (mig 0150) ya contesta **qué componente** se movió: stock,
cartera, pasivos. Eso alcanza para descartar hipótesis, no para explicar.
Explicar es decir **qué documento** lo movió: la factura 12345, el cheque de
ELF, la compra de hilado.

Llegar a ese nivel tiene un obstáculo concreto: `scripts/import_dbf.py` borra
POSDAT, COMPRA, DOLARES, ACTIVOS y RETIROS **enteras** en cada sync, así que
`fecha_crea` queda pisada con la hora del sync y la pregunta "¿qué se cargó
hoy?" no tiene respuesta en esas tablas. La única forma que sobrevive a eso es
no preguntarle a la fila cuándo nació, sino **guardar una foto propia y
diffearla**. Lo que está en la foto nueva y no en la vieja, entró.

## El invariante

    Δ utilidad = Σ (aporte de cada movimiento)

sin residuo, siempre. Se cumple **por construcción**: si el detalle por
documento no llega a explicar el componente entero, la diferencia se guarda
como una fila sintética `#ajuste:<componente>`, con nombre y a la vista.

Eso NO es hacer trampa con la cuadratura — es lo contrario. Un `#ajuste` que
aparece es una tarea del entrenamiento: alguien tiene que mirar por qué ese
pedazo no se explica y escribir la regla. El día que no aparezca ninguno, el
sistema explica el 100% de lo que se movió. **Ese es el número que mide el
progreso, no la cuadratura.**

## Familias: lo que mueve la utilidad y lo que sólo la cambia de lugar

Una cobranza no genera utilidad: la factura baja $1.000 y el cheque sube
$1.000. Netea cero **solo**, sin que haya que emparejar documentos, porque
cada movimiento aporta con el signo de su componente. Por eso los movimientos
se agrupan en familias que *deberían* netear cero:

    cobranza  = factura que baja  +  cheque que entra
    depósito  = cheque que sale   +  banco que sube
    pago      = banco que baja    +  pasivo que baja

Cuando una familia NO netea cero, esa diferencia es la explicación de verdad:
una retención, un descuento, una nota de crédito, un cheque devuelto. Ahí es
donde hay que mirar.

## Gotchas que ya están adentro

· **Hora de ECUADOR** (UTC−5) para todo. El server corre en UTC: después de
  las 19:00 EC ya está en el día siguiente.
· **Los pasivos aportan al revés.** Si `totp` sube, la utilidad baja.
· **La maquinaria baja sola todos los días.** `activos_totales()` prorratea la
  amortización por día del mes (MENU.PRG). No es un error y tiene su propia
  regla para que no asuste.
· **La tarifa del stock revalúa TODO de un saque.** Por eso `dia_detalle`
  guarda `cantidad` y `precio` aparte: un Δ de stock se parte en "entraron
  kilos" (Δkg × precio viejo) y "cambió el $/kg" (kg nuevos × Δprecio).
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

import db

_LOG = logging.getLogger("programa_core.dia")

#: Horas de Ecuador en las que se clavan las dos capturas ancla.
HORA_MANANA = 7
HORA_CIERRE = 19

#: Debajo de esto un Δ es ruido de centavos y no se guarda como movimiento.
UMBRAL = 0.01

#: Los 11 componentes del patrimonio y su signo hacia la utilidad. Mismo orden
#: y mismos signos que `traza.COMPONENTES` — si uno cambia, cambian los dos.
COMPONENTES: tuple[tuple[str, int], ...] = (
    ("caja", 1), ("bancos", 1), ("cheques", 1), ("facturas", 1),
    ("antic", 1), ("vsto", 1), ("vqx", 1), ("umaq", 1), ("uact", 1),
    ("totp", -1), ("uret", 1),
)

SIGNO = dict(COMPONENTES)

ETIQUETAS = {
    "caja": "Caja", "bancos": "Bancos", "cheques": "Cheques",
    "facturas": "Facturas", "antic": "Anticipos", "vsto": "Stock MP+Prod.",
    "vqx": "Stock Químicos", "umaq": "Maquinaria", "uact": "Terrenos/Edif.",
    "totp": "Pasivos", "uret": "Dividendos",
}

#: De qué clave del balance sale cada componente.
_CLAVE_BALANCE = {
    "caja": "salcaj", "bancos": "salbanc_total", "cheques": "totc",
    "facturas": "totf", "antic": "antic", "vsto": "vsto", "vqx": "vqx",
    "umaq": "umaq", "uact": "uact", "totp": "totp", "uret": "uret",
}


# ── Tiempo ──────────────────────────────────────────────────────────────────

def ahora_ec() -> datetime:
    """Ahora en Ecuador (UTC−5, sin horario de verano)."""
    return datetime.now(UTC) - timedelta(hours=5)


def hoy_ec():
    return ahora_ec().date()


def _hora(env: str, default: int) -> int:
    try:
        h = int(os.environ.get(env, str(default)))
    except (TypeError, ValueError):
        return default
    return h if 0 <= h <= 23 else default


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# ── El detalle: qué documentos componen cada componente ─────────────────────

def _rows(sql: str, params=None) -> list[dict]:
    """SELECT fail-soft: una fuente caída no puede tumbar la captura."""
    try:
        return db.fetch_all(sql, params) or []
    except Exception as e:  # noqa: BLE001
        _LOG.warning("dia: no pude leer el detalle (%s)", e)
        return []


def _det_facturas() -> list[dict]:
    """Cartera de facturas — mismo WHERE que `queries.totf()`."""
    return [
        {"doc_id": f"f{r['id_factura']}",
         "etiqueta": f"Factura {r.get('numf_completo') or r.get('numf')} · {(r.get('codigo_cli') or '').strip()}",
         "importe": _f(r.get("saldo"))}
        for r in _rows(
            """
            SELECT id_factura, numf, numf_completo, codigo_cli, saldo
              FROM scintela.factura
             WHERE (stat IS NULL OR stat IN ('Z','A','',' '))
               AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
               AND COALESCE(saldo, 0) <> 0
            """
        )
    ]


def _det_cheques() -> list[dict]:
    """Cheques en cartera — mismo WHERE que `queries.totc()`."""
    return [
        {"doc_id": f"c{r['id_cheque']}",
         "etiqueta": f"Cheque {(r.get('no_cheque') or '').strip()} · {(r.get('codigo_cli') or '').strip()}",
         "importe": _f(r.get("importe"))}
        for r in _rows(
            """
            SELECT id_cheque, no_cheque, codigo_cli, importe
              FROM scintela.cheque
             WHERE stat IN ('Z','1','2','3','P','D')
               AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
               AND COALESCE(importe, 0) <> 0
            """
        )
    ]


def _det_caja() -> list[dict]:
    """Movimientos de caja con signo (E=+, S=−) — como `queries.salcaj()`.

    El opening (el saldo con el que arranca la primera fila) no es un
    documento: entra como sintética para que la suma cierre.
    """
    out = [
        {"doc_id": f"k{r['id_caja']}",
         "etiqueta": f"Caja {(r.get('tipo') or '').strip()} · {(r.get('concepto') or '').strip()[:60]}",
         "importe": _f(r.get("firmado"))}
        for r in _rows(
            """
            SELECT id_caja, tipo, concepto,
                   CASE WHEN tipo = 'E' THEN importe
                        WHEN tipo = 'S' THEN -importe
                        ELSE importe END AS firmado
              FROM scintela.caja
             WHERE COALESCE(importe, 0) <> 0
            """
        )
    ]
    ap = _rows(
        """
        SELECT saldo - CASE WHEN tipo = 'E' THEN importe
                            WHEN tipo = 'S' THEN -importe
                            ELSE importe END AS apertura
          FROM scintela.caja
         WHERE saldo IS NOT NULL
         ORDER BY fecha ASC, id_caja ASC
         LIMIT 1
        """
    )
    if ap and _f(ap[0].get("apertura")):
        out.append({"doc_id": "#apertura", "etiqueta": "Saldo de apertura de caja",
                    "importe": _f(ap[0].get("apertura"))})
    return out


def _det_bancos() -> list[dict]:
    """Un renglón por banco. El saldo bancario NO es sumable por documento: el
    dBase mantiene un running `saldo` por fila y `saldo_bancos()` resuelve cuál
    vale. Así que el "documento" acá es la cuenta, no la transacción."""
    try:
        from modules.informes import queries as _q
        bancos = _q.saldo_bancos() or []
    except Exception as e:  # noqa: BLE001
        _LOG.warning("dia: no pude leer los bancos (%s)", e)
        return []
    out = [
        {"doc_id": f"b{b.get('no_banco')}",
         "etiqueta": f"Banco {(b.get('nombre') or '').strip() or b.get('no_banco')}",
         "importe": _f(b.get("saldo"))}
        for b in bancos
    ]
    try:
        from modules.informes import queries as _q
        pos = _q.posdat_totales() or {}
        for k, rot in (("pos1", "Cheques emitidos sin debitar (P1)"),
                       ("pos2", "Cheques emitidos sin debitar (P2)")):
            if _f(pos.get(k)):
                out.append({"doc_id": f"#{k}", "etiqueta": rot, "importe": _f(pos.get(k))})
    except Exception as e:  # noqa: BLE001
        _LOG.warning("dia: no pude leer P1/P2 (%s)", e)
    return out


def _det_antic() -> list[dict]:
    """Anticipos vivos — `queries.anticipos()`. El descuento por mercadería ya
    recibida (cruce con Asinfo) entra como sintética: no es una fila de
    `dolares`, es un ajuste calculado contra otra base de datos."""
    out = [
        {"doc_id": f"d{r['id_dolares']}",
         "etiqueta": f"Anticipo {(r.get('cta') or '').strip()} · {(r.get('concepto') or '').strip()[:60]}",
         "importe": _f(r.get("importe"))}
        for r in _rows(
            """
            SELECT id_dolares, cta, concepto, importe
              FROM scintela.dolares
             WHERE (st IS NULL OR st IN ('', ' '))
               AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
               AND COALESCE(importe, 0) <> 0
            """
        )
    ]
    try:
        from modules.informes import queries as _q
        recibido = _f(_q.anticipos_con_mercaderia_recibida())
        if recibido:
            out.append({"doc_id": "#recibidos",
                        "etiqueta": "Menos anticipos cuya mercadería ya está en stock",
                        "importe": -recibido})
    except Exception as e:  # noqa: BLE001
        _LOG.warning("dia: no pude leer anticipos recibidos (%s)", e)
    return out


def _det_totp() -> list[dict]:
    """Deuda viva — `POSDAT_DEUDA_VIVA_WHERE` (banc=0, no anulada)."""
    return [
        {"doc_id": f"p{r['id_posdat']}",
         "etiqueta": f"Deuda {(r.get('prov') or '').strip()} {r.get('num') or ''} · {(r.get('concepto') or '').strip()[:50]}",
         "importe": _f(r.get("importe"))}
        for r in _rows(
            """
            SELECT id_posdat, prov, num, concepto, importe
              FROM scintela.posdat
             WHERE COALESCE(banc, 0) = 0
               AND (anulada IS NOT TRUE OR anulada IS NULL)
               AND COALESCE(importe, 0) <> 0
            """
        )
    ]


def _det_activos(cual: str) -> list[dict]:
    """UMAQ (tipo M/C/K) o UACT (tipo I/T), con el valor en libros prorrateado
    al día — la misma fórmula que `activos_totales()`."""
    tipos = ("M", "C", "K") if cual == "umaq" else ("I", "T")
    try:
        from modules.activos.queries import borrado_where_sql as _borr
        borr = _borr()
    except Exception:  # noqa: BLE001
        borr = ""
    return [
        {"doc_id": f"a{r['id_activos']}",
         "etiqueta": f"{(r.get('concepto') or '').strip()[:60]}",
         "importe": _f(r.get("valor_calc"))}
        for r in _rows(
            f"""
            WITH coef AS (
              SELECT LEAST(EXTRACT(DAY FROM (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date)::numeric, 30) / 30.0 AS c
            )
            SELECT id_activos, concepto,
                   GREATEST(COALESCE(inicial, 0)
                            - COALESCE(amortizac, 0)
                            - (SELECT c FROM coef) * COALESCE(cuota, 0), 0) AS valor_calc
              FROM scintela.activos
             WHERE UPPER(TRIM(COALESCE(tipo, ''))) IN %s
               {borr}
            """,
            (tipos,),
        )
    ]


def _det_uret() -> list[dict]:
    """Retiros del mes en curso (hora Ecuador) — `uret_mes_corriente()`."""
    return [
        {"doc_id": f"r{r['id_retiro']}",
         "etiqueta": f"Retiro {(r.get('de') or '').strip()} · {(r.get('concepto') or '').strip()[:60]}",
         "importe": _f(r.get("ret"))}
        for r in _rows(
            """
            SELECT id_retiro, de, concepto, ret
              FROM scintela.retiros
             WHERE fecha >= date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date)
               AND fecha <  date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date) + INTERVAL '1 month'
               AND COALESCE(ret, 0) <> 0
            """
        )
    ]


def _det_vsto(bal: dict) -> list[dict]:
    """Stock MP+Prod: una fila por etapa, con los KILOS y el $/kg aparte.

    El stock se valúa a promedio ponderado, así que un cambio de tarifa mueve
    el valor de TODO el stock de golpe. Guardar `cantidad` y `precio` por
    separado es lo que después deja partir el Δ en "entraron kilos" vs
    "cambió el $/kg" — que son dos noticias completamente distintas.
    """
    etapas = (bal or {}).get("stock_etapas") or {}
    rot = {"hilado": "Stock hilado", "tejido": "Stock tejido",
           "terminado": "Stock terminado"}
    out = []
    for k, r in rot.items():
        e = etapas.get(k) or {}
        if not e:
            continue
        out.append({"doc_id": f"#{k}", "etiqueta": r,
                    "importe": _f(e.get("us")),
                    "cantidad": _f(e.get("kg")), "precio": _f(e.get("ukg"))})
    return out


def _det_vqx(bal: dict) -> list[dict]:
    """Stock de químicos. Viene de formulas_app en vivo y sin caché: hoy es un
    solo número y no hay detalle por colorante del lado de PC."""
    v = _f(((bal or {}).get("diagnostico") or {}).get("componentes", {}).get("vqx"))
    return [{"doc_id": "#quimicos", "etiqueta": "Stock de químicos (formulas_app)",
             "importe": v}] if v else []


def detalle(bal: dict) -> list[dict]:
    """La foto de detalle completa: componente + documento + importe.

    Cierra contra el balance por construcción: a cada componente se le agrega
    una fila `#ajuste:<comp>` con lo que el detalle no llegó a explicar.
    """
    comp = ((bal or {}).get("diagnostico") or {}).get("componentes") or {}
    fuentes = {
        "facturas": _det_facturas, "cheques": _det_cheques, "caja": _det_caja,
        "bancos": _det_bancos, "antic": _det_antic, "totp": _det_totp,
        "uret": _det_uret,
        "umaq": lambda: _det_activos("umaq"), "uact": lambda: _det_activos("uact"),
        "vsto": lambda: _det_vsto(bal), "vqx": lambda: _det_vqx(bal),
    }
    out: list[dict] = []
    for c, _signo in COMPONENTES:
        try:
            filas = fuentes[c]() or []
        except Exception as e:  # noqa: BLE001
            _LOG.warning("dia: detalle de %s falló (%s)", c, e)
            filas = []
        suma = 0.0
        for f in filas:
            f["componente"] = c
            f.setdefault("cantidad", None)
            f.setdefault("precio", None)
            suma += _f(f.get("importe"))
            out.append(f)
        objetivo = _f(comp.get(_CLAVE_BALANCE[c]))
        falta = round(objetivo - suma, 2)
        if abs(falta) >= UMBRAL:
            # No se explica con documentos. Queda con nombre y a la vista: es
            # exactamente la lista de tareas del entrenamiento.
            out.append({"componente": c, "doc_id": f"#ajuste:{c}",
                        "etiqueta": f"{ETIQUETAS[c]}: sin explicar por documento",
                        "importe": falta, "cantidad": None, "precio": None})
    return out


# ── Reglas: cómo se llama en castellano lo que pasó ─────────────────────────

#: Familias que DEBERÍAN netear cero. Si no netean, esa diferencia es la
#: explicación real (una retención, un descuento, una NC, un cheque devuelto).
FAMILIAS = {"utilidad", "traspaso", "sin_explicar"}


def regla(componente: str, tipo: str, doc_id: str, delta: float) -> tuple[str, str]:
    """(nombre en castellano, familia) de un movimiento.

    `familia`:
      · `traspaso`    — plata que cambia de lugar sin crear ni destruir
                        utilidad (cobranzas, depósitos, pagos de deuda).
      · `utilidad`    — sí mueve el resultado.
      · `sin_explicar`— un `#ajuste`: todavía no sabemos por qué.
    """
    doc_id = doc_id or ""
    if doc_id.startswith("#ajuste:"):
        return "Sin explicar todavía", "sin_explicar"

    if componente == "facturas":
        if tipo == "alta":
            return "Venta facturada", "utilidad"
        if tipo == "baja":
            return "Factura cancelada del todo", "traspaso"
        return ("Abono a factura" if delta < 0 else "Factura corregida en más"), "traspaso"

    if componente == "cheques":
        if tipo == "alta":
            return "Cheque recibido", "traspaso"
        if tipo == "baja":
            return "Cheque depositado o dado de baja", "traspaso"
        return "Cheque corregido", "traspaso"

    if componente == "bancos":
        if doc_id.startswith("#pos"):
            return "Cheques emitidos sin debitar", "traspaso"
        return "Movimiento bancario", "traspaso"

    if componente == "caja":
        if doc_id == "#apertura":
            return "Apertura de caja", "traspaso"
        return ("Ingreso de caja" if delta > 0 else "Gasto de caja"), "utilidad"

    if componente == "totp":
        # Ojo con el signo: `delta` es del componente, y el pasivo aporta al
        # revés. Una deuda que SUBE (delta > 0) BAJA la utilidad.
        if tipo == "alta":
            return "Deuda nueva cargada", "utilidad"
        if tipo == "baja":
            return "Deuda pagada o dada de baja", "traspaso"
        return "Deuda corregida", "utilidad"

    if componente == "antic":
        if doc_id == "#recibidos":
            return "Anticipos cuya mercadería ya entró al stock", "traspaso"
        return ("Anticipo entregado" if delta > 0 else "Anticipo aplicado"), "traspaso"

    if componente in ("umaq", "uact"):
        if tipo == "cambio":
            # MENU.PRG prorratea la amortización por día del mes: el valor en
            # libros baja SOLO todos los días. No es un error.
            return "Amortización del día", "utilidad"
        return ("Activo dado de alta" if tipo == "alta" else "Activo dado de baja"), "utilidad"

    if componente == "uret":
        return "Retiro de dividendos", "traspaso"

    if componente == "vsto":
        return "Stock", "utilidad"

    if componente == "vqx":
        return "Stock de químicos", "utilidad"

    return ETIQUETAS.get(componente, componente), "utilidad"


def _partir_stock(fila: dict, antes: dict) -> list[dict]:
    """Parte el Δ de una etapa de stock en KILOS y TARIFA.

        Δvalor = Δkg × precio_viejo  +  kg_nuevos × Δprecio

    Son dos noticias distintas: "entraron/salieron kilos" es producción real,
    "cambió el $/kg" es una revaluación que mueve TODO el stock de un saque y
    no vendió ni produjo nada.
    """
    kg0, kg1 = _f(antes.get("cantidad")), _f(fila.get("cantidad"))
    p0, p1 = _f(antes.get("precio")), _f(fila.get("precio"))
    if not (kg0 or kg1) or not (p0 or p1):
        return []
    d_kg = round((kg1 - kg0) * p0, 2)
    d_pr = round(kg1 * (p1 - p0), 2)
    rot = fila.get("etiqueta") or "Stock"
    out = []
    if abs(d_kg) >= UMBRAL:
        out.append({"sub": "kilos", "delta": d_kg,
                    "regla": f"{rot}: entraron/salieron kilos",
                    "detalle": f"{kg1 - kg0:+,.2f} kg a $ {p0:,.4f}/kg"})
    if abs(d_pr) >= UMBRAL:
        out.append({"sub": "tarifa", "delta": d_pr,
                    "regla": f"{rot}: cambió el $/kg",
                    "detalle": f"$ {p0:,.4f} → $ {p1:,.4f} sobre {kg1:,.2f} kg"})
    return out


# ── Captura ─────────────────────────────────────────────────────────────────

def _foto_guardada() -> dict[tuple[str, str], dict]:
    filas = _rows("SELECT componente, doc_id, etiqueta, importe, cantidad, precio "
                  "FROM scintela.dia_detalle")
    return {(r["componente"], r["doc_id"]): r for r in filas}


def _diff(nueva: list[dict], vieja: dict) -> list[dict]:
    """Movimientos entre la foto vieja y la nueva.

    Un Δ de stock se parte en kilos y tarifa (dos movimientos en vez de uno),
    porque decir "el stock subió $80.000" no explica nada y decir "entraron
    12.000 kg" o "la tarifa pasó de 2,99 a 3,02" sí.
    """
    movs: list[dict] = []
    vistas = set()
    for f in nueva:
        clave = (f["componente"], f["doc_id"])
        vistas.add(clave)
        antes = vieja.get(clave)
        imp1 = _f(f.get("importe"))
        imp0 = _f(antes.get("importe")) if antes else 0.0
        d = round(imp1 - imp0, 2)
        if abs(d) < UMBRAL:
            continue
        tipo = "cambio" if antes else "alta"
        if f["componente"] == "vsto" and antes:
            partes = _partir_stock(f, antes)
            if partes:
                for p in partes:
                    movs.append({
                        "componente": "vsto", "tipo": "cambio",
                        "doc_id": f"{f['doc_id']}:{p['sub']}",
                        "etiqueta": p["detalle"],
                        "importe_antes": None, "importe_despues": None,
                        "delta": p["delta"], "aporte": p["delta"],
                        "regla": p["regla"], "familia": "utilidad",
                    })
                # El redondeo de la partición no puede perder plata.
                resto = round(d - sum(p["delta"] for p in partes), 2)
                if abs(resto) >= UMBRAL:
                    movs.append({
                        "componente": "vsto", "tipo": "cambio",
                        "doc_id": f"{f['doc_id']}:resto", "etiqueta": f.get("etiqueta"),
                        "importe_antes": imp0, "importe_despues": imp1,
                        "delta": resto, "aporte": resto,
                        "regla": "Stock: redondeo de la partición", "familia": "utilidad",
                    })
                continue
        r, fam = regla(f["componente"], tipo, f["doc_id"], d)
        movs.append({
            "componente": f["componente"], "tipo": tipo, "doc_id": f["doc_id"],
            "etiqueta": f.get("etiqueta"),
            "importe_antes": imp0 if antes else None, "importe_despues": imp1,
            "delta": d, "aporte": round(d * SIGNO[f["componente"]], 2),
            "regla": r, "familia": fam,
        })
    for clave, a in vieja.items():
        if clave in vistas:
            continue
        imp0 = _f(a.get("importe"))
        if abs(imp0) < UMBRAL:
            continue
        comp = clave[0]
        if comp not in SIGNO:
            continue
        d = round(-imp0, 2)
        r, fam = regla(comp, "baja", clave[1], d)
        movs.append({
            "componente": comp, "tipo": "baja", "doc_id": clave[1],
            "etiqueta": a.get("etiqueta"),
            "importe_antes": imp0, "importe_despues": 0.0,
            "delta": d, "aporte": round(d * SIGNO[comp], 2),
            "regla": r, "familia": fam,
        })
    movs.sort(key=lambda m: abs(m["aporte"]), reverse=True)
    return movs


#: Filas por sentencia al reescribir la foto. La cartera viva son miles de
#: documentos: una fila por INSERT dejaría la transacción abierta demasiado
#: tiempo, y esto corre en el hilo de fondo del server de producción.
LOTE = 500


def _insertar_en_lote(conn, tabla: str, cols: tuple[str, ...], filas: list[dict]) -> int:
    """INSERT multi-fila por lotes. Devuelve cuántas filas metió."""
    n = 0
    campos = ", ".join(cols)
    for i in range(0, len(filas), LOTE):
        trozo = filas[i:i + LOTE]
        marcas = ", ".join(
            "(" + ", ".join(f"%({c}_{j})s" for c in cols) + ")"
            for j in range(len(trozo)))
        params = {f"{c}_{j}": f.get(c) for j, f in enumerate(trozo) for c in cols}
        db.execute(f"INSERT INTO {tabla} ({campos}) VALUES {marcas}", params, conn=conn)
        n += len(trozo)
    return n


def capturar(momento: str = "manual", bal: dict | None = None) -> dict:
    """Saca una foto, la diffea contra la anterior y guarda los movimientos.

    Nunca levanta: si algo falla, se loguea y devuelve `{"ok": False}`. Esto
    cuelga del hilo de fondo y no puede tumbar nada.
    """
    res = {"ok": False, "id_captura": None, "movimientos": 0, "motivo": ""}
    try:
        if bal is None:
            from modules.informes import queries as _q
            bal = _q.informe_balance()
        comp = ((bal or {}).get("diagnostico") or {}).get("componentes") or {}
        if comp.get("utilidad") is None:
            res["motivo"] = "balance sin componentes"
            return res

        fila = {"fecha_ec": hoy_ec(), "momento": (momento or "manual")[:20],
                "utilidad": _f(comp.get("utilidad")),
                "patr_neto": round(_f(comp.get("patr")) - _f(comp.get("uret")), 2)}
        for c, _s in COMPONENTES:
            fila[c] = _f(comp.get(_CLAVE_BALANCE[c]))
        # Los KILOS por etapa (mig 0162). Van guardados y no calculados al
        # leer porque NO se pueden reconstruir para atrás: salen de Asinfo en
        # vivo (saldo de bodega) o del cuadro del dBase del mes corriente.
        etapas = (bal or {}).get("stock_etapas") or {}
        for et in ("hilado", "tejido", "terminado"):
            e = etapas.get(et) or {}
            fila[f"{et}_kg"] = _f(e.get("kg")) or None
            fila[f"{et}_ukg"] = _f(e.get("ukg")) or None

        nueva = detalle(bal)
        vieja = _foto_guardada()
        # La PRIMERA captura de la historia no tiene contra qué compararse: si
        # se diffeara contra la nada, cada factura viva de la cartera saldría
        # como "venta de hoy" y el primer día mostraría un Δ del tamaño del
        # balance entero. Se guarda la foto y listo — el primer día explicable
        # es el siguiente. (Mismo criterio que `traza.con_deltas`, que deja la
        # foto más vieja con delta None.)
        #
        # ⚠ "Primera" se decide por si hubo alguna CAPTURA, no por si la foto
        # está vacía: una foto legítimamente vacía (todo cobrado, todo pagado)
        # no puede hacer que la captura siguiente se descarte entera.
        primera = not _rows("SELECT 1 AS x FROM scintela.dia_captura LIMIT 1")
        movs = [] if primera else _diff(nueva, vieja)

        cols = list(fila.keys())
        campos = ", ".join(cols)
        marcas = ", ".join(f"%({c})s" for c in cols)
        with db.tx() as conn:
            cap = db.execute_returning(
                f"INSERT INTO scintela.dia_captura ({campos}) VALUES ({marcas}) "
                f"ON CONFLICT DO NOTHING RETURNING id_captura",
                fila, conn=conn,
            )
            if not cap:
                # El índice único de ancla ya tenía la captura de este momento:
                # el hilo de fondo pasó dos veces. No es un error.
                res["motivo"] = "ya había captura de este momento"
                return res
            idc = cap["id_captura"]
            for m in movs:
                m["id_captura"] = idc
            _insertar_en_lote(
                conn, "scintela.dia_movimiento",
                ("id_captura", "componente", "tipo", "doc_id", "etiqueta",
                 "importe_antes", "importe_despues", "delta", "aporte",
                 "regla", "familia"), movs)
            # La foto rodante se reemplaza entera: lo que había que conservar
            # (el diff) ya salió a dia_movimiento.
            db.execute("DELETE FROM scintela.dia_detalle", None, conn=conn)
            _insertar_en_lote(
                conn, "scintela.dia_detalle",
                ("componente", "doc_id", "etiqueta", "importe", "cantidad",
                 "precio"), nueva)
        res.update(ok=True, id_captura=idc, movimientos=len(movs),
                   primera=primera)
    except Exception as e:  # noqa: BLE001 -- cuelga del hilo de fondo
        _LOG.warning("dia: no pude capturar (%s)", e)
        res["motivo"] = str(e)[:150]
    return res


def correr_si_toca() -> dict:
    """Entrada del hilo de fondo. Clava las capturas de 07:00 y 19:00 EC.

    La idempotencia NO se apoya en una variable de proceso (se pierde en cada
    restart, y el server reinicia): la garantiza el índice único
    `(fecha_ec, momento)` de la tabla. El hilo puede pasar cien veces.
    """
    res = {"capturado": "", "motivo": ""}
    if os.environ.get("DIA_EXPLICACION", "1").strip() == "0":
        res["motivo"] = "apagado"
        return res
    try:
        h = ahora_ec().hour
        hoy = hoy_ec()
        pendientes = []
        if h >= _hora("DIA_HORA_CIERRE", HORA_CIERRE):
            pendientes.append("cierre")
        if h >= _hora("DIA_HORA_MANANA", HORA_MANANA):
            pendientes.append("manana")
        if not pendientes:
            res["motivo"] = "todavía no son las 7"
            return res
        hechas = {r["momento"] for r in _rows(
            "SELECT momento FROM scintela.dia_captura WHERE fecha_ec = %s", (hoy,))}
        for m in pendientes:
            if m in hechas:
                continue
            r = capturar(m)
            if r.get("ok"):
                res["capturado"] = m
                _LOG.info("dia: captura '%s' de %s (%s movimientos)",
                          m, hoy, r.get("movimientos"))
                break
            res["motivo"] = r.get("motivo") or ""
    except Exception as e:  # noqa: BLE001 -- el hilo no se cae por esto
        _LOG.warning("dia: correr_si_toca (%s)", e)
        res["motivo"] = str(e)[:150]
    return res


# ── El resumen para accionistas ─────────────────────────────────────────────
# TMT 2026-08-05 (dueña, viendo la primera versión): *"la explicación debería
# ser más: se produjo tanta tela, eso cambió de valor de x a x. se vendió tanto
# etc. algo más senior resumido digestible para accionistas"*.
#
# El desglose por componente y por documento contesta "¿de dónde salió cada
# peso?" — la pregunta del que audita. El accionista hace otra: **qué pasó en
# la fábrica**. Esa se contesta en KILOS y en tres frases, no en once
# componentes contables.
#
# Las tres cifras derivadas de abajo son la parte que hace que el resumen
# suene a fábrica y no a balance. Cada una sale de una identidad simple, y
# cada una lleva su supuesto escrito al lado — se marcan como ESTIMADAS en la
# pantalla porque un ajuste de inventario las corre sin avisar:
#
#   producción terminada ≈ Δ kg terminado + kg vendidos
#   consumo de hilado    ≈ kg comprados  − Δ kg hilado
#   cobrado              ≈ $ facturado   − Δ cartera de facturas

ETAPAS = (("hilado", "Hilado"), ("tejido", "Tejido crudo"),
          ("terminado", "Tela terminada"))


def ventas_del_dia(fecha) -> dict:
    """Lo facturado ESE día: {n, kg, us}. Por `fecha` del documento, no por
    `fecha_crea` (que el sync del dBase pisa). Las anuladas no cuentan, así que
    un resumen que se vuelve a mirar meses después dice la verdad de hoy."""
    r = _rows(
        """
        SELECT COUNT(*) AS n, COALESCE(SUM(kg), 0) AS kg,
               COALESCE(SUM(importe), 0) AS us
          FROM scintela.factura
         WHERE fecha = %s
           AND COALESCE(stat, '') NOT IN ('X', 'Y')
        """, (fecha,))
    d = r[0] if r else {}
    return {"n": int(d.get("n") or 0), "kg": _f(d.get("kg")), "us": _f(d.get("us"))}


def compras_del_dia(fecha) -> dict:
    """Lo comprado ESE día: {n, kg, us}. Mismo criterio de fecha."""
    r = _rows(
        """
        SELECT COUNT(*) AS n, COALESCE(SUM(kg), 0) AS kg,
               COALESCE(SUM(importe), 0) AS us
          FROM scintela.compra
         WHERE fecha = %s
        """, (fecha,))
    d = r[0] if r else {}
    return {"n": int(d.get("n") or 0), "kg": _f(d.get("kg")), "us": _f(d.get("us"))}


def produccion_del_dia(fecha) -> dict:
    """Kilos PRODUCIDOS, DESPACHADOS y DESPERDICIADOS ese día.

    ⭐ TMT 2026-08-05: *"se produce 24/7, algo no sabés"*. Tenía razón.

    La primera versión DERIVABA la producción del stock
    (`Δ kg terminado + kg vendidos`). Está mal por tres motivos, y el error
    medido fue de 2,4× (5.280 kg derivados contra 12.838 reales el 04/08):

    1. **Hay medida directa**: `cantidad_fabricada` de las órdenes de
       fabricación que cierran en bodega 53. No hay por qué derivar nada.
    2. **El saldo de bodega no cuadra** contra producido − despachado: la
       pantalla `/produccion-terminado-asinfo` tiene una columna
       *"diferencia de medición"* que en agosto acumulaba +11.539 kg, más de
       un tercio de la producción del mes. Cualquier derivación arrastra eso.
    3. **Falta el desperdicio** — ~5 % en terminado. No está ni en el stock ni
       en las ventas: se evapora entre las dos puntas.

    Así que esto NO calcula nada: lee la fila del día de
    `terminado_asinfo.resumen()`, que ya modela
    `INICIAL + PRODUCIDO − VENDIDO = FINAL` con el desperdicio al costado.
    Duplicar la cuenta habría sido inventar un segundo número que discute con
    el primero.

    ⚠ Los kg se imputan al día en que la orden **CIERRA**, no a los días en
    que se produjo. Una orden que tejió tres días descarga todo en uno, así
    que el diario es grumoso aunque la planta no pare: un día en cero
    normalmente significa "no cerró ninguna orden", no "no se trabajó". Por
    eso se devuelve también el acumulado del mes, que es donde el ruido se
    promedia.
    """
    vacio = {"disponible": False}
    try:
        from modules.terminado_asinfo import service as _term
        r = _term.resumen(fecha.year, fecha.month)
    except Exception as e:  # noqa: BLE001 -- Asinfo caído no tumba la pantalla
        _LOG.warning("dia: no pude leer la producción (%s)", e)
        return vacio
    if not (r or {}).get("disponible"):
        return vacio
    dias = ((r.get("dias") or {}).get("filas")) or []
    clave = fecha.isoformat()
    hoy = next((f for f in dias if f.get("periodo") == clave), None)
    mes = (r.get("dias") or {}).get("total") or {}
    # Promedio de los días que SÍ cerraron órdenes: sin meta contra la cual
    # medir (la dueña: *"no tenemos meta"*), la referencia es la propia
    # tendencia. Los días en cero la hundirían sin significar nada.
    con_prod = [f for f in dias if _f(f.get("producido")) > 0]
    prom = (round(sum(_f(f["producido"]) for f in con_prod) / len(con_prod), 2)
            if con_prod else None)
    if not hoy:
        return {"disponible": True, "sin_fila": True, "mes": mes,
                "promedio_dia": prom, "dias_con_produccion": len(con_prod)}
    return {
        "disponible": True, "sin_fila": False,
        "producido": _f(hoy.get("producido")),
        "despachado": _f(hoy.get("vendido")),
        "desperdicio_kg": _f(hoy.get("desperdicio_kg")),
        "desperdicio_pct": hoy.get("desperdicio_pct"),
        "n_ofs": int(hoy.get("n_ofs") or 0),
        "inicial": hoy.get("inicial"), "final": hoy.get("final"),
        "otros": hoy.get("otros"),
        "mes": mes, "promedio_dia": prom,
        "dias_con_produccion": len(con_prod),
    }


def tejido_del_dia(fecha) -> dict:
    """Kilos de tela CRUDA que cerraron ese día (tejeduría, bodega 52).

    TMT 2026-08-05: *"en producción deberíamos decir cuánto se tejió y cuánto
    se terminó"*. Son dos etapas distintas de la misma fábrica y hasta ahora
    la pantalla sólo mostraba la segunda: el hilo se teje (crudo) y recién
    después se tiñe y se termina. Un día puede tejer mucho y terminar poco.

    ⭐ **No se deriva del stock, se MIDE** — misma regla que
    `produccion_del_dia`: sale de `cantidad_fabricada` de las OFs hoja
    cerradas en bodega 52, que es la fuente que ya usa
    `/produccion-tejeduria-asinfo`. Duplicar la cuenta sería inventar un
    segundo número que discute con el primero.

    ⚠ Mismo sesgo que terminado: los kg se imputan al día en que la orden
    **cierra**. Por eso va también el acumulado del mes.
    """
    vacio = {"disponible": False}
    try:
        from modules.asinfo import service as _asinfo
        r = _asinfo.produccion_tejeduria_mes(fecha.year, fecha.month)
    except Exception as e:  # noqa: BLE001 -- Asinfo caído no tumba la pantalla
        _LOG.warning("dia: no pude leer la tejeduría (%s)", e)
        return vacio
    if not (r or {}).get("disponible"):
        return vacio
    clave = fecha.isoformat()
    deldia = [o for o in (r.get("ofs") or []) if str(o.get("dia") or "") == clave]
    return {
        "disponible": True,
        "kg": round(sum(_f(o.get("kg")) for o in deldia), 2),
        "n_ofs": len(deldia),
        "mes_kg": _f(r.get("total_kg")),
    }


def _etapa(desde: dict, hasta: dict, et: str) -> dict:
    """Una etapa de stock entre dos capturas, con el valor partido en kilos y
    tarifa. `None` si esa captura no guardó los kilos (capturas viejas, o
    Asinfo caído)."""
    kg0, kg1 = desde.get(f"{et}_kg"), hasta.get(f"{et}_kg")
    p0, p1 = desde.get(f"{et}_ukg"), hasta.get(f"{et}_ukg")
    if kg0 is None or kg1 is None or p0 is None or p1 is None:
        return {}
    kg0, kg1, p0, p1 = _f(kg0), _f(kg1), _f(p0), _f(p1)
    return {
        "kg0": kg0, "kg1": kg1, "d_kg": round(kg1 - kg0, 2),
        "p0": p0, "p1": p1, "d_p": round(p1 - p0, 4),
        "us0": round(kg0 * p0, 2), "us1": round(kg1 * p1, 2),
        "d_us": round(kg1 * p1 - kg0 * p0, 2),
        "por_kilos": round((kg1 - kg0) * p0, 2),
        "por_tarifa": round(kg1 * (p1 - p0), 2),
    }


def resumen(fecha=None) -> dict:
    """El día contado como se lo cuenta a un accionista: qué se produjo, qué se
    vendió, cuánto cambió de valor la tela y qué quedó de resultado."""
    fecha = fecha or hoy_ec()
    caps = capturas(fecha)
    out = {"fecha": fecha, "ok": False, "dia_parcial": False,
           "etapas": [], "frases": []}
    if len(caps) < 2:
        return out
    d, h = caps[0], caps[-1]
    v, c = ventas_del_dia(fecha), compras_del_dia(fecha)

    etapas = {}
    for et, rot in ETAPAS:
        e = _etapa(d, h, et)
        if e:
            e["rotulo"] = rot
            etapas[et] = e
            out["etapas"].append(dict(e, clave=et))

    por_kilos = round(sum(e["por_kilos"] for e in etapas.values()), 2)
    por_tarifa = round(sum(e["por_tarifa"] for e in etapas.values()), 2)
    d_stock = round(_f(h.get("vsto")) - _f(d.get("vsto")), 2)
    d_cartera = round(_f(h.get("facturas")) - _f(d.get("facturas")), 2)
    d_deuda = round(_f(h.get("totp")) - _f(d.get("totp")), 2)
    d_util = round(_f(h.get("utilidad")) - _f(d.get("utilidad")), 2)

    hil = etapas.get("hilado") or {}
    prod = produccion_del_dia(fecha)
    out.update({
        "ok": True, "desde": d, "hasta": h,
        # Lo setea `ventana()`: si el arranque es del mismo día, el tramo es
        # más corto que 24 h. Tiene que llegar hasta el mensaje de WhatsApp o
        # se manda un número que no se puede comparar contra otros días sin
        # que nadie lo sepa.
        "dia_parcial": bool(d.get("fecha_ec") == h.get("fecha_ec")),
        "d_utilidad": d_util, "ventas": v, "compras": c,
        "d_stock": d_stock, "por_kilos": por_kilos, "por_tarifa": por_tarifa,
        "d_cartera": d_cartera, "d_deuda": d_deuda,
        "produccion": prod,
        # Lo cobrado sigue siendo derivado (facturado − Δ cartera), pero acá la
        # identidad es exacta salvo retenciones y NC — se cruzó contra la plata
        # que entró a bancos+caja+cheques y dio $292 de diferencia sobre
        # $61.847. La producción, en cambio, ya NO se deriva: ver
        # `produccion_del_dia`.
        "cobrado": round(v["us"] - d_cartera, 2),
        "tarifa_quieta": abs(_f(hil.get("d_p"))) < 0.00005 if hil else None,
        # Precio realizado y margen: los dos números con los que se dirige una
        # fábrica. El costo de lo despachado se valúa a la tarifa de terminado.
        "precio_kg": (round(v["us"] / v["kg"], 4) if v["kg"] else None),
    })
    ter = etapas.get("terminado") or {}
    # ⚠ El costo se valúa con los kilos FACTURADOS, no con los despachados.
    # Verificado en vivo el 05/08: se habían facturado 545 kg pero despachado
    # 2.069 físicos (la mercadería sale un día y se factura otro), y el margen
    # salía **−134,7 %**. Son dos universos distintos: la plata viene de la
    # factura, así que el costo tiene que venir de los mismos kilos que esa
    # factura. Los despachados siguen contándose aparte, en la línea de
    # producción, que es donde significan algo.
    if ter.get("p1") and v["kg"]:
        costo = round(v["kg"] * _f(ter["p1"]), 2)
        out["costo_despachado"] = costo
        out["margen"] = round(v["us"] - costo, 2)
        out["margen_pct"] = (round(100.0 * out["margen"] / v["us"], 1)
                             if v["us"] else None)
    return out


# ── El mensaje de WhatsApp ──────────────────────────────────────────────────
# TMT 2026-08-05: *"me gustaría también mostrar la utilidad hasta hoy, algo para
# mandar chiquito e informativo a mí, andres y federico por whatsapp"*.
#
# ⭐ La utilidad del balance YA ES la del mes acumulada — `PATR − PATANT +
# retiros`, contra el último cierre. O sea que "la utilidad hasta hoy" no hay
# que calcularla: es el mismo número que la pantalla muestra arriba. Lo que
# cambia es el encuadre: el titular pasa a ser el ACUMULADO y el día queda
# como el aporte de la jornada.
#
# Se genera el texto y se muestra para copiar. **Programa Core no manda nada**:
# el mensaje sale del teléfono de quien aprieta, con su WhatsApp y su cuenta.

#: Ancho cómodo de lectura en un teléfono. Más que esto y WhatsApp corta las
#: líneas por su cuenta, en el lugar equivocado.
ANCHO_WA = 34

_MESES = ("ene", "feb", "mar", "abr", "may", "jun",
          "jul", "ago", "sep", "oct", "nov", "dic")
_DIAS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")


def ventas_del_mes(fecha) -> dict:
    """Lo facturado en el mes hasta `fecha` inclusive."""
    r = _rows(
        """
        SELECT COUNT(*) AS n, COALESCE(SUM(kg), 0) AS kg,
               COALESCE(SUM(importe), 0) AS us
          FROM scintela.factura
         WHERE fecha >= date_trunc('month', %s::date)::date
           AND fecha <= %s
           AND COALESCE(stat, '') NOT IN ('X', 'Y')
        """, (fecha, fecha))
    d = r[0] if r else {}
    return {"n": int(d.get("n") or 0), "kg": _f(d.get("kg")), "us": _f(d.get("us"))}


def _n(v, dec: int = 0) -> str:
    from filters import num_es
    return num_es(round(_f(v), dec), dec)


def porque_subio(e: dict, n: int = 3) -> list[dict]:
    """Las `n` líneas que explican el día + un "Resto", **sumando exacto**.

    Es el bloque *Por qué subió* de la pantalla. La última fila tiene que dar
    `e["d_utilidad"]` o el lector deja de creerle a la tabla: por eso el resto
    NO se descarta ni se redondea, se calcula como la diferencia contra el
    total. Ahí adentro caen los movimientos chicos y los traspasos, que netean
    cero de a pares.

    🚨 Las etiquetas son las que da el motor (`Venta facturada`, `Stock`,
    `Amortización del día`). Tentación a evitar: poner acá *"Costo de la tela
    vendida −$ 70.573"*, que es el número del margen. No es un movimiento: el
    componente de stock se mueve por lo que se produjo Y por lo que salió, y
    mezclarlos rompería la suma. La cuenta del margen va en el titular, en
    castellano, donde no promete cuadrar con nada.
    """
    total = _f((e or {}).get("d_utilidad"))
    filas = [r for r in ((e or {}).get("reglas") or [])
             if r.get("familia") in ("utilidad", "sin_explicar")
             and abs(_f(r.get("aporte"))) >= 1]
    filas.sort(key=lambda r: abs(_f(r.get("aporte"))), reverse=True)
    top = filas[:max(0, n)]
    out = [{"label": r.get("regla") or "—", "monto": round(_f(r.get("aporte")), 2),
            "familia": r.get("familia")} for r in top]
    resto = round(total - sum(x["monto"] for x in out), 2)
    if abs(resto) >= 1:
        out.append({"label": "Resto — stock, caja, bancos", "monto": resto,
                    "familia": "resto"})
    return out


def deuda_hoy(fecha=None) -> dict:
    """La deuda a proveedores (posdatados) y cuánto de eso vence pronto.

    TMT 2026-08-05: *"no decís nada de posdatados"*. La utilidad ya los tiene
    adentro — `totp` aporta con signo −1, una deuda nueva la baja — pero el
    **nivel** y el **vencimiento** no salían en ningún lado del día, y son la
    otra mitad de la pregunta de un accionista: no sólo *cuánto ganamos*, sino
    *cuánto hay que pagar y cuándo*.

    ⭐ Pasivos = `posdat` con **`banc = 0`** (los `banc = 9` son cheques ya
    emitidos, no deuda abierta). Es la misma definición que usa el Balance:
    si un día no coinciden, el que está mal es éste.
    """
    fecha = fecha or hoy_ec()
    r = _rows(
        """
        SELECT COUNT(*) AS n,
               COALESCE(SUM(importe), 0) AS total,
               COALESCE(SUM(importe) FILTER (WHERE fechad < %s), 0) AS vencido,
               COALESCE(SUM(importe) FILTER (
                   WHERE fechad >= %s AND fechad < %s + 7), 0) AS prox7,
               COALESCE(SUM(importe) FILTER (
                   WHERE fechad >= %s AND fechad < %s + 30), 0) AS prox30
          FROM scintela.posdat
         WHERE banc = 0 AND NOT COALESCE(anulada, FALSE)
        """, (fecha, fecha, fecha, fecha, fecha))
    d = r[0] if r else {}
    return {
        "n": int(d.get("n") or 0),
        "total": _f(d.get("total")),
        "vencido": _f(d.get("vencido")),
        "prox7": _f(d.get("prox7")),
        "prox30": _f(d.get("prox30")),
    }


def motores_del_dia(fecha, n: int = 3) -> list[dict]:
    """Las `n` reglas que MÁS movieron la utilidad del día, con su aporte.

    TMT 2026-08-05: la nota tiene que contestar *por qué* subió, no sólo
    *cuánto*. El motor ya lo sabe — `explicar()` agrupa cada movimiento por
    regla — así que esto es puro recorte y orden, sin una consulta nueva.

    ⭐ **Sólo entran las familias que mueven el resultado.** Un `traspaso`
    (cobranza, depósito, pago) netea cero *de a pares*, así que mostrar una
    punta sola —"Cheque recibido +$ 61.847"— haría parecer que la cobranza
    generó utilidad. No la genera: la cambia de lugar.

    ⭐ **`sin_explicar` SÍ entra.** Si lo que más movió la utilidad es un
    `#ajuste`, esconderlo sería mentir sobre el porqué justo el día que más
    importa saberlo. Es la misma regla que en la pantalla, donde el `#ajuste`
    baja el `explicado_pct` en vez de disimularse.
    """
    try:
        e = explicar(fecha)
    except Exception as exc:  # noqa: BLE001
        # La nota es un extra: si la explicación se cae, el resumen igual sale.
        _LOG.warning("dia: no pude armar los motores del día (%s)", exc)
        return []
    filas = [r for r in (e.get("reglas") or [])
             if r.get("familia") in ("utilidad", "sin_explicar")
             and abs(_f(r.get("aporte"))) >= 1]
    filas.sort(key=lambda r: abs(_f(r.get("aporte"))), reverse=True)
    return filas[:max(0, n)]


def _lineas_motores(motores: list[dict]) -> list[str]:
    """Las filas del bloque *Lo movió hoy*, de ANCHO_WA exacto.

    Dos detalles que sólo se ven en un teléfono:
    · **Los importes se alinean entre sí**, no cada uno contra el borde. El
      ancho del número se calcula sobre el bloque entero, así `$ 41.200` y
      `$  3.532` quedan en columna y se comparan de un vistazo. Alinear cada
      línea por su cuenta deja el `$` bailando.
    · **El nombre se corta con puntos suspensivos** si no entra: una línea de
      35 caracteres la parte WhatsApp donde se le antoja y el bloque deja de
      leerse en columna.
    """
    if not motores:
        return []
    nums = [_n(abs(_f(m.get("aporte")))) for m in motores]
    ancho_num = max(len(n) for n in nums)
    out = []
    for m, num in zip(motores, nums):  # noqa: B905
        signo = "+" if _f(m.get("aporte")) >= 0 else "−"
        monto = f"{signo}$ {num:>{ancho_num}}"
        sitio = ANCHO_WA - len(monto) - 1
        if sitio < 1:                  # un importe absurdo: que vaya solo
            out.append(monto[:ANCHO_WA])
            continue
        nombre = m.get("regla") or "—"
        if len(nombre) > sitio:
            nombre = nombre[:sitio - 1].rstrip() + "…"
        out.append(f"{nombre:<{sitio}} {monto}")
    return out


def mensaje_whatsapp(fecha=None) -> str:
    """El resumen del día en texto plano, listo para pegar en WhatsApp.

    Reglas de formato que importan y no son obvias:
    · **Nada de tablas ni markdown raro.** WhatsApp sólo entiende `*negrita*`
      y `_cursiva_`. Una tabla con pipes se ve como basura en un teléfono.
    · **Una idea por línea**, ordenadas de más a menos importante: quien lo
      lee en el celular corta a la tercera.
    · **Sin líneas de relleno.** Si un dato no está, la línea no va — un cero
      o un guión ocupan lo mismo que un número y no dicen nada.
    """
    fecha = fecha or hoy_ec()
    r = resumen(fecha)
    if not r.get("ok"):
        return ""

    h = r["hasta"]
    rot = f"{_DIAS[fecha.weekday()]} {fecha.day} {_MESES[fecha.month - 1]}"
    L = [f"*INTELA · {rot}*", ""]

    # El titular es el ACUMULADO del mes; el día, su aporte.
    L.append(f"*Utilidad de {_MESES[fecha.month - 1]}: "
             f"$ {_n(h.get('utilidad'), 0)}*")
    d = r["d_utilidad"]
    L.append(f"{'Hoy +' if d >= 0 else 'Hoy −'}$ {_n(abs(d), 0)}")
    if r.get("dia_parcial"):
        L.append("_(tramo corto, no son 24 h)_")
    L.append("")

    # El porqué va ARRIBA de los kilos: es lo que un accionista pregunta
    # apenas ve el número, y el que lee en el celular corta a la tercera
    # línea. Si el día no movió nada explicable, el bloque entero no va —
    # misma regla que el resto: un título con nada abajo no dice nada.
    motores = motores_del_dia(fecha)
    if motores:
        L.append("*Lo movió hoy*")
        L.extend(_lineas_motores(motores))
        L.append("")

    p = r.get("produccion") or {}
    # Un 0 acá casi nunca significa "no se produjo": significa que todavía no
    # cerró ninguna orden. A media mañana es siempre 0. Misma regla que el
    # resto: si el dato no dice nada, la línea no va.
    if p.get("disponible") and not p.get("sin_fila") and _f(p.get("producido")):
        linea = f"Producción  {_n(p.get('producido'))} kg"
        mes = p.get("mes") or {}
        if mes.get("producido"):
            linea += f" · mes {_n(mes['producido'])}"
        L.append(linea)

    v, vm = r["ventas"], ventas_del_mes(fecha)
    if v["n"]:
        L.append(f"Ventas      $ {_n(v['us'])} · {_n(v['kg'])} kg")
    if vm["us"]:
        L.append(f"  mes       $ {_n(vm['us'])} · {_n(vm['kg'])} kg")
    if r.get("margen_pct") is not None:
        L.append(f"Margen      {_n(r['margen_pct'], 1)} %")
    if r.get("cobrado"):
        L.append(f"Cobrado     $ {_n(r['cobrado'])}")

    return "\n".join(L).rstrip()


# ── Lectura: la explicación ─────────────────────────────────────────────────

def capturas(fecha) -> list[dict]:
    return _rows(
        """
        SELECT *, TO_CHAR(creado_en AT TIME ZONE 'America/Guayaquil', 'HH24:MI') AS hora
          FROM scintela.dia_captura
         WHERE fecha_ec = %s
         ORDER BY creado_en ASC
        """, (fecha,))


def ventana(fecha) -> tuple[dict | None, dict | None]:
    """Las dos puntas del día: el cierre de AYER y el cierre de HOY.

    ⭐ TMT 2026-08-05, corrigiendo el diseño original: *"se produce 24/7"*.
    La primera versión comparaba la foto de las 07:00 contra la de las 19:00 —
    doce horas. Eso sirve para una oficina; para una planta que no para deja
    afuera el turno noche entero, que es donde cierra buena parte de las
    órdenes de fabricación. Peor: las ventas y las compras del día SÍ son de
    24 h, así que la comparación mezclaba dos ventanas distintas y hacía
    parecer que la fábrica producía la mitad de lo que produce.

    Ahora el día son **24 horas**: la última captura de ayer contra la última
    de hoy. Si no hay captura de ayer (el primer día, o el server estuvo
    caído), cae en la primera de hoy y se avisa, porque ese tramo es más corto
    y los números no son comparables contra los de otros días.
    """
    hoy = capturas(fecha)
    if not hoy:
        return None, None
    ayer = _rows(
        """
        SELECT *, TO_CHAR(creado_en AT TIME ZONE 'America/Guayaquil', 'HH24:MI') AS hora
          FROM scintela.dia_captura
         WHERE fecha_ec < %s
         ORDER BY creado_en DESC
         LIMIT 1
        """, (fecha,))
    if ayer:
        return ayer[0], hoy[-1]
    return (hoy[0], hoy[-1]) if len(hoy) > 1 else (None, None)


def explicar(fecha=None) -> dict:
    """La explicación del día: Δ utilidad, quién lo movió, y qué falta explicar.

    La ventana son **24 horas** — cierre de ayer contra cierre de hoy. Ver
    `ventana()` para por qué no son las 07:00→19:00 del diseño original.
    """
    fecha = fecha or hoy_ec()
    caps = capturas(fecha)
    out = {
        "fecha": fecha, "capturas": caps, "ok": False, "motivo": "",
        "desde": None, "hasta": None, "d_utilidad": 0.0, "dia_parcial": False,
        "componentes": [], "movimientos": [], "familias": [],
        "sin_explicar": [], "residuo": 0.0, "explicado_pct": 100.0,
    }
    desde, hasta = ventana(fecha)
    if not desde or not hasta:
        out["motivo"] = ("Todavía no hay con qué comparar: hace falta una captura "
                         "de ayer o una segunda de hoy.")
        if caps:
            out["desde"] = caps[0]
        return out

    # Si el arranque es del mismo día, el tramo es más corto que 24 h y los
    # números no se pueden comparar contra los de otros días.
    out["dia_parcial"] = bool(desde.get("fecha_ec") == hasta.get("fecha_ec"))
    out["desde"], out["hasta"] = desde, hasta
    out["d_utilidad"] = round(_f(hasta.get("utilidad")) - _f(desde.get("utilidad")), 2)

    for c, s in COMPONENTES:
        d = round(_f(hasta.get(c)) - _f(desde.get(c)), 2)
        if abs(d) < UMBRAL:
            continue
        out["componentes"].append({
            "col": c, "label": ETIQUETAS[c], "delta": d,
            "aporte": round(d * s, 2),
        })
    out["componentes"].sort(key=lambda x: abs(x["aporte"]), reverse=True)

    # Todos los movimientos de la ventana: las capturas POSTERIORES al
    # arranque y hasta el cierre, inclusive. Cruza la medianoche, así que se
    # filtra por id_captura y no por fecha_ec.
    movs = _rows(
        "SELECT * FROM scintela.dia_movimiento "
        " WHERE id_captura > %s AND id_captura <= %s "
        " ORDER BY ABS(aporte) DESC",
        (desde["id_captura"], hasta["id_captura"]))
    out["movimientos"] = movs

    por_fam: dict[str, dict] = {}
    por_regla: dict[str, dict] = {}
    for m in movs:
        fam = m.get("familia") or "utilidad"
        f = por_fam.setdefault(fam, {"familia": fam, "aporte": 0.0, "n": 0})
        f["aporte"] = round(f["aporte"] + _f(m.get("aporte")), 2)
        f["n"] += 1
        r = por_regla.setdefault(m.get("regla") or "—", {
            "regla": m.get("regla") or "—", "familia": fam, "aporte": 0.0, "n": 0})
        r["aporte"] = round(r["aporte"] + _f(m.get("aporte")), 2)
        r["n"] += 1
    out["familias"] = sorted(por_fam.values(), key=lambda x: abs(x["aporte"]), reverse=True)
    out["reglas"] = sorted(por_regla.values(), key=lambda x: abs(x["aporte"]), reverse=True)
    out["sin_explicar"] = [m for m in movs if (m.get("familia") == "sin_explicar")]

    total = round(sum(_f(m.get("aporte")) for m in movs), 2)
    out["residuo"] = round(out["d_utilidad"] - total, 2)
    ciego = round(sum(abs(_f(m.get("aporte"))) for m in out["sin_explicar"]), 2)
    bruto = round(sum(abs(_f(m.get("aporte"))) for m in movs), 2)
    out["explicado_pct"] = round(100.0 * (1 - ciego / bruto), 1) if bruto else 100.0
    out["ok"] = abs(out["residuo"]) < 1 and not out["sin_explicar"]
    return out


def racha_limpia(dias: int = 30) -> int:
    """Días consecutivos (hacia atrás desde ayer) sin nada sin explicar.

    Es la métrica del entrenamiento: mientras haya `#ajuste`, hay trabajo.
    """
    filas = _rows(
        """
        SELECT c.fecha_ec,
               COUNT(m.id_mov) FILTER (WHERE m.familia = 'sin_explicar') AS ciegos
          FROM scintela.dia_captura c
          LEFT JOIN scintela.dia_movimiento m ON m.id_captura = c.id_captura
         WHERE c.fecha_ec < (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date
         GROUP BY c.fecha_ec
         ORDER BY c.fecha_ec DESC
         LIMIT %s
        """, (max(1, min(365, dias)),))
    n = 0
    for f in filas:
        if int(f.get("ciegos") or 0):
            break
        n += 1
    return n


def guardar_nota(id_captura: int, nota: str) -> bool:
    """La nota que la dueña escribe cuando entiende algo que el sistema no."""
    try:
        db.execute("UPDATE scintela.dia_captura SET nota = %s WHERE id_captura = %s",
                   ((nota or "").strip()[:2000] or None, int(id_captura)))
        return True
    except Exception as e:  # noqa: BLE001
        _LOG.warning("dia: no pude guardar la nota (%s)", e)
        return False
