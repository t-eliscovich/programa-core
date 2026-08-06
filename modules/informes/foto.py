"""El motor de la FOTO: de un componente del balance a los documentos que lo forman.

TMT 2026-08-06 (dueña, mirando /informes/traza): *"¿podés explicarme mejor el
movimiento exacto que causó la movida?"*.

Este módulo es la maquinaria que contesta esa pregunta, y no vive ni en la
traza ni en la explicación del día porque las dos la usan:

    · `detalle(bal)`  saca la foto —componente + documento + importe— y cierra
      contra el balance POR CONSTRUCCIÓN, con una fila `#ajuste:<comp>` por lo
      que el detalle no llegó a explicar.
    · `diff(nueva, vieja)` compara dos fotos y devuelve qué entró, qué salió y
      qué cambió, con el aporte firmado de cada cosa a la utilidad.

Antes esto vivía adentro de `dia.py` y corría dos veces por día. Ahora corre
cada cinco minutos desde `traza.py`, y la explicación del día es la suma de
esas ventanas. Un `#ajuste` que quedaba encerrado en doce horas queda encerrado
en cinco minutos, que es lo que lo vuelve cazable.

Dos cosas que parecen detalles y no lo son:

· **La foto se actualiza POR DIFERENCIA.** Son ~7.100 filas y 288 vueltas por
  día. Borrarla y reescribirla entera —como hacía la versión de dos capturas
  diarias— serían dos millones de escrituras diarias para mover, en una vuelta
  típica, menos de cincuenta filas. `aplicar()` toca sólo lo que cambió.

· **El stock se explica por CAUDALES.** TMT 2026-08-06: *"pero stock también,
  por qué sube y por qué baja"*. El valor de una etapa se mueve por dos motivos
  distintos —entraron/salieron kilos, o cambió el $/kg— y los kilos, a su vez,
  se mueven por producción y por despacho. Los caudales se guardan en la foto
  como filas `#flujo` (importe 0: no suman al balance, sólo se diffean) y salen
  del movimiento REAL de bodega de Asinfo, que es el que cierra con el stock.
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, date, datetime, timedelta

import db

_LOG = logging.getLogger("programa_core.foto")

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

#: El pseudo-componente donde viven los caudales del stock. NO está en
#: COMPONENTES a propósito: no suma al balance y nunca genera un movimiento
#: propio — sólo sirve para poder decir, al diffear, por qué se movieron los
#: kilos de una etapa.
FLUJO = "#flujo"

#: Dónde vive la marca de que la foto ya se sacó alguna vez.
#: 🚨 "Primera foto" NO se decide por si la tabla está vacía. Una foto
#: legítimamente vacía —todo cobrado, todo pagado, sin activos— haría que la
#: vuelta siguiente se diffeara contra la nada y cada factura viva de la
#: cartera saliera como "venta de este minuto". Se decide por esta marca, que
#: se escribe con la primera foto y no se borra nunca. (Mismo criterio que
#: usaba `dia.capturar()` con la existencia de una captura.)
META = "#meta"

#: Qué bodega de Asinfo mide el movimiento físico de cada etapa del stock.
#: 51 = materia prima (hilado) · 52 = tela cruda (tejido) · 53 = terminado.
BODEGA_ETAPA = {"hilado": 51, "tejido": 52, "terminado": 53}

#: Cómo se llama, en castellano, que una etapa reciba o entregue kilos.
CAUDALES = {
    ("hilado", "ingreso"):    "entró materia prima",
    ("hilado", "egreso"):     "se consumió hilado en tejeduría",
    ("tejido", "ingreso"):    "se tejió tela cruda",
    ("tejido", "egreso"):     "salió tela cruda a tintorería",
    ("terminado", "ingreso"): "entró tela terminada",
    ("terminado", "egreso"):  "se despachó tela a clientes",
}

#: Cuánto puede fallarle a los caudales antes de que dejemos de usarlos para
#: PARTIR el Δ de kilos y pasen a ser sólo una nota al pie. Los caudales y el
#: saldo de las etapas salen de dos lecturas distintas de Asinfo con cachés
#: propias: en una ventana de cinco minutos se desfasan seguido, y partir el Δ
#: con números que no cierran fabricaría precisión que no tenemos.
TOLERANCIA_CAUDAL = 0.005          # 0,5 % del Δ de kilos

#: Las etapas en el orden en que la tela las recorre. La tela se teje, después
#: se tiñe y se termina: si en una ventana el tejido baja lo mismo que sube el
#: terminado, no pasaron dos cosas — pasó UNA.
ORDEN_ETAPAS = ("hilado", "tejido", "terminado")

#: Cuánto pueden diferir los kilos que salen de una etapa y los que entran a la
#: siguiente para seguir llamándolo traspaso.
TOLERANCIA_TRASPASO = 0.01         # 1 %


# ── Tiempo ──────────────────────────────────────────────────────────────────

def ahora_ec() -> datetime:
    """Ahora en Ecuador (UTC−5, sin horario de verano)."""
    return datetime.now(UTC) - timedelta(hours=5)


def hoy_ec():
    return ahora_ec().date()


def _n(v: float, dec: int = 2, signo: bool = False) -> str:
    """Número en formato Ecuador: punto de miles, coma decimal.

    🚨 El f-string con `:,` imprime `312,486.53` — formato yanqui. Toda la app
    usa `num_es`; acá no se puede (esto arma texto, no renderiza), así que se
    da vuelta a mano.
    """
    crudo = f"{v:+,.{dec}f}" if signo else f"{v:,.{dec}f}"
    return crudo.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _rows(sql: str, params=None) -> list[dict]:
    """SELECT fail-soft: una fuente caída no puede tumbar la captura."""
    try:
        return db.fetch_all(sql, params) or []
    except Exception as e:  # noqa: BLE001
        _LOG.warning("foto: no pude leer el detalle (%s)", e)
        return []


# ── El detalle: qué documentos componen cada componente ─────────────────────

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
        _LOG.warning("foto: no pude leer los bancos (%s)", e)
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
        _LOG.warning("foto: no pude leer P1/P2 (%s)", e)
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
        _LOG.warning("foto: no pude leer anticipos recibidos (%s)", e)
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


def _det_flujo() -> list[dict]:
    """Los CAUDALES de cada etapa del stock, acumulados desde el 1° del mes.

    TMT 2026-08-06: *"pero stock también, por qué sube y por qué baja"*.

    `_det_vsto` dice cuánto vale cada etapa; esto dice por dónde entró y por
    dónde salió. Se usa `movimiento_bodega_mes`, que mide el movimiento REAL de
    la bodega a partir de los deltas del saldo por lote y **cierra por
    construcción con el stock** (identidad telescópica): no la producción
    declarada, que subcuenta, ni el despacho documento, que corre por delante
    del saldo. Es la misma función que alimenta el Flujo de producción.

    Van con `importe = 0`: no son plata, no entran en ningún componente y no
    generan movimientos propios. Están en la foto sólo para poder diffearlas —
    la diferencia entre dos fotos es lo que se movió en esa ventana.

    ⚠ El acumulado se reinicia el 1°. Por eso el mes va DENTRO del `doc_id`: al
    cambiar de mes la clave vieja desaparece (y las bajas de `#flujo` se
    ignoran) y la nueva nace en casi cero, en vez de aparecer un Δ negativo del
    tamaño del mes entero.
    """
    # ⚠ Son tres consultas a Asinfo por vuelta (bodegas 51/52/53) con un LAG
    # sobre `saldo_producto_lote`. Las cachea el service 5 minutos —la misma
    # cadencia de la traza, así que sale una por bodega y por ventana— pero si
    # alguna vez le pesan al ERP, `TRAZA_CAUDALES=0` las apaga sin tocar código
    # y el stock vuelve a informarse en un solo renglón.
    if os.environ.get("TRAZA_CAUDALES", "1").strip() == "0":
        return []
    hoy = hoy_ec()
    corte = date(hoy.year, hoy.month, 1)
    mes = f"{hoy.year:04d}-{hoy.month:02d}"
    try:
        from modules.asinfo import service as _asvc
    except Exception as e:  # noqa: BLE001 -- fail-soft
        _LOG.warning("foto: no pude importar asinfo (%s)", e)
        return []
    out: list[dict] = []
    for etapa, bodega in BODEGA_ETAPA.items():
        try:
            mov = _asvc.movimiento_bodega_mes(bodega, corte) or {}
        except Exception as e:  # noqa: BLE001 -- fail-soft
            _LOG.warning("foto: sin movimiento de bodega %s (%s)", bodega, e)
            continue
        for sentido in ("ingreso", "egreso"):
            out.append({
                "componente": FLUJO,
                "doc_id": f"{etapa}:{sentido}:{mes}",
                "etiqueta": CAUDALES.get((etapa, sentido), f"{etapa} {sentido}"),
                "importe": 0.0,
                "cantidad": round(_f(mov.get(sentido)), 2),
                "precio": None,
            })
    return out


def detalle(bal: dict) -> list[dict]:
    """La foto completa: componente + documento + importe.

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
            _LOG.warning("foto: detalle de %s falló (%s)", c, e)
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
    try:
        out.extend(_det_flujo())
    except Exception as e:  # noqa: BLE001 -- los caudales son opcionales
        _LOG.warning("foto: sin caudales de stock (%s)", e)
    out.append({"componente": META, "doc_id": "iniciada",
                "etiqueta": "La foto ya se sacó alguna vez",
                "importe": 0.0, "cantidad": None, "precio": None})
    return out

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


