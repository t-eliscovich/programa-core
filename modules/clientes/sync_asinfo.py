"""Sync del maestro de clientes Asinfo → Programa Core.

⭐ POR QUÉ (TMT 2026-08-05, pedido de la dueña). Con el dBase retirado, los
clientes nuevos nacen en Asinfo (el ERP que factura contra el SRI) y PC se
enteraba tarde y mal: la ficha llegaba pelada por el auto-create de la carga
de facturas, y nadie le cargaba cupo ni descuento hasta que algo se rompía.

Decisiones de la dueña (05/08/2026), medidas contra la base en vivo:

- **"Lo nuevo vale más"**: NOMBRE y RUC se pisan con Asinfo. El nombre queda
  en formato fiscal "APELLIDO NOMBRE" (3.388 de 3.603 diferían sólo por el
  orden). Asinfo es la autoridad del RUC (68 diferían; varios eran typos PC).
- **Cupo sólo existe en PC** → este sync no lo toca nunca.
  Tampoco `vend` (mueve las comisiones), ni `correo` (ya existe el espejo
  `cliente_mail_asinfo`, con su propio filtro de mails de la casa), ni la
  dirección (Asinfo no tiene texto de dirección: 0 de 3.635), ni
  observación/pago/stop.
- **Teléfono: PC gana.** 2.640 de 3.635 teléfonos de Asinfo son basura
  (`2222222` o <7 dígitos) y PC tiene 3.833 reales. Sólo se RELLENA el
  teléfono cuando PC está vacío y el de Asinfo parece real.
- **DESCUENTO: Asinfo rellena, PC manda** (TMT 2026-08-25, dueña). Asinfo sí
  lo tiene cargado, en una LISTA por cliente (`cliente.id_lista_descuentos` →
  `lista_descuentos.nombre`, con nombres tipo `5%y7%`). El primer tramo es el
  5% de contado, igual para todos; el SEGUNDO es el descuento del cliente, y
  es el que PC guarda en `cliente.descuento`. Medido el 25/08 contra la base
  en vivo: de los 3.644 códigos que cruzan, 3.333 ya coincidían (96%), 166
  estaban vacíos en PC y 145 difieren. Por eso: se RELLENA cuando en PC está
  vacío o en cero, NUNCA se pisa un descuento cargado, y las diferencias se
  listan en la pantalla del sync para que las decida una persona.
- **Cliente nuevo limpio → alta automática + campanita** para que Andrés le
  cargue el cupo (el descuento ya viene de Asinfo, si la lista se entiende).
- **Cliente nuevo cuyo RUC YA está en PC bajo otro código → NO se importa**
  (patrón sucursal/recodificación: duplicaría plata). Queda como conflicto en
  /clientes/sync-asinfo con aviso, y lo decide una persona. Ídem los códigos
  que Asinfo tiene duplicados internamente (AR1, PRE al 05/08).

En Asinfo el código de 3 letras vive en `empresa.nombre_comercial`
(`empresa.codigo` ES el RUC — ver skill clientes-codigos-duplicados), el
teléfono en `direccion_empresa` (la fila principal activa) y el descuento en
`cliente` (una fila por empresa: medido 25/08, 3.654 empresas cliente activas
= 3.654 filas en `cliente`, ninguna repetida y ninguna faltante).

Corre 2× por día (11:00 y 16:00 EC) vía `scripts/sync_clientes_asinfo.py`
en el EC2, y a demanda desde la pantalla /clientes/sync-asinfo.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time as _time
from datetime import UTC, datetime
from urllib.parse import quote as _quote

import db

_LOG = logging.getLogger("programa_core.clientes.sync_asinfo")

#: Códigos de cliente válidos: 1 a 4 caracteres visibles. No se exige
#: [A-Z0-9] porque en producción existen códigos con caracteres raros
#: (`D´J`) que el resto del sistema maneja bien (todo JOINea por el string).
_MAX_COD = 4

_SQL_ASINFO = """
SELECT UPPER(LTRIM(RTRIM(COALESCE(e.nombre_comercial, '')))) AS cod,
       LTRIM(RTRIM(COALESCE(e.identificacion, '')))          AS ruc,
       LTRIM(RTRIM(COALESCE(e.nombre_fiscal, '')))           AS nombre,
       LTRIM(RTRIM(COALESCE(d.telefono1, '')))               AS tel1,
       LTRIM(RTRIM(COALESCE(d.telefono2, '')))               AS tel2,
       LTRIM(RTRIM(COALESCE(ld.nombre, '')))                 AS lista_desc
  FROM empresa e
  LEFT JOIN direccion_empresa d
         ON d.id_empresa = e.id_empresa
        AND d.indicador_direccion_principal = 1
        AND d.activo = 1
  LEFT JOIN cliente cl
         ON cl.id_empresa = e.id_empresa
  LEFT JOIN lista_descuentos ld
         ON ld.id_lista_descuentos = cl.id_lista_descuentos
 WHERE e.indicador_cliente = 1
   AND e.activo = 1
