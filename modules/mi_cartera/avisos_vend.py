"""La campanita del vendedor y los avisos de problemas de la app.

Tamara 13/10/2026: *"me gustaría una campanita y avisar al vendedor si algún
cliente pasa a rojo"* y *"algo para que puedan quejarse de algo de la app"*
(le llega a ella, a nadie más).

Tres piezas:

1. **La foto del semáforo** (`foto_semaforo`). El color de cobranza de cada
   cliente se calculaba en el momento (`analisis.cobranza.colores`) y no
   quedaba nada: sin la foto de ayer no hay forma de saber quién PASÓ a rojo.
   La graba la rutina diaria (health/all) y, si un día no corrió, la primera
   pantalla del portal que la pide (en un hilo, sin hacer esperar a nadie).
   ⭐ "Pasó a rojo" = hoy rojo (o incobrable: para el vendedor es rojo, como
   en su ficha) y en la foto ANTERIOR no. El primer día no hay foto anterior:
   sólo se graba, para no mandarle de golpe todos los rojos que ya existían.

2. **La campanita** (`lista`, `n_nuevos`, `marcar_vistos`): los avisos del
   vendedor (`scintela.vendedor_aviso`) + las sugerencias de Competencia del
   día, que ya tenían su propia cuenta (`sugerencias.pendientes`).

3. **Avisar un problema** (`guardar_reporte`, `responder`): el reporte va a
   `scintela.reporte_app` y deja un aviso en la campanita de la oficina que
   lleva a `/admin/reportes-app/<id>` — una pantalla con permiso
   `app.reportes`, que no tiene ningún rol: la ve sólo quien tiene `*`.

Todo fail-soft: la campanita es un aviso, nunca un freno para la pantalla.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, date, datetime, timedelta, timezone

import db
from filters import today_ec

_LOG = logging.getLogger("programa_core.mi_cartera.avisos")

ROJOS = ("rojo", "inc")
DIAS_LISTA = 30
FOTO_MAX_BYTES = 6 * 1024 * 1024

_EC = timezone(timedelta(hours=-5))


# ── 1. La foto del semáforo y "pasó a rojo" ──────────────────────────────────

def foto_semaforo(hoy: date | None = None, colores: dict | None = None) -> dict:
    """Graba (o pisa) la foto de hoy y avisa a cada vendedor los clientes que
    pasaron a rojo. Idempotente: puede correr varias veces por día — la clave
    del aviso es (cliente, día)."""
    hoy = hoy or today_ec()
    if colores is None:
        from modules.analisis import cobranza
        colores = cobranza.colores(None if hoy == today_ec() else hoy)
    cods = list(colores)
    if cods:
        db.execute(
            """
            INSERT INTO scintela.semaforo_foto (fecha, codigo_cli, color, motivos)
            SELECT %s, c, k, m
              FROM unnest(%s::text[], %s::text[], %s::text[]) AS t(c, k, m)
            ON CONFLICT (fecha, codigo_cli) DO UPDATE
               SET color = EXCLUDED.color, motivos = EXCLUDED.motivos,
                   creado_en = now()
            """,
            (hoy, cods, [colores[c]["color"] for c in cods],
             [" · ".join(colores[c].get("motivos") or []) for c in cods]),
        )
    ant = db.fetch_one(
        "SELECT MAX(fecha) AS f FROM scintela.semaforo_foto WHERE fecha < %s", (hoy,))
    ant_fecha = ant and ant.get("f")
    if not ant_fecha:
        return {"fecha": str(hoy), "clientes": len(cods), "pasaron": 0,
                "nota": "primera foto: no hay contra qué comparar"}
    antes = {r["codigo_cli"]: r["color"] for r in db.fetch_all(
        "SELECT codigo_cli, color FROM scintela.semaforo_foto WHERE fecha = %s",
        (ant_fecha,))}
    pasaron = [c for c in cods
               if colores[c]["color"] in ROJOS and antes.get(c) not in ROJOS]
    n = _avisar_rojos(pasaron, colores, hoy) if pasaron else 0
    return {"fecha": str(hoy), "clientes": len(cods), "contra": str(ant_fecha),
            "pasaron": len(pasaron), "avisos": n}


def _avisar_rojos(cods: list[str], colores: dict, hoy: date) -> int:
    fichas = db.fetch_all(
        """
        SELECT UPPER(TRIM(c.codigo_cli)) AS cod, TRIM(c.nombre) AS nombre,
               UPPER(TRIM(c.vend)) AS vend
          FROM scintela.cliente c
          JOIN scintela.vendedor v ON v.codigo = UPPER(TRIM(c.vend)) AND v.activo
         WHERE UPPER(TRIM(c.codigo_cli)) = ANY(%s)
        """, (cods,))
    n = 0
    for f in fichas:
        motivos = colores.get(f["cod"], {}).get("motivos") or []
        n += db.execute(
            """
            INSERT INTO scintela.vendedor_aviso (vend, tipo, titulo, detalle, url, clave)
            VALUES (%s, 'rojo', %s, %s, %s, %s)
            ON CONFLICT (clave) DO NOTHING
            """,
            (f["vend"], f"{f['cod']} · {(f['nombre'] or '').title()} pasó a rojo"[:200],
             motivos[0] if motivos else None,
             f"/mi-cartera/cliente/{f['cod']}", f"rojo:{f['cod']}:{hoy.isoformat()}"),
        ) or 0
    return n


_foto_dia = {"d": None}
_foto_lock = threading.Lock()


def asegurar_foto_de_hoy() -> None:
    """Si la rutina diaria no grabó la foto de hoy, la graba la primera
    pantalla que pregunta — en un hilo: el vendedor no espera el semáforo."""
    hoy = today_ec()
    with _foto_lock:
        if _foto_dia["d"] == hoy:
            return
        _foto_dia["d"] = hoy
    try:
        if db.fetch_one("SELECT 1 AS si FROM scintela.semaforo_foto WHERE fecha = %s LIMIT 1",
                        (hoy,)):
            return
    except Exception:  # noqa: BLE001 — sin la tabla (migración pendiente) no hay foto
        return

    def _correr():
        try:
            foto_semaforo(hoy)
        except Exception as e:  # noqa: BLE001
            _LOG.warning("foto del semáforo falló: %s", e)
            _foto_dia["d"] = None

    threading.Thread(target=_correr, daemon=True, name="semaforo-foto").start()


# ── 2. La campanita del vendedor ────────────────────────────────────────────

def _cuando(ts) -> str:
    if not isinstance(ts, datetime):
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    t = ts.astimezone(_EC)
    hoy = today_ec()
    if t.date() == hoy:
        return t.strftime("%H:%M")
    if t.date() == hoy - timedelta(days=1):
        return "ayer"
    return t.strftime("%d/%m")


def _competencia(vend: str) -> tuple[dict | None, bool]:
    """El renglón de las sugerencias del día y si todavía no las vio."""
    from . import sugerencias
    hoy = today_ec()
    try:
        sug = sugerencias.del_dia(vend, hoy)
    except Exception:  # noqa: BLE001
        return None, False
    if not sug:
        return None, False
    telas = []
    for s in sug:
        if s.get("tela") and s["tela"] not in telas:
            telas.append(s["tela"])
    n = len(sug)
    item = {"tipo": "competencia",
            "titulo": f"{n} sugerencia{'' if n == 1 else 's'} de la competencia para hoy",
            "detalle": ", ".join(telas[:3]), "cuando": "hoy",
            "url": "/mi-cartera#competencia"}
    try:
        nueva = sugerencias.pendientes(vend, hoy) > 0
    except Exception:  # noqa: BLE001
        nueva = False
    return item, nueva


def lista(vend: str) -> tuple[list[dict], list[dict]]:
    """(nuevos, anteriores) de la campanita, más nuevo primero."""
    vend = (vend or "").upper().strip()
    nuevos: list[dict] = []
    viejos: list[dict] = []
    try:
        filas = db.fetch_all(
            """
            SELECT tipo, titulo, detalle, url, creado_en, visto_en
              FROM scintela.vendedor_aviso
             WHERE vend = %s AND creado_en >= now() - make_interval(days => %s)
             ORDER BY creado_en DESC, id_aviso DESC
             LIMIT 100
            """, (vend, DIAS_LISTA))
    except Exception as e:  # noqa: BLE001
        _LOG.warning("campanita de %s: %s", vend, e)
        filas = []
    for f in filas:
        it = {"tipo": f["tipo"], "titulo": f["titulo"], "detalle": f["detalle"],
              "url": f["url"] or "/mi-cartera", "cuando": _cuando(f["creado_en"]),
              "_t": f["creado_en"]}
        (viejos if f["visto_en"] else nuevos).append(it)
    try:
        for it, nuevo in _de_jonathan(vend):
            (nuevos if nuevo else viejos).append(it)
    except Exception as e:  # noqa: BLE001
        _LOG.warning("mensajes de Jonathan para %s: %s", vend, e)
    def _orden(it):
        t = it.get("_t")
        return t.timestamp() if isinstance(t, datetime) else 0
    nuevos.sort(key=_orden, reverse=True)
    viejos.sort(key=_orden, reverse=True)
    comp, comp_nueva = _competencia(vend)
    if comp:
        (nuevos if comp_nueva else viejos).insert(0, comp)
    return nuevos, viejos


def n_nuevos(vend: str) -> int:
    """El numerito de la campana: avisos sin ver + las sugerencias de hoy si
    todavía no abrió el Inicio. Nunca rompe: 0."""
    vend = (vend or "").upper().strip()
    if not vend:
        return 0
    asegurar_foto_de_hoy()
    n = 0
    try:
        r = db.fetch_one(
            """SELECT COUNT(*) AS n FROM scintela.vendedor_aviso
                WHERE vend = %s AND visto_en IS NULL
                  AND creado_en >= now() - make_interval(days => %s)""",
            (vend, DIAS_LISTA))
        n = int((r or {}).get("n") or 0)
    except Exception:  # noqa: BLE001
        n = 0
    try:
        n += sum(1 for _, nuevo in _de_jonathan(vend) if nuevo)
    except Exception:  # noqa: BLE001
        pass
    return n


def marcar_vistos(vend: str) -> None:
    try:
        db.execute(
            "UPDATE scintela.vendedor_aviso SET visto_en = now() "
            "WHERE vend = %s AND visto_en IS NULL",
            ((vend or "").upper().strip(),))
    except Exception as e:  # noqa: BLE001
        _LOG.warning("no pude marcar vistos de %s: %s", vend, e)


# ── 3. Avisar un problema ───────────────────────────────────────────────────

def guardar_reporte(*, usuario: str, vend: str | None, texto: str, pantalla: str,
                    dispositivo: str, user_agent: str,
                    foto: bytes | None = None, foto_tipo: str | None = None) -> int:
    """Guarda el reporte y le deja el aviso a Tamara. Devuelve el id."""
    texto = (texto or "").strip()[:2000]
    if not texto:
        raise ValueError("Escribí qué pasó.")
    if foto and len(foto) > FOTO_MAX_BYTES:
        raise ValueError("La foto es muy grande (máximo 6 MB).")
    if foto and not (foto_tipo or "").startswith("image/"):
        foto, foto_tipo = None, None
    r = db.execute_returning(
        """
        INSERT INTO scintela.reporte_app
               (usuario, vend, texto, pantalla, dispositivo, user_agent, foto, foto_tipo)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id_reporte
        """,
        ((usuario or "?")[:60], (vend or None), texto, (pantalla or "")[:300],
         (dispositivo or "")[:40], (user_agent or "")[:400], foto, foto_tipo))
    rid = int(r["id_reporte"])
    from modules.avisos import queries as avisos
    quien = vend or usuario
    avisos.avisar(fuente="reporte_app", nivel="alerta",
                  titulo=f"{quien} avisa un problema de la app",
                  detalle=texto[:300], url=f"/admin/reportes-app/{rid}",
                  clave=f"reporte_app:{rid}")
    return rid


def reportes_de(usuario: str) -> list[dict]:
    """Los avisos que mandó este usuario, más nuevo primero, con la respuesta."""
    try:
        filas = db.fetch_all(
            """SELECT id_reporte, texto, creado_en, respuesta
                 FROM scintela.reporte_app WHERE usuario = %s
                ORDER BY creado_en DESC LIMIT 20""", ((usuario or "")[:60],))
    except Exception:  # noqa: BLE001
        return []
    return [{"texto": f["texto"], "respuesta": f["respuesta"],
             "cuando": _cuando(f["creado_en"])} for f in filas]


def responder(id_reporte: int, respuesta: str, por: str, resuelto: bool = True) -> bool:
    """Tamara contesta: queda en el reporte y le llega a la campanita del
    vendedor que lo mandó."""
    respuesta = (respuesta or "").strip()[:2000]
    if not respuesta:
        raise ValueError("Escribí la respuesta.")
    r = db.execute_returning(
        """
        UPDATE scintela.reporte_app
           SET respuesta = %s, respondido_en = now(), respondido_por = %s,
               resuelto = %s
         WHERE id_reporte = %s
        RETURNING vend, texto
        """, (respuesta, (por or "")[:60], bool(resuelto), id_reporte))
    if not r:
        return False
    if r.get("vend"):
        db.execute(
            """
            INSERT INTO scintela.vendedor_aviso (vend, tipo, titulo, detalle, url, clave)
            VALUES (%s, 'respuesta', 'Tamara te contestó', %s,
                    '/mi-cartera/avisar-problema', %s)
            ON CONFLICT (clave) DO UPDATE
               SET detalle = EXCLUDED.detalle, creado_en = now(), visto_en = NULL
            """, (r["vend"], respuesta[:300], f"respuesta:{id_reporte}"))
    # El aviso de la oficina se da vuelta a "resuelto" (mismo renglón), así no
    # queda como pendiente en la campanita de Tamara.
    from modules.avisos import queries as avisos
    for a in avisos.abiertos_por_clave(f"reporte_app:{id_reporte}"):
        if a.get("clave") == f"reporte_app:{id_reporte}":
            avisos.resolver(a["id_aviso"], titulo=f"Contestado: {r['texto'][:120]}",
                            detalle=respuesta[:300])
    return True


def reportes(abiertos: bool | None = None) -> list[dict]:
    """La bandeja de Tamara."""
    where = ""
    if abiertos is True:
        where = "WHERE NOT resuelto"
    filas = db.fetch_all(
        f"""SELECT id_reporte, usuario, vend, texto, pantalla, dispositivo,
                   creado_en, respuesta, respondido_en, resuelto,
                   (foto IS NOT NULL) AS tiene_foto
              FROM scintela.reporte_app {where}
             ORDER BY resuelto, creado_en DESC LIMIT 200""")
    for f in filas:
        f["cuando"] = _cuando(f["creado_en"])
    return filas


def reporte(id_reporte: int) -> dict | None:
    f = db.fetch_one(
        """SELECT id_reporte, usuario, vend, texto, pantalla, dispositivo, user_agent,
                  creado_en, respuesta, respondido_en, respondido_por, resuelto,
                  (foto IS NOT NULL) AS tiene_foto
             FROM scintela.reporte_app WHERE id_reporte = %s""", (id_reporte,))
    if f:
        f["cuando"] = _cuando(f["creado_en"])
    return f


def foto_de(id_reporte: int) -> tuple[bytes, str] | None:
    f = db.fetch_one("SELECT foto, foto_tipo FROM scintela.reporte_app "
                     "WHERE id_reporte = %s AND foto IS NOT NULL", (id_reporte,))
    if not f:
        return None
    return bytes(f["foto"]), f["foto_tipo"] or "image/jpeg"


# ── Mensajes del memo (fase 2: formulas_app) ─────────────────────────────────

_charla_ok = {"t": 0.0, "ok": False}


def charla_disponible() -> bool:
    """Los mensajes con Jonathan necesitan su tabla en formulas_app. Sin ella
    la conversación no se muestra: mejor nada que un cuadro donde se escribe
    y no le llega a nadie. Se pregunta cada 5 minutos, no en cada pantalla."""
    import time

    from modules._lib import formulas_memos
    if time.monotonic() - _charla_ok["t"] > 300:
        _charla_ok.update(t=time.monotonic(), ok=formulas_memos.mensajes_disponible())
    return _charla_ok["ok"]


def charlas(numeros: list[str], vend: str) -> tuple[dict, dict]:
    """({numero: [mensajes]}, {numero: n_no_leidos}) de los pedidos del
    vendedor. Los de la fábrica que no leyó cuentan como nuevos."""
    if not numeros or not charla_disponible():
        return {}, {}
    from modules._lib import formulas_memos
    crudo = formulas_memos.mensajes(numeros)
    out, nuevos = {}, {}
    for num, msgs in crudo.items():
        out[num] = [{"de": m["de"], "texto": m["texto"],
                     "cuando": _cuando(m["creado_en"])} for m in msgs]
        n = sum(1 for m in msgs if m["de"] == "fabrica" and not m["leido_en"])
        if n:
            nuevos[num] = n
    return out, nuevos


def marcar_charlas_leidas(numeros: list[str]) -> None:
    if numeros and charla_disponible():
        from modules._lib import formulas_memos
        formulas_memos.marcar_leidos_por_vendedor(numeros)


def _de_jonathan(vend: str) -> list[tuple[dict, bool]]:
    """Los mensajes de la fábrica para la campanita: (item, nuevo)."""
    if not charla_disponible():
        return []
    from modules._lib import formulas_memos
    out = []
    for m in formulas_memos.nuevos_de_la_fabrica(vend):
        cli = f" ({m['cliente']})" if m["cliente"] else ""
        out.append(({"tipo": "jonathan", "titulo": f"Jonathan, sobre {m['numero']}{cli}",
                     "detalle": (m["texto"] or "")[:200], "cuando": _cuando(m["creado_en"]),
                     "url": f"/mi-cartera/pedidos#ped-{m['numero']}", "_t": m["creado_en"]},
                    not m["leido"]))
    return out