# ── Partir el Δ del stock: kilos vs tarifa, y por qué se movieron los kilos ──

def _nota_caudales(etapa: str, ent: float, sal: float) -> str:
    """Los caudales en castellano, para colgar de una etiqueta."""
    trozos = [
        f"{CAUDALES[(etapa, sentido)]} {_n(abs(kg), 0)} kg"
        for sentido, kg in (("ingreso", ent), ("egreso", sal))
        if abs(kg) >= 1 and (etapa, sentido) in CAUDALES
    ]
    return (" · " + " · ".join(trozos)) if trozos else ""


def _partir_kilos(etapa: str, d_kg: float, p0: float, d_valor: float,
                  rot: str, caudales: dict) -> list[dict]:
    """El Δ de KILOS de una etapa, abierto por caudal cuando se puede.

    TMT 2026-08-06: *"por qué sube y por qué baja"*. Una etapa sube porque le
    entraron kilos y baja porque se los llevaron; decir "el stock de tejido se
    movió +3.520" no contesta ninguna de las dos.

    Los caudales salen de una lectura de Asinfo distinta de la del saldo y con
    su propia caché de 5 minutos, así que en una ventana corta se desfasan.
    Cuando `ingreso − egreso` NO reconstruye el Δ físico dentro de la
    tolerancia, **no se parte**: sale un solo movimiento como antes y los
    caudales van de nota al pie. Partir con números que no cierran sería
    inventar una precisión que no tenemos.
    """
    ent = _f(caudales.get((etapa, "ingreso")))
    sal = _f(caudales.get((etapa, "egreso")))
    cierra = (abs(ent) >= UMBRAL or abs(sal) >= UMBRAL) and abs(
        (ent - sal) - d_kg) <= max(abs(d_kg) * TOLERANCIA_CAUDAL, UMBRAL)
    if not cierra:
        return [{
            "sub": f"{etapa}:kilos", "delta": d_valor, "regla": rot,
            "detalle": f"{_n(d_kg, 2, signo=True)} kg"
                       + _nota_caudales(etapa, ent, sal),
        }]
    out = []
    for sentido, kg in (("ingreso", ent), ("egreso", -sal)):
        if abs(kg) < UMBRAL:
            continue
        out.append({
            "sub": f"{etapa}:{sentido}", "delta": round(kg * p0, 2), "regla": rot,
            "detalle": f"{_n(kg, 0, signo=True)} kg · {CAUDALES[(etapa, sentido)]}",
        })
    return out