"""

_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS scintela.clientes_sync_asinfo_log (
    id_corrida  SERIAL PRIMARY KEY,
    corrido     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    usuario     VARCHAR(60),
    ok          BOOLEAN NOT NULL,
    resumen     TEXT
)
"""


def ruc10(valor: str | None) -> str:
    """Los 10 primeros dígitos del RUC — la clave de cruce PC↔Asinfo."""
    digitos = re.sub(r"\D", "", valor or "")
    return digitos[:10] if len(digitos) >= 10 else ""


def telefono_util(tel: str | None) -> str:
    """El teléfono de Asinfo, sólo si parece real.

    Asinfo rellena con `2222222` (y variantes de un solo dígito repetido)
    cuando el cliente no dio teléfono. Eso NO puede pisar ni rellenar nada.
    """
    tel = (tel or "").strip()
    digitos = re.sub(r"\D", "", tel)
    if len(digitos) < 7:
        return ""
    if len(set(digitos)) == 1 or "2222222" in digitos:
        return ""
    return tel[:30]


#: `5%y7%` → 7.0. El primer tramo (5%) es el descuento de contado, igual
#: para las 12 listas que existen; el segundo es el del cliente. Cualquier
#: nombre con otra forma NO se interpreta: se avisa y la ficha queda como
#: está. Preferimos no cargar nada antes que cargar un número inventado.
_RE_LISTA_DESC = re.compile(r"^\s*5\s*%\s*y\s*(\d{1,2}(?:[.,]\d{1,2})?)\s*%\s*$", re.I)


def descuento_de_lista(nombre: str | None) -> float | None:
    """El descuento del cliente que esconde el nombre de la lista de Asinfo."""
    m = _RE_LISTA_DESC.match(nombre or "")
    if not m:
        return None
    try:
        valor = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    return valor if 0 <= valor <= 99 else None


def descuento_a_escribir(pc_val, asi_val) -> float | None:
    """El descuento que hay que GRABAR, o None si la ficha no se toca.

    Rellena sólo el hueco: vacío o cero en PC. Un descuento ya cargado no se
    pisa nunca — eso es una diferencia para que la mire una persona.
    """
    if asi_val is None:
        return None
    if pc_val is None:
        return float(asi_val)
    if float(pc_val) == 0.0 and float(asi_val) != 0.0:
        return float(asi_val)
    return None


def _norm_nombre(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).upper()


def asegurar_tabla() -> None:
    db.execute(_BOOTSTRAP_SQL)


def traer_asinfo() -> tuple[list[dict], bool]:
    """Maestro de clientes activos de Asinfo. `(filas, contestó)`.

    `contestó=False` = Metabase caído/sin config — que NO es lo mismo que
    "Asinfo no tiene clientes". Con False no se toca nada.
    """
    from modules._lib import metabase_client as mc

    if not mc.disponible():
        return [], False
    filas, contesto = mc.fetch_dataset_estado(2, _SQL_ASINFO, max_results=20000)
    return list(filas or []), bool(contesto)


