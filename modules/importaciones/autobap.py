"""Conversión AUTOMÁTICA anticipo USD → compra BAP al recibir la importación.

TMT 2026-07-29 (dueña): *"cuando recibo una importación, en anticipos se marca
recibido y el usuario la carga a compras. ¿Podemos hacer que sea automático?"*

De los tres pasos del circuito, dos ya eran automáticos:
  1. el anticipo se carga (a mano, y así queda);
  2. **la recepción la marca Asinfo sola** (`recepcion_proveedor.fecha`);
  3. alguien tilda los anticipos y aprieta "Convertir a compra"  ← ESTE.

Este módulo hace el 3. Reusa `dolares.queries.convertir_a_compra()` SIN tocarla:
mismo asiento que el botón, mismo `mov_doble bap_anticipo_a_compra`, mismo
reverso desde /historial. Lo único distinto es `usuario_crea='bap-auto'`.

REGLA CLAVE (dueña, 29/07): NO es "convertir en el momento de recibir" sino
**"convertir todo anticipo cuya importación ya esté recibida"**, corriendo
siempre — los CAE/SALDO/MAPFRE/flete llegan hasta 6 meses DESPUÉS de la
recepción (la AC 77 se recibió el 22/12/25 y su anticipo se cargó el 18/06/26).
El tardío se convierte solo también, con su propia compra BAP contra la misma
importación (el programa ya soporta varias compras por importación).

DESFASE CON EL dBASE: la conversión mueve el monto de ANTICIPOS a COMPRAS sólo
en PC. La dueña lo aceptó explícitamente ("convierte solo, entiendo que va a
diferir del dBase") y la campanita deja el recordatorio de qué BAP hacer allá.
"""
from __future__ import annotations

import logging
import os
import threading
import time as _time

import db
from filters import today_ec

_LOG = logging.getLogger("programa_core.autobap")

USUARIO_AUTOBAP = "bap-auto"

_LOCK = threading.Lock()
_ultimo_ts = 0.0
_INTERVALO_MIN = 1800.0  # 30 min entre corridas (freno propio dentro del ciclo)

# Topes por corrida. NO gatean la operación normal: están para que un dato raro
# de Asinfo no dispare una conversión masiva. Dimensionados sobre lo real (una
# importación entera ronda los $70-100k). Editables desde la pantalla.
_TOPE_IM_DEFAULT = 10
_TOPE_USD_DEFAULT = 500_000.0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def config() -> dict:
    """Config viva. Fail-soft: si la tabla no existe todavía, apagado."""
    try:
        row = db.fetch_one(
            "SELECT activo, TO_CHAR(fecha_corte,'YYYY-MM-DD') AS fecha_corte, "
            "       tope_importaciones, tope_usd "
            "  FROM scintela.autobap_config WHERE id = 1"
        )
    except Exception as e:  # noqa: BLE001
        _LOG.warning("autobap_config no disponible: %s", e)
        return {"activo": False, "fecha_corte": None,
                "tope_importaciones": _TOPE_IM_DEFAULT,
                "tope_usd": _TOPE_USD_DEFAULT, "disponible": False}
    if not row:
        return {"activo": False, "fecha_corte": None,
                "tope_importaciones": _TOPE_IM_DEFAULT,
                "tope_usd": _TOPE_USD_DEFAULT, "disponible": True}
    return {
        "activo": bool(row["activo"]),
        "fecha_corte": row["fecha_corte"],
        "tope_importaciones": int(row["tope_importaciones"] or _TOPE_IM_DEFAULT),
        "tope_usd": float(row["tope_usd"] or _TOPE_USD_DEFAULT),
        "disponible": True,
    }


def set_topes(*, tope_importaciones, tope_usd, usuario: str = "web") -> dict:
    """Cambia los topes desde la pantalla (sin deploy). TMT 2026-07-29: el
    primer valor (5 / US$60.000) frenaba SIEMPRE — una sola importación ronda
    los $70-100k. El tope está para atajar un dato raro de Asinfo, no para
    gatear la operación normal."""
    n = int(tope_importaciones)
    u = float(tope_usd)
    if n < 1 or n > 500:
        raise ValueError("El tope de importaciones tiene que estar entre 1 y 500.")
    if u <= 0 or u > 50_000_000:
        raise ValueError("El tope en US$ tiene que ser positivo y razonable.")
    db.execute(
        "UPDATE scintela.autobap_config "
        "   SET tope_importaciones = %s, tope_usd = %s, "
        "       usuario_modifica = %s, fecha_modifica = now() "
        " WHERE id = 1",
        (n, u, (usuario or "web")[:50]),
    )
    return config()