def _partir_stock(fila: dict, antes: dict, caudales: dict | None = None) -> list[dict]:
    """Parte el Δ de UNA etapa en kilos y tarifa.

        Δvalor = Δkg × precio_viejo  +  kg_nuevos × Δprecio

    Son dos noticias distintas: los kilos son producción real, la tarifa es una
    revaluación que mueve todo el stock de un saque sin producir ni vender.
    """
    kg0, kg1 = _f(antes.get("cantidad")), _f(fila.get("cantidad"))
    p0, p1 = _f(antes.get("precio")), _f(fila.get("precio"))
    if not (kg0 or kg1) or not (p0 or p1):
        return []
    d_kg = round((kg1 - kg0) * p0, 2)
    d_pr = round(kg1 * (p1 - p0), 2)
    rot = fila.get("etiqueta") or "Stock"
    etapa = (fila.get("doc_id") or "").lstrip("#")
    out = []
    if abs(d_kg) >= UMBRAL:
        out.extend(_partir_kilos(etapa, kg1 - kg0, p0, d_kg, rot, caudales or {}))
    # 🚨 Sólo se informa la tarifa si CAMBIÓ a la vista. Con 300.000 kg, una
    # diferencia en la quinta decimal del $/kg da varios dólares y generaba un
    # renglón que decía "cambió el $/kg: $ 5,2591 → $ 5,2591" — o sea, nada.
    if abs(d_pr) >= UMBRAL and round(p0, 4) != round(p1, 4):
        out.append({"sub": f"{etapa}:tarifa", "delta": d_pr, "regla": rot,
                    "detalle": f"$ {_n(p0, 4)} → $ {_n(p1, 4)}/kg"})
    return out