def sincronizar(usuario: str = "sync-asinfo") -> dict:
    """Un pase completo, idempotente. Nunca levanta: devuelve el reporte.

    Reporte: {ok, actualizados, altas: [cod], conflictos: [...],
    dup_asinfo: [cod], descuentos_puestos, dif_descuento: [...],
    listas_raras: [...], sin_tocar, leidas_de_asinfo, error?}.
    """
    try:
        return _sincronizar(usuario)
    except Exception as e:  # noqa: BLE001 — el cron no puede morir a mitad
        _LOG.exception("sync_asinfo murió")
        reporte = {"ok": False, "error": str(e)[:300]}
        _guardar_log(usuario, reporte)
        return reporte


def _sincronizar(usuario: str) -> dict:
    filas, contesto = traer_asinfo()
    if not contesto:
        reporte = {"ok": False, "error": "Metabase no contestó", "leidas_de_asinfo": 0}
        _guardar_log(usuario, reporte)
        return reporte

    # ── Lado Asinfo: depurar ────────────────────────────────────────────
    por_cod: dict[str, dict] = {}
    duplicados: set[str] = set()
    for f in filas:
        cod = str(f.get("cod") or "").strip().upper()
        if not (0 < len(cod) <= _MAX_COD):
            continue
        if cod in por_cod:
            duplicados.add(cod)
            continue
        por_cod[cod] = {
            "cod": cod,
            "ruc": str(f.get("ruc") or "").strip()[:16],
            "nombre": str(f.get("nombre") or "").strip()[:200],
            "tel": telefono_util(f.get("tel1")) or telefono_util(f.get("tel2")),
            "lista_desc": str(f.get("lista_desc") or "").strip()[:60],
            "desc": descuento_de_lista(f.get("lista_desc")),
        }
    # Un código que Asinfo tiene repetido no se puede sincronizar: no hay
    # forma de saber cuál de las dos empresas es "la" del código.
    for cod in duplicados:
        por_cod.pop(cod, None)

    # ── Lado PC ─────────────────────────────────────────────────────────
    pc_rows = db.fetch_all(
        "SELECT id_cliente, UPPER(TRIM(codigo_cli)) AS cod, nombre, ruc, "
        "       telefono, descuento "
        "FROM scintela.cliente"
    ) or []
    pc_por_cod = {r["cod"]: r for r in pc_rows if r.get("cod")}
    pc_por_ruc: dict[str, list[str]] = {}
    for r in pc_rows:
        clave = ruc10(r.get("ruc"))
        if clave and r.get("cod"):
            pc_por_ruc.setdefault(clave, []).append(r["cod"])

    # ── Fichas existentes: pisar nombre/RUC, rellenar teléfono ─────────
    cambios: list[tuple] = []  # (cod, nombre|None, ruc|None, tel|None, desc|None)
    dif_descuento: list[dict] = []
    listas_raras: list[dict] = []
    for cod, a in por_cod.items():
        p = pc_por_cod.get(cod)
        if p is None:
            continue
        nombre_nuevo = a["nombre"] if (
            a["nombre"] and _norm_nombre(a["nombre"]) != _norm_nombre(p.get("nombre"))
        ) else None
        ruc_nuevo = a["ruc"] if (
            a["ruc"]
            and re.sub(r"\D", "", a["ruc"]) != re.sub(r"\D", "", p.get("ruc") or "")
        ) else None
        tel_nuevo = a["tel"] if (a["tel"] and not (p.get("telefono") or "").strip()) else None
        desc_nuevo = descuento_a_escribir(p.get("descuento"), a["desc"])
        if a["desc"] is None and a["lista_desc"]:
            listas_raras.append({"cod": cod, "lista": a["lista_desc"]})
        elif (
            a["desc"] is not None
            and p.get("descuento") is not None
            and float(p["descuento"]) != 0.0
            and float(p["descuento"]) != float(a["desc"])
        ):
            dif_descuento.append({
                "cod": cod, "nombre": (p.get("nombre") or "")[:60],
                "pc": float(p["descuento"]), "asinfo": float(a["desc"]),
            })
        if nombre_nuevo or ruc_nuevo or tel_nuevo or desc_nuevo is not None:
            cambios.append((cod, nombre_nuevo, ruc_nuevo, tel_nuevo, desc_nuevo))

    actualizados = _aplicar_cambios(cambios, usuario) if cambios else 0

    # ── Altas ───────────────────────────────────────────────────────────
    altas: list[str] = []
    conflictos: list[dict] = []
    for cod, a in por_cod.items():
        if cod in pc_por_cod:
            continue
        ocupantes = pc_por_ruc.get(ruc10(a["ruc"]), [])
        if ocupantes:
            # El RUC ya vive en PC bajo otro código → sucursal o
            # recodificación. Importarlo a ciegas duplicaría la plata del
            # cliente (todo JOINea por código). Lo decide una persona.
            conflictos.append({
                "cod": cod, "ruc": a["ruc"], "nombre": a["nombre"],
                "en_pc": sorted(set(ocupantes)),
            })
            continue
        if not a["nombre"]:
            conflictos.append({"cod": cod, "ruc": a["ruc"], "nombre": "",
                               "en_pc": [], "motivo": "sin nombre en Asinfo"})
            continue
        if _alta(a, usuario):
            altas.append(cod)

    _avisar_conflictos(conflictos, duplicados)
    _avisar_descuentos(dif_descuento, listas_raras)

    reporte = {
        "ok": True,
        "leidas_de_asinfo": len(filas),
        "clientes_asinfo": len(por_cod),
        "actualizados": actualizados,
        "altas": sorted(altas),
        "conflictos": conflictos,
        "dup_asinfo": sorted(duplicados),
        "descuentos_puestos": sum(1 for c in cambios if c[4] is not None),
        "dif_descuento": sorted(dif_descuento, key=lambda d: d["cod"]),
        "listas_raras": sorted(listas_raras, key=lambda d: d["cod"]),
        "sin_tocar": len(por_cod) - len(cambios) - len(altas) - len(conflictos),
    }
    _guardar_log(usuario, reporte)
    return reporte