def set_activo(activo: bool, usuario: str = "web") -> dict:
    """Prende o apaga. Al PRENDER sella la fecha de corte con HOY si no había:
    el primer barrido no dispara el histórico (guard 4)."""
    hoy = today_ec()
    if activo:
        db.execute(
            "UPDATE scintela.autobap_config "
            "   SET activo = TRUE, fecha_corte = COALESCE(fecha_corte, %s), "
            "       usuario_modifica = %s, fecha_modifica = now() "
            " WHERE id = 1",
            (hoy, (usuario or "web")[:50]),
        )
    else:
        db.execute(
            "UPDATE scintela.autobap_config "
            "   SET activo = FALSE, usuario_modifica = %s, fecha_modifica = now() "
            " WHERE id = 1",
            ((usuario or "web")[:50],),
        )
    return config()


# ---------------------------------------------------------------------------
# Avisos (la campanita)
# ---------------------------------------------------------------------------

def avisar(**kw) -> None:
    """Escribe una fila en el log. Nunca levanta."""
    try:
        db.execute(
            """
            INSERT INTO scintela.autobap_log
                (tipo, im_numero, codigo_prov, ref_num, anio, fecha_recepcion,
                 kg, n_anticipos, importe, id_compra, comprobante, mensaje)
            VALUES (%(tipo)s, %(im_numero)s, %(codigo_prov)s, %(ref_num)s,
                    %(anio)s, %(fecha_recepcion)s, %(kg)s, %(n_anticipos)s,
                    %(importe)s, %(id_compra)s, %(comprobante)s, %(mensaje)s)
            """,
            {
                "tipo": kw.get("tipo", "conversion")[:20],
                "im_numero": (kw.get("im_numero") or None),
                "codigo_prov": (kw.get("codigo_prov") or None),
                "ref_num": kw.get("ref_num"),
                "anio": kw.get("anio"),
                "fecha_recepcion": kw.get("fecha_recepcion") or None,
                "kg": kw.get("kg"),
                "n_anticipos": kw.get("n_anticipos"),
                "importe": kw.get("importe"),
                "id_compra": kw.get("id_compra"),
                "comprobante": (kw.get("comprobante") or None),
                "mensaje": (kw.get("mensaje") or "")[:400],
            },
        )
    except Exception as e:  # noqa: BLE001
        _LOG.warning("no pude escribir el aviso: %s", e)


def avisos(solo_no_leidos: bool = True, limite: int = 30) -> list[dict]:
    try:
        return db.fetch_all(
            f"""
            SELECT id_autobap_log, tipo, im_numero, codigo_prov, ref_num, anio,
                   TO_CHAR(fecha_recepcion,'YYYY-MM-DD') AS fecha_recepcion,
                   kg, n_anticipos, importe, id_compra, comprobante, mensaje,
                   leido, TO_CHAR(creado_en,'YYYY-MM-DD HH24:MI') AS creado_en
              FROM scintela.autobap_log
             {"WHERE NOT leido" if solo_no_leidos else ""}
             ORDER BY creado_en DESC, id_autobap_log DESC
             LIMIT %s
            """,
            (int(limite),),
        ) or []
    except Exception:  # noqa: BLE001
        return []


def marcar_leidos(usuario: str = "web") -> int:
    try:
        db.execute("UPDATE scintela.autobap_log SET leido = TRUE WHERE NOT leido")
        return 1
    except Exception:  # noqa: BLE001
        return 0


def n_no_leidos() -> int:
    try:
        row = db.fetch_one(
            "SELECT COUNT(*) AS n FROM scintela.autobap_log WHERE NOT leido")
        return int((row or {}).get("n") or 0)
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
# El motor
# ---------------------------------------------------------------------------