def _traspasos(est: dict) -> list[dict]:
    """Los kilos que pasaron de una etapa a la SIGUIENTE, como un solo hecho.

    🚨 TMT 2026-08-06, mirando cuatro renglones para explicar $ 36:
    *"ejemplo esto, +21kg de tejido a terminado. listo, todo el resto no
    entiendo para qué"*.

    Tenía razón. La tela que se termina sale de tejido y entra a terminado en
    el mismo instante; contarlo como "salieron 21 kg de tejido (−77,05)" más
    "entraron 21 kg a terminado (+113,86)" es la contabilidad hablando sola.
    Es UN movimiento, y lo que aporta a la utilidad es la diferencia de precio
    entre las dos etapas — el valor que le agregó el proceso.

    Muta `est`: descuenta del Δ de cada etapa los kilos ya explicados, así lo
    que queda después son entradas o salidas de verdad.
    """
    out = []
    for a_et, b_et in zip(ORDEN_ETAPAS, ORDEN_ETAPAS[1:], strict=False):
        a, b = est.get(a_et), est.get(b_et)
        if not a or not b:
            continue
        if a["dkg"] >= -UMBRAL or b["dkg"] <= UMBRAL:
            continue                       # tiene que bajar una y subir la otra
        kg = min(-a["dkg"], b["dkg"])
        if abs(a["dkg"] + b["dkg"]) > max(kg * TOLERANCIA_TRASPASO, UMBRAL):
            continue                       # no se corresponden: no es traspaso
        out.append({
            "sub": f"{a_et}-{b_et}",
            "delta": round(kg * (b["p0"] - a["p0"]), 2),
            "regla": "Stock",
            "detalle": f"{_n(kg, 0)} kg {a_et}→{b_et}",
        })
        a["dkg"] += kg
        b["dkg"] -= kg
    return out