def _aplicar_cambios(cambios: list[tuple], usuario: str) -> int:
    """UN solo UPDATE con VALUES — un viaje por fila a RDS ya tiró un 502
    en el cron de mails (TMT 2026-08-03); acá el primer pase toca ~3.400."""
    marcadores = ",".join(["(%s, %s, %s, %s, %s)"] * len(cambios))
    planos = [
        (str(v) if (i == 4 and v is not None) else v)
        for fila in cambios for i, v in enumerate(fila)
    ]
    return db.execute(
        f"""
        UPDATE scintela.cliente c
           SET nombre   = COALESCE(v.nombre, c.nombre),
               ruc      = COALESCE(v.ruc, c.ruc),
               telefono = COALESCE(v.telefono, c.telefono),
               descuento = COALESCE(v.descuento::numeric, c.descuento),
               fecha_modifica   = CURRENT_TIMESTAMP,
               usuario_modifica = %s
          FROM (VALUES {marcadores}) AS v(cod, nombre, ruc, telefono, descuento)
         WHERE UPPER(TRIM(c.codigo_cli)) = v.cod
        """,
        tuple([usuario[:60], *planos]),
    )


def _alta(a: dict, usuario: str) -> bool:
    """INSERT del cliente nuevo + campanita para lo que Asinfo no sabe."""
    from modules.avisos.queries import avisar
    from modules.clientes import queries as cli_q

    try:
        cli_q.crear(
            codigo_cli=a["cod"], nombre=a["nombre"], ruc=a["ruc"] or None,
            telefono=a["tel"] or None, descuento=a.get("desc"),
            usuario=usuario[:50],
        )
    except Exception as e:  # noqa: BLE001 — una alta rota no frena las demás
        _LOG.warning("alta de %s falló: %s", a["cod"], e)
        return False
    falta = "cupo" if a.get("desc") is not None else "cupo y descuento"
    avisar(
        fuente="clientes",
        nivel="alerta",
        titulo=f"Cliente nuevo {a['cod']} — cargarle {falta}",
        detalle=(a["nombre"] or "")[:150],
        # Directo a la pantalla de EDITAR, que es donde se carga el cupo —
        # no a la lista filtrada. TMT 2026-08-06 (dueña): el click de la
        # campanita te tiene que llevar derecho a cargarlo.
        # quote() porque hay códigos con caracteres raros (D´J existe).
        url=f"/clientes/{_quote(a['cod'])}/editar",
        clave=f"cliente-nuevo-{a['cod']}",
    )
    return True