def pendientes(cfg: dict | None = None) -> dict:
    """DRY-RUN: qué convertiría ahora. No escribe nada.

    Devuelve {grupos, total_usd, n_importaciones, frenos, disponible}.
    Cada grupo: {im_numero, codigo_prov, ref, anio, fecha_recepcion, kg,
                 ids, n, importe, motivo_freno}.
    """
    from modules.dolares import anio as anio_mod
    from modules.dolares import queries as dol_q

    from . import service as imp_service

    cfg = cfg or config()
    out = {"grupos": [], "total_usd": 0.0, "n_importaciones": 0,
           "frenos": [], "disponible": True}

    # ── GUARD 1: Asinfo mudo → no se toca nada ──────────────────────────────
    try:
        index = imp_service._index_importaciones_por_codigo()
    except Exception as e:  # noqa: BLE001
        out["disponible"] = False
        out["frenos"].append(f"Asinfo no contesta ({e}) — no se convierte nada.")
        return out
    if not index:
        out["disponible"] = False
        out["frenos"].append("Asinfo no devolvió importaciones — no se convierte nada.")
        return out

    # ── GUARD 2: sólo proveedores de HILADO (tipo H) ────────────────────────
    # 'U' (maquinaria) va por «Activar máquina»; 'Q' (químicos) no pasa por
    # importaciones. Es el mismo rechazo que ya hace el botón manual.
    try:
        filas = dol_q.anticipos_vivos(tipos_filter=["H"])
    except Exception as e:  # noqa: BLE001
        out["disponible"] = False
        out["frenos"].append(f"No pude leer los anticipos ({e}).")
        return out
    if not filas:
        return out

    # Año explícito (columna) + recepción de Asinfo, igual que /dolares.
    try:
        anio_mod.adjuntar_anio(filas)
    except Exception:  # noqa: BLE001
        pass
    try:
        imp_service.adjuntar_recepcion_asinfo(filas)
    except Exception as e:  # noqa: BLE001
        out["disponible"] = False
        out["frenos"].append(f"No pude cruzar con Asinfo ({e}).")
        return out

    corte = cfg.get("fecha_corte")
    grupos: dict[tuple, dict] = {}
    ambiguos: set[tuple] = set()

    for f in filas:
        im = f.get("im_numero")
        rec = f.get("fecha_recepcion_im")
        cta = (f.get("cta") or "").strip().upper()
        ref = f.get("ref")
        # ── GUARD 3: sólo con recepción REAL en Asinfo ──────────────────────
        if not im or not rec or not cta or ref is None:
            continue
        # ── GUARD 4: fecha de corte — no dispara el histórico ───────────────
        # La fecha de corte se sella el día que se prende y es INCLUSIVA: las
        # recepciones de ESE día entran (si prendés un lunes, lo que llegó el
        # lunes se convierte), lo anterior queda como está.
        if corte and str(rec)[:10] < str(corte)[:10]:
            continue
        # ── GUARD 9 (nuevo, 29/07): código repetido entre campañas y el
        # anticipo SIN año escrito → no se convierte solo. Es justo el caso
        # que mandaba el AC 58 contra la importación equivocada.
        if f.get("anio_ref") is None and len(index.get((cta, int(ref))) or []) > 1:
            ambiguos.add((cta, int(ref)))
            continue
        g = grupos.setdefault((cta, im), {
            "im_numero": im, "codigo_prov": cta, "ref": int(ref),
            "anio": f.get("anio_ref"), "fecha_recepcion": rec,
            "kg": f.get("kg_im"), "ids": [], "n": 0, "importe": 0.0,
            "conceptos": [],
        })
        g["ids"].append(int(f["id_dolares"]))
        g["n"] += 1
        g["importe"] += float(f.get("importe") or 0)
        g["conceptos"].append((f.get("concepto") or "").strip())

    for k in ambiguos:
        out["frenos"].append(
            f"{k[0]} {k[1]}: el número se repite entre campañas y el anticipo no "
            f"tiene el año puesto — poné el año en /dolares y se convierte solo."
        )

    lista = sorted(grupos.values(), key=lambda g: (g["fecha_recepcion"], g["im_numero"]))
    for g in lista:
        g["importe"] = round(g["importe"], 2)
    out["grupos"] = lista
    out["n_importaciones"] = len(lista)
    out["total_usd"] = round(sum(g["importe"] for g in lista), 2)
    return out


