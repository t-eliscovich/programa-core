"""El catálogo de frases: el HECHO con nombre, no la firma por componentes.

TMT 2026-08-07. La traza venía **re-deduciendo desde los saldos** algo que la
app ya escribe con nombre y apellido en `scintela.mov_doble`. De ahí salían
reglas-adivinanza como *"Cheque depositado o dado de baja"* — que no sabe cuál
de las dos fue, y en el caso que miró la dueña no era ninguna: era un cheque
cancelado por un anticipo.

Medido el 07/08 sobre producción: **50 tipos, 18.896 hechos, y los 12 primeros
cubren el 95,4%**. O sea que esto no es entrenar mil ventanas: son doce frases.

Dos capas, y el orden importa:

  1. **El hecho con nombre** (`mov_doble.tipo`) — esto. Trae el importe, el
     concepto y la metadata (cliente, cheque, factura, banco).
  2. **La firma por componentes** (`foto.regla`) — SÓLO como fallback, para las
     ventanas sin ningún hecho registrado: revaluación del hilado, amortización,
     cambio de fuente. Son pocas y son las caras.

🚨 **La frase nombra el hecho, no lo interpreta.** Dueña, 07/08: *"todos los
acorté, está muy wordy"*. El Δ ya está en la columna de al lado; agregarle "la
utilidad no cambia" a cada renglón es ruido. Determinista y sin modelo: cada
frase es una plantilla llena con datos de la base.
"""
from __future__ import annotations

import logging

import db

_LOG = logging.getLogger(__name__)


def _n(v) -> str:
    """Importe en formato Ecuador: 2.006,02."""
    try:
        s = f"{abs(float(v)):,.2f}"
    except (TypeError, ValueError):
        return "0,00"
    return s.replace(",", "·").replace(".", ",").replace("·", ".")


def _md(h: dict) -> dict:
    m = h.get("metadata")
    return m if isinstance(m, dict) else {}


# ── Las frases ────────────────────────────────────────────────────────────
# Cada una recibe la fila de mov_doble y devuelve UN renglón. Corto: el
# número ya se ve en la columna.

def _factura_emitida(h):
    m = _md(h)
    return f"F#{m.get('numf') or h.get('destino_id')} de {m.get('codigo_cli') or ''} por $ {_n(h.get('importe'))}".replace("  ", " ")


def _cheque_aplicado(h):
    m = _md(h)
    saldo = m.get("saldo_factura_post")
    numf = m.get("numf") or m.get("id_factura")
    verbo = "totalizó" if (saldo is not None and abs(float(saldo)) < 0.01) else "abonó"
    txt = f"Cheque {m.get('id_cheque') or h.get('origen_id')} {verbo} F#{numf} de $ {_n(h.get('importe'))}"
    # 🚨 Sólo si NO totalizó se dice el saldo. Si quedó en cero, decir "saldo
    # $0" es una palabra de más (dueña, 07/08).
    if verbo == "abonó" and saldo is not None:
        txt += f", saldo $ {_n(saldo)}"
    return txt


def _cheque_creado(h):
    m = _md(h)
    # 🚨 Un cheque que NO es anticipo entra SIEMPRE aplicado a una factura —
    # medido: los 6 últimos tienen exactamente 1 aplicación cada uno. Emitir
    # también "cheque recibido" duplica el renglón, así que habla la
    # aplicación y este se calla.
    if not m.get("es_anticipo"):
        return None
    return f"Cheque de {m.get('codigo_cli') or ''} por $ {_n(h.get('importe'))} (anticipo)".replace("  ", " ")


def _cheque_depositado(h):
    m = _md(h)
    banco = m.get("banco_nombre") or ""
    return f"Se depositó 1 cheque por $ {_n(h.get('importe'))}" + (f" en {banco}" if banco else "")


def _retencion(h):
    m = _md(h)
    return f"Retención de $ {_n(h.get('importe'))} sobre F#{m.get('numf') or m.get('id_factura') or ''}".strip()


def _devolucion(h):
    m = _md(h)
    return f"Devolución F#{m.get('numf') or ''} de {m.get('codigo_cli') or ''} por $ {_n(h.get('importe'))}".replace("  ", " ")


def _compra_a_posdat(h):
    m = _md(h)
    return f"Compra {m.get('numero_compra') or ''} a {m.get('codigo_prov') or ''} por $ {_n(h.get('importe'))}".replace("  ", " ")


def _cheque_efectivo(h):
    m = _md(h)
    return f"Cheque de {m.get('codigo_cli') or ''} por $ {_n(h.get('importe'))} cobrado en efectivo".replace("  ", " ")


def _caja_gasto(h):
    return f"{(h.get('concepto') or 'Gasto').strip()} $ {_n(h.get('importe'))} por caja"


def _compra_backfill(h):
    m = _md(h)
    return f"Carga histórica compra {m.get('numero_compra') or ''} {m.get('codigo_prov') or ''}".strip()


def _gasto_a_posdat(h):
    c = (h.get("concepto") or "").replace("[backfill] ", "").strip()
    return f"{c or 'Gasto'} por $ {_n(h.get('importe'))}"


def _cheque_emitido_gasto(h):
    m = _md(h)
    ben = (h.get("concepto") or "").strip()
    n = m.get("no_cheque") or ""
    return f"Cheque {n} por $ {_n(h.get('importe'))} {ben}".replace("  ", " ").strip()