def _avisar_conflictos(conflictos: list[dict], dup_asinfo: set[str]) -> None:
    from modules.avisos.queries import avisar

    for c in conflictos:
        en_pc = ", ".join(c.get("en_pc") or [])
        detalle = (
            f"RUC {c['ruc']} ya está en PC como {en_pc}" if en_pc
            else c.get("motivo", "")
        )
        avisar(
            fuente="clientes",
            nivel="alerta",
            titulo=f"Cliente Asinfo {c['cod']} no se importó — revisar",
            detalle=detalle[:200],
            url="/clientes/sync-asinfo",
            clave=f"cliente-conflicto-{c['cod']}",
        )
    for cod in sorted(dup_asinfo):
        avisar(
            fuente="clientes",
            nivel="alerta",
            titulo=f"Asinfo tiene el código {cod} duplicado — no se sincroniza",
            detalle="Dos empresas activas comparten el código en Asinfo.",
            url="/clientes/sync-asinfo",
            clave=f"cliente-dup-asinfo-{cod}",
        )


def _avisar_descuentos(dif: list[dict], raras: list[dict]) -> None:
    """UNA campanita por cada cosa, no una por cliente.

    Las diferencias de descuento son ~145 al estrenar esto: una campanita por
    cliente sería un buzón inservible. El número va en el título y el detalle
    está en la pantalla del sync. `clave` con la cantidad adentro para que un
    número distinto vuelva a avisar y el mismo número no repita.
    """
    from modules.avisos.queries import avisar

    if dif:
        cods = ", ".join(d["cod"] for d in sorted(dif, key=lambda d: d["cod"])[:8])
        avisar(
            fuente="clientes", nivel="alerta",
            titulo=f"{len(dif)} clientes con descuento distinto al de Asinfo",
            detalle=f"Manda el de Programa Core. {cods}"[:200],
            url="/clientes/sync-asinfo",
            clave=f"clientes-dif-descuento-{len(dif)}",
        )
    if raras:
        cods = ", ".join(f"{r['cod']} ({r['lista']})" for r in raras[:5])
        avisar(
            fuente="clientes", nivel="alerta",
            titulo=f"{len(raras)} listas de descuento de Asinfo que no entiendo",
            detalle=f"No se cargó nada para: {cods}"[:200],
            url="/clientes/sync-asinfo",
            clave=f"clientes-listas-raras-{len(raras)}",
        )


def _para_log(reporte: dict) -> dict:
    """El reporte, recortado para que entre en el log SIN romper el JSON.

    Se guarda como texto y se relee con `json.loads`: cortar el STRING a lo
    bruto dejaría un JSON inválido y la corrida se vería vacía en la pantalla.
    Por eso se recortan las LISTAS largas, no el texto.
    """
    chico = dict(reporte)
    for campo in ("dif_descuento", "listas_raras", "conflictos"):
        filas = chico.get(campo)
        if isinstance(filas, list) and len(filas) > 300:
            chico[campo] = filas[:300]
            chico[f"{campo}_total"] = len(filas)
    return chico