def correr(*, dry_run: bool = False, usuario: str = USUARIO_AUTOBAP,
           forzar_topes: bool = False) -> dict:
    """Convierte lo pendiente. Devuelve {convertidas, importe, frenos, grupos}.

    Nunca levanta: cualquier error se loguea y se avisa por la campanita.
    """
    from modules.dolares import queries as dol_q
    from periodo_guard import asegurar_fecha_abierta

    cfg = config()
    res = {"convertidas": 0, "importe": 0.0, "frenos": [], "grupos": [],
           "corrio": True}
    p = pendientes(cfg)
    res["grupos"] = p["grupos"]
    res["frenos"] = list(p["frenos"])
    if not p["disponible"] or not p["grupos"]:
        return res

    # ── GUARD 5: topes por corrida ──────────────────────────────────────────
    if not forzar_topes and (
            p["n_importaciones"] > cfg["tope_importaciones"]
            or p["total_usd"] > cfg["tope_usd"]):
        msg = (f"FRENADO: {p['n_importaciones']} importaciones por "
               f"$ {p['total_usd']:,.2f} superan el tope "
               f"({cfg['tope_importaciones']} / $ {cfg['tope_usd']:,.2f}). "
               f"Revisalo en Ingreso de hilado → Automático.")
        res["frenos"].append(msg)
        avisar(tipo="freno", mensaje=msg, importe=p["total_usd"],
               n_anticipos=p["n_importaciones"])
        return res

    if dry_run:
        return res

    hoy = today_ec()
    for g in p["grupos"]:
        try:
            # ── GUARD 6: período contable cerrado ───────────────────────────
            asegurar_fecha_abierta(hoy)
            r = dol_q.convertir_a_compra(
                codigo_prov=g["codigo_prov"],
                ids_anticipos=g["ids"],
                fecha=hoy,
                # El concepto de la compra va con el NÚMERO SOLO: el cruce de
                # compras junta todos los dígitos ("58/26" sería 5826).
                concepto=str(g["ref"]),
                tipo_compra="H",
                kg=None,
                motivo="Conversión automática al recibir la importación",
                usuario=usuario,
            )
            res["convertidas"] += 1
            res["importe"] += float(r.get("importe_total") or 0)
            avisar(
                tipo="conversion", im_numero=g["im_numero"],
                codigo_prov=g["codigo_prov"], ref_num=g["ref"], anio=g.get("anio"),
                fecha_recepcion=g["fecha_recepcion"], kg=g.get("kg"),
                n_anticipos=g["n"], importe=r.get("importe_total"),
                id_compra=r.get("id_compra"), comprobante=r.get("comprobante"),
                mensaje=(
                    f"{g['im_numero']} de {g['codigo_prov']} recibida el "
                    f"{str(g['fecha_recepcion'])[8:10]}/{str(g['fecha_recepcion'])[5:7]}"
                    f" — convertí {g['n']} anticipo(s) por "
                    f"$ {float(r.get('importe_total') or 0):,.2f} a la compra "
                    f"{r.get('comprobante')}. Acordate del BAP en el dBase: "
                    f"{g['codigo_prov']} {g['ref']}."
                ),
            )
        except Exception as e:  # noqa: BLE001 -- una importación no frena al resto
            _LOG.warning("autobap %s falló: %s", g.get("im_numero"), e)
            msg = f"{g.get('im_numero')} de {g.get('codigo_prov')}: no pude convertir ({e})."
            res["frenos"].append(msg)
            avisar(tipo="error", im_numero=g.get("im_numero"),
                   codigo_prov=g.get("codigo_prov"), ref_num=g.get("ref"),
                   mensaje=msg)
    res["importe"] = round(res["importe"], 2)
    return res


def correr_si_toca() -> dict:
    """Entrada del hilo de fondo: respeta el switch, el env y el freno de 30
    minutos. Nunca levanta."""
    global _ultimo_ts
    res = {"convertidas": 0, "importe": 0.0, "corrio": False}
    if os.environ.get("AUTOBAP", "1").strip() == "0":
        return res
    ahora = _time.monotonic()
    if ahora - _ultimo_ts < _INTERVALO_MIN:
        return res
    if not _LOCK.acquire(blocking=False):
        return res
    try:
        _ultimo_ts = _time.monotonic()
        # ── GUARD 7: el switch ──────────────────────────────────────────────
        if not config().get("activo"):
            return res
        r = correr()
        r["corrio"] = True
        return r
    except Exception as e:  # noqa: BLE001
        _LOG.warning("autobap ciclo falló: %s", e)
        return res
    finally:
        try:
            _LOCK.release()
        except RuntimeError:
            pass