def _cancelado_por_anticipo(h):
    m = _md(h)
    return (f"Anticipo de {m.get('codigo_cli') or ''} por $ {_n(h.get('importe'))} "
            f"canceló el cheque {m.get('id_cheque') or ''}").replace("  ", " ")


#: tipo de mov_doble → la frase. Los 12 primeros cubren el 95,4% de los hechos.
FRASES = {
    "factura_emitida": _factura_emitida,
    "cheque_aplicado_a_factura": _cheque_aplicado,
    "cheque_creado": _cheque_creado,
    "cheque_depositado": _cheque_depositado,
    "retencion_asinfo_aplicada": _retencion,
    "factura_devolucion": _devolucion,
    "compra_a_posdat": _compra_a_posdat,
    "cheque_efectivo_to_caja": _cheque_efectivo,
    "caja_s_to_gasto": _caja_gasto,
    "compra_backfill": _compra_backfill,
    "gasto_a_posdat": _gasto_a_posdat,
    "cheque_emitido_gasto": _cheque_emitido_gasto,
    "cheque_cancelado_por_anticipo": _cancelado_por_anticipo,
}

#: De qué tabla de mov_doble sale el `doc_id` que usa la foto (ver
#: `foto.py`: f{factura} c{cheque} k{caja} b{no_banco} p{posdat}).
_PREFIJO = {"factura": "f", "cheque": "c", "caja": "k", "posdat": "p"}


def _doc_ids(h: dict) -> list[str]:
    """Los doc_id de la foto que este hecho puede explicar."""
    out = []
    for tabla, id_ in ((h.get("origen_table"), h.get("origen_id")),
                       (h.get("destino_table"), h.get("destino_id"))):
        pre = _PREFIJO.get(tabla or "")
        if pre and id_:
            out.append(f"{pre}{id_}")
    nb = _md(h).get("no_banco")
    toca_banco = "transacciones_bancarias" in (h.get("origen_table"), h.get("destino_table"))
    if toca_banco and nb is not None:
        out.append(f"b{nb}")
    return out


def hechos(desde, hasta) -> dict[str, str]:
    """{doc_id: frase} de los hechos con nombre de esa ventana.

    Fail-soft: si la consulta falla se devuelve vacío y la traza sigue
    mostrando lo de siempre (la firma por componentes).
    """
    if not hasta:
        return {}
    try:
        filas = db.fetch_all(
            """
            SELECT tipo, importe, concepto, origen_table, origen_id,
                   destino_table, destino_id, metadata
              FROM scintela.mov_doble
             WHERE estado = 'activo'
               AND fecha_creacion > %s AND fecha_creacion <= %s
             ORDER BY id_mov_doble
            """, (desde, hasta)) or []
    except Exception as e:  # noqa: BLE001 -- nunca romper la traza
        _LOG.warning("catalogo: no se pudieron leer los hechos (%s)", e)
        return {}

    # 🚨 Los depósitos van AGRUPADOS: 22 cheques depositados juntos son UN
    # renglón, no 22 (dueña, 07/08: *"tiene que ser, se depositaron X cheques
    # por X monto"*). El grupo lo trae la propia metadata.
    dep: dict[tuple, dict] = {}
    for h in filas:
        if h.get("tipo") != "cheque_depositado":
            continue
        m = _md(h)
        k = (m.get("n_grupo"), m.get("no_banco"))
        g = dep.setdefault(k, {"n": 0, "us": 0.0, "banco": m.get("banco_nombre") or ""})
        g["n"] += 1
        g["us"] += float(h.get("importe") or 0)

    out: dict[str, str] = {}
    for h in filas:
        tipo = h.get("tipo")
        arma = FRASES.get(tipo)
        if not arma:
            continue
        try:
            if tipo == "cheque_depositado":
                m = _md(h)
                g = dep.get((m.get("n_grupo"), m.get("no_banco")))
                if g and g["n"] > 1:
                    txt = (f"Se depositaron {g['n']} cheques por $ {_n(g['us'])}"
                           + (f" en {g['banco']}" if g["banco"] else ""))
                else:
                    txt = arma(h)
            else:
                txt = arma(h)
        except Exception:  # noqa: BLE001 -- una frase rota no tira la pantalla
            continue
        if not txt:
            continue
        for doc in _doc_ids(h):
            # El primero que llega manda: dentro de una ventana un mismo
            # documento puede tener dos hechos (alta y aplicación), y el que
            # importa es el que explica el movimiento, no el que lo creó.
            out.setdefault(doc, txt)
    return out


def aplicar(movs: list[dict], desde, hasta) -> list[dict]:
    """Le pone a cada movimiento la frase del catálogo, si existe.

    Deja `etiqueta` intacta como respaldo en `etiqueta_firma`: si mañana hay
    que comparar las dos capas, el dato está.
    """
    mapa = hechos(desde, hasta)
    if not mapa:
        return movs
    vistos: set[str] = set()
    for m in movs:
        doc = m.get("doc_id") or ""
        txt = mapa.get(doc)
        if not txt or txt in vistos:
            continue
        vistos.add(txt)
        m["etiqueta_firma"] = m.get("etiqueta")
        m["etiqueta"] = txt
        m["del_catalogo"] = True
    return movs