def _guardar_log(usuario: str, reporte: dict) -> None:
    try:
        asegurar_tabla()
        db.execute(
            "INSERT INTO scintela.clientes_sync_asinfo_log (usuario, ok, resumen) "
            "VALUES (%s, %s, %s)",
            (usuario[:60], bool(reporte.get("ok")),
             json.dumps(_para_log(reporte))[:60000]),
        )
    except Exception as e:  # noqa: BLE001
        _LOG.warning("no pude guardar el log del sync: %s", e)


def ultimas_corridas(limite: int = 10) -> list[dict]:
    try:
        asegurar_tabla()
        rows = db.fetch_all(
            "SELECT corrido, usuario, ok, resumen "
            "FROM scintela.clientes_sync_asinfo_log "
            "ORDER BY id_corrida DESC LIMIT %s",
            (limite,),
        ) or []
    except Exception as e:  # noqa: BLE001
        _LOG.warning("no pude leer el log del sync: %s", e)
        return []
    for r in rows:
        try:
            r["reporte"] = json.loads(r.pop("resumen") or "{}")
        except (ValueError, TypeError):
            r["reporte"] = {}
    return rows


# ---------------------------------------------------------------------------
# Corre SOLO, sin cron del EC2 — TMT 2026-08-05 (dueña: "las importaciones y
# las facturas no están hechas por EC2, no hacemos eso"). Mismo patrón que la
# autocarga de facturas / tejeduría / químicos: el hilo de fondo de
# modules/_lib/autocarga_facturas.py llama `correr_si_toca()` cada ~2 min y
# ACÁ se decide si toca. Ventanas pedidas: 11:00 y 16:00 hora Ecuador
# (= 16:00 y 21:00 UTC; el server y Postgres están en UTC).
#
# El guard de "ya corrió en esta ventana" NO es en memoria: mira el log
# (`clientes_sync_asinfo_log`), así un restart del server no lo repite y una
# corrida MANUAL dentro de la ventana también cuenta. SYNC_CLIENTES_AUTO=0
# lo apaga.
# ---------------------------------------------------------------------------

_VENTANAS_UTC = (16, 21)  # 11:00 y 16:00 Ecuador (UTC-5)
_CHECK_MIN_SECS = 300     # mirar el log a lo sumo cada 5 min
_auto_lock = threading.Lock()
_auto_ultimo_check = 0.0


def _inicio_ventana_utc(ahora_utc: datetime) -> datetime | None:
    """El comienzo de la ventana vigente, o None si todavía no abrió ninguna.

    Después de medianoche UTC (19:00 EC) no abre ninguna: las dos corridas
    del día ya pasaron.
    """
    inicio = None
    for h in _VENTANAS_UTC:
        cand = ahora_utc.replace(hour=h, minute=0, second=0, microsecond=0)
        if cand <= ahora_utc:
            inicio = cand
    return inicio


def correr_si_toca() -> dict:
    """Entrada del hilo de fondo. Nunca levanta."""
    res = {"corrio": False}
    if os.environ.get("SYNC_CLIENTES_AUTO", "1") == "0":
        return res
    global _auto_ultimo_check
    ahora_mono = _time.monotonic()
    with _auto_lock:
        if _auto_ultimo_check and (ahora_mono - _auto_ultimo_check) < _CHECK_MIN_SECS:
            return res
        _auto_ultimo_check = ahora_mono
    try:
        ahora_utc = datetime.now(UTC)
        inicio = _inicio_ventana_utc(ahora_utc)
        if inicio is None:
            return res
        asegurar_tabla()
        ya = db.fetch_one(
            "SELECT 1 AS x FROM scintela.clientes_sync_asinfo_log "
            "WHERE ok AND corrido >= %s LIMIT 1",
            (inicio.replace(tzinfo=None),),
        )
        if ya:
            return res
        res["corrio"] = True
        res["reporte"] = sincronizar(usuario="auto-sync-clientes")
    except Exception as e:  # noqa: BLE001 — el hilo no se cae por esto
        _LOG.warning("sync clientes (fondo): %s", e)
    return res