def _mov_stock(etapas: dict, vieja: dict, caudales: dict) -> list[dict]:
    """Todo lo que se movió en el stock, en la menor cantidad de renglones.

    Primero los traspasos entre etapas (un renglón por cada uno), y recién
    después, con lo que sobra, las entradas y salidas de verdad y los cambios
    de tarifa. El redondeo se dobla en el renglón más grande: tiene que existir
    para que Δ = Σ aportes cierre, pero no merece una línea propia.
    """
    est, objetivo = {}, 0.0
    for et in ORDEN_ETAPAS:
        f = etapas.get(et)
        a = vieja.get(("vsto", f"#{et}")) if f else None
        if not f or not a:
            continue
        est[et] = {"fila": f, "antes": a, "p0": _f(a.get("precio")),
                   "dkg": _f(f.get("cantidad")) - _f(a.get("cantidad"))}
        objetivo += _f(f.get("importe")) - _f(a.get("importe"))
    if not est:
        return []
    objetivo = round(objetivo, 2)

    partes = _traspasos(est)
    for e in est.values():
        # Lo que quedó después de descontar el traspaso: kilos que entraron o
        # salieron de verdad, más la tarifa.
        fila = dict(e["fila"])
        fila["cantidad"] = _f(e["antes"].get("cantidad")) + e["dkg"]
        partes.extend(_partir_stock(fila, e["antes"], caudales))
    if not partes:
        return []
    resto = round(objetivo - sum(p["delta"] for p in partes), 2)
    if abs(resto) >= UMBRAL:
        mayor = max(partes, key=lambda p: abs(p["delta"]))
        mayor["delta"] = round(mayor["delta"] + resto, 2)
    return [{
        "componente": "vsto", "tipo": "cambio", "doc_id": f"#stock:{p['sub']}",
        "etiqueta": p["detalle"], "importe_antes": None, "importe_despues": None,
        "delta": p["delta"], "aporte": p["delta"],
        "regla": p["regla"], "familia": "utilidad",
    } for p in partes if abs(p["delta"]) >= UMBRAL]


# ── Diff: qué se movió entre dos fotos ──────────────────────────────────────

def es_primera(vieja: dict) -> bool:
    """¿Es la primera foto de la historia? Ver el comentario de `META`."""
    return (META, "iniciada") not in (vieja or {})


def guardada() -> dict[tuple[str, str], dict]:
    """La foto rodante que quedó de la vuelta anterior."""
    filas = _rows("SELECT componente, doc_id, etiqueta, importe, cantidad, precio "
                  "FROM scintela.traza_detalle")
    return {(r["componente"], r["doc_id"]): r for r in filas}


def _caudales(nueva: list[dict], vieja: dict) -> dict[tuple[str, str], float]:
    """Cuántos kilos entraron y salieron de cada etapa EN ESTA VENTANA.

    Las filas `#flujo` guardan el acumulado del mes, así que el caudal de la
    ventana es la diferencia entre las dos fotos. Una clave que no estaba en la
    foto vieja (el 1° de cada mes, cuando el acumulado se reinicia) arranca
    desde cero, que es justamente lo que corresponde.
    """
    out: dict[tuple[str, str], float] = {}
    for f in nueva:
        if f.get("componente") != FLUJO:
            continue
        partes = (f.get("doc_id") or "").split(":")
        if len(partes) < 2:
            continue
        antes = vieja.get((FLUJO, f["doc_id"]))
        d = round(_f(f.get("cantidad")) - _f((antes or {}).get("cantidad")), 2)
        out[(partes[0], partes[1])] = d
    return out


def diff(nueva: list[dict], vieja: dict) -> list[dict]:
    """Movimientos entre la foto vieja y la nueva.

    Un Δ de stock se parte en kilos y tarifa (y los kilos, en caudales), porque
    decir "el stock subió $80.000" no explica nada y decir "se tejieron
    12.000 kg" sí.
    """
    caudales = _caudales(nueva, vieja)
    etapas = {(f.get("doc_id") or "").lstrip("#"): f for f in nueva
              if f["componente"] == "vsto"
              and (f.get("doc_id") or "").lstrip("#") in ORDEN_ETAPAS}
    # El stock se resuelve entero y de una: un traspaso entre etapas toca DOS
    # filas de la foto y hay que verlas juntas para saber que es un solo hecho.
    movs: list[dict] = _mov_stock(etapas, vieja, caudales)
    vistas = set()
    for f in nueva:
        clave = (f["componente"], f["doc_id"])
        vistas.add(clave)
        if f["componente"] not in SIGNO:      # las filas #flujo no son plata
            continue
        if f["componente"] == "vsto" and (f.get("doc_id") or "").lstrip("#") in ORDEN_ETAPAS:
            continue                          # ya lo resolvió `_mov_stock`
        antes = vieja.get(clave)
        imp1 = _f(f.get("importe"))
        imp0 = _f(antes.get("importe")) if antes else 0.0
        d = round(imp1 - imp0, 2)
        if abs(d) < UMBRAL:
            continue
        tipo = "cambio" if antes else "alta"
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
        comp = clave[0]
        if comp not in SIGNO:                 # #flujo: el cambio de mes no es una baja
            continue
        imp0 = _f(a.get("importe"))
        if abs(imp0) < UMBRAL:
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


# ── Escribir la foto ────────────────────────────────────────────────────────

#: Filas por sentencia. La cartera viva son miles de documentos: una fila por
#: INSERT dejaría la transacción abierta demasiado tiempo, y esto corre en el
#: hilo de fondo del server de producción.
LOTE = 500

_COLS_FOTO = ("componente", "doc_id", "etiqueta", "importe", "cantidad", "precio")


def _en_lote(conn, tabla: str, cols: tuple[str, ...], filas: list[dict],
             sufijo: str = "") -> int:
    """INSERT multi-fila por lotes. Devuelve cuántas filas metió."""
    n = 0
    campos = ", ".join(cols)
    for i in range(0, len(filas), LOTE):
        trozo = filas[i:i + LOTE]
        marcas = ", ".join(
            "(" + ", ".join(f"%({c}_{j})s" for c in cols) + ")"
            for j in range(len(trozo)))
        params = {f"{c}_{j}": f.get(c) for j, f in enumerate(trozo) for c in cols}
        db.execute(f"INSERT INTO {tabla} ({campos}) VALUES {marcas} {sufijo}",
                   params, conn=conn)
        n += len(trozo)
    return n


def _cambio(a: dict, f: dict) -> bool:
    return (_f(a.get("importe")) != _f(f.get("importe"))
            or _f(a.get("cantidad")) != _f(f.get("cantidad"))
            or _f(a.get("precio")) != _f(f.get("precio"))
            or (a.get("etiqueta") or "") != (f.get("etiqueta") or ""))


def aplicar(conn, nueva: list[dict], vieja: dict) -> dict:
    """Deja `traza_detalle` igual a `nueva`, tocando SÓLO lo que cambió.

    🚨 La versión de dos capturas diarias borraba la foto entera y la
    reescribía. A 288 vueltas por día eso son ~2 millones de filas escritas
    para mover, en una vuelta típica, menos de cincuenta: el autovacuum no
    llega y la tabla se infla. Acá se hace por diferencia.

    ⚠ Se actualiza ante CUALQUIER cambio, incluso menor al umbral que hace
    falta para generar un movimiento. Si no, un documento que se corre medio
    centavo por vuelta nunca alcanzaría el umbral y la foto se iría quedando
    atrás en silencio.
    """
    idx = {(f["componente"], f["doc_id"]): f for f in nueva}
    nuevas = [f for k, f in idx.items() if k not in vieja]
    cambiadas = [f for k, f in idx.items()
                 if k in vieja and _cambio(vieja[k], f)]
    bajas = [k for k in vieja if k not in idx]

    escritas = nuevas + cambiadas
    if escritas:
        _en_lote(conn, "scintela.traza_detalle", _COLS_FOTO, escritas,
                 sufijo="ON CONFLICT (componente, doc_id) DO UPDATE SET "
                        "etiqueta = EXCLUDED.etiqueta, "
                        "importe = EXCLUDED.importe, "
                        "cantidad = EXCLUDED.cantidad, "
                        "precio = EXCLUDED.precio")
    for i in range(0, len(bajas), LOTE):
        trozo = bajas[i:i + LOTE]
        db.execute(
            "DELETE FROM scintela.traza_detalle WHERE (componente, doc_id) IN %s",
            (tuple(trozo),), conn=conn)
    return {"altas": len(nuevas), "cambios": len(cambiadas), "bajas": len(bajas)}


#: Las columnas con las que se graba un movimiento.
COLS_MOV = ("id_traza", "componente", "tipo", "doc_id", "etiqueta",
            "importe_antes", "importe_despues", "delta", "aporte",
            "regla", "familia")


def guardar_movimientos(conn, id_traza: int, movs: list[dict]) -> int:
    """Graba el diff colgado de la foto que lo produjo."""
    if not movs:
        return 0
    for m in movs:
        m["id_traza"] = id_traza
    return _en_lote(conn, "scintela.dia_movimiento", COLS_MOV, movs)
