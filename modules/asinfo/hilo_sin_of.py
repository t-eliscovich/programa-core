"""Hilo que sale de bodega SIN orden de fabricación — aviso a la campanita.

TMT 2026-08-11. La dueña vio la utilidad caer $ 24.327 en una hora, en dos
escalones (07:35 −14.624 y 08:32 −9.703) y preguntó si era un bug.

No lo era, y el diagnóstico vale escribirlo porque el aviso nace de él:

    El stock de hilado del balance NO es el saldo de la bodega 51 a secas:

        hilado = bodega 51  +  EN PROCESO

    donde "en proceso" = material despachado a órdenes de fabricación ABIERTAS
    y todavía no devuelto como tela (`asinfo.service.fabricacion_proceso(52)`).
    Esa cuenta se arma arrancando desde las ÓRDENES: le pregunta a cada OFT
    abierta cuánto hilo le despacharon. Un despacho sin orden no lo reclama
    nadie — sale de la bodega y no entra a ningún lado. Deja de ser un activo.

Ese día salieron a Ponce 8.100 kg en dos despachos marcados `#OF:NR`
("A PONCE PENDIENTE 180/C KW22"): el hilo se mandó al telar antes de crear la
orden. El circuito normal es al revés — primero la OFT con los kilos
planificados, después el despacho colgado de ella, y calzan exactos
(30/07: OFT-000040127 planificó 4.050 → OSM-000010333 despachó 4.050).

**Por qué esto no se había visto nunca.** La regla vive desde el 12/07
(`4217dc91`, el balance pasó a leer `inventario_por_etapa`), pero en todo 2026
los despachos de hilo sin orden fueron 1 en enero (97 kg), 3 en junio (2.934) y
2 en julio (21). Contra 279-381 despachos POR MES, todos con su orden. 8.100 kg
en un día no tiene precedente: la regla nunca se había puesto a prueba.

**El síntoma que lo distingue de un bache normal: NO REBOTA.** Un despacho CON
orden también hace un pozo — la bodega baja al instante y el "en proceso" tarda
hasta 10 minutos (el caché de `fabricacion_proceso`), así que la foto del medio
ve el hueco. Pero se recompone en 1-3 fotos. Las 20 bajas de más de 700 kg de
`traza_utilidad` desde el 31/07 rebotaron TODAS (03/08 −4.590→+3.567, 04/08
−3.398→+3.241, 06/08 −2.484→+2.371, 07/08 −1.368→+1.323, 09/08 −1.394→+1.327…)
menos las dos del 11/08, cuyo mayor rebote en tres fotos fue 0,00.

Decisiones de este aviso:

· **Va a la CAMPANITA**, no al balance — mismo criterio que las importaciones
  sin plata (dueña 2026-07-31: *"nada de rojo en resultados, en la campanita"*).
· **Piso de kilos.** Con `_MIN_KG` en 200 los ocho meses de 2026 habrían dado
  cinco avisos. Sin piso, un despacho de 21 kg encendería la campanita por
  US$ 64 y entrenaría a ignorarla.
· **Ventana de días**, para que el estreno no vuelque un backlog. La vigilancia
  de importaciones aprendió eso rompiéndolo: sin techo, 200+ avisos a los tres
  minutos de deployar.
· **Se preguntan las DOS junctions** (la de cabecera, que es la que alimenta el
  balance, y la de detalle) y sólo se avisa si las dos están vacías. Una sola
  daría falsos positivos el día que Asinfo cambie por cuál cuelga la orden.
· **Clave idempotente por número de OSM**: se dice una vez y no vuelve, aunque
  el ciclo pase cuatro veces por hora.
· **Y se DA VUELTA solo cuando cargan la orden** (dueña 2026-08-11: *"y si
  cargan la oft, también saldría en campanita no?"*, y después *"claro,
  campanita"*). Quedaría colgado diciendo "falta cargar" sobre algo ya cargado
  — la forma más rápida de que la campanita deje de creerse. Pero archivarlo
  en silencio tampoco: ella vio el anuncio bajar de 4 a 3 sin que nadie se lo
  dijera. Así que el mismo barrido reescribe EL MISMO aviso —pasa a ✅ y dice
  con qué orden se resolvió—, y queda un renglón por despacho de punta a punta.
· **Se habla en KILOS, no en plata** (dueña 2026-08-11: *"no digas la utilidad,
  decí la bodega baja x kg"*). Quien recibe este aviso es quien despacha, y lo
  que puede arreglar son los kilos que faltan cargar — la plata es una
  consecuencia que se mira en otra pantalla y acá sólo agrega ruido.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time as _t
from datetime import UTC, datetime, timedelta

_LOG = logging.getLogger("programa_core.asinfo.hilo_sin_of")

# Bodegas de Asinfo cuyo material, al salir sin orden, se cae del balance.
# 51 = Hilo (entra a "en proceso" de tejeduría) · 52 = Tela Cruda (a tintura).
MATERIAL_POR_BODEGA = {51: "hilo", 52: "tela cruda"}

# Se vigila SÓLO el hilo. La tela cruda sale sin orden casi todos los días
# (medido: 13 despachos en los primeros 11 días de agosto, 200-500 kg cada uno)
# y avisar de eso sería un ⚠ diario, que es la forma más rápida de que la
# campanita se vuelva invisible. Que la tela cruda también se caiga del balance
# es un hallazgo aparte, para decidir con la dueña — no se mete de contrabando
# en el aviso que pidió. `HILO_SIN_OF_BODEGAS=51,52` la enciende sin deploy.
BODEGAS_DEFAULT = (51,)

# Piso de kilos para avisar. Medido sobre 2026: con 200 kg salen cinco avisos
# en ocho meses; sin piso, tres de ellos serían de 21, 37 y 97 kg.
_MIN_KG_DEFAULT = 200.0

# Sólo se miran los despachos de los últimos N días. No es un inventario
# histórico: es "esto pasó y todavía se puede arreglar".
_DIAS_DEFAULT = 7

_FRENO_SECS = 15 * 60          # cuatro chances por hora: alcanza para el día
_ultima_corrida = 0.0
_lock = threading.Lock()


def _min_kg() -> float:
    try:
        v = float(os.environ.get("HILO_SIN_OF_MIN_KG", _MIN_KG_DEFAULT))
        return v if v > 0 else _MIN_KG_DEFAULT
    except (TypeError, ValueError):
        return _MIN_KG_DEFAULT


def _dias() -> int:
    try:
        v = int(os.environ.get("HILO_SIN_OF_DIAS", _DIAS_DEFAULT))
        return v if v >= 1 else _DIAS_DEFAULT
    except (TypeError, ValueError):
        return _DIAS_DEFAULT


def _bodegas() -> tuple[int, ...]:
    """Las bodegas vigiladas. `HILO_SIN_OF_BODEGAS=51,52` suma la tela cruda."""
    crudo = (os.environ.get("HILO_SIN_OF_BODEGAS") or "").strip()
    if not crudo:
        return BODEGAS_DEFAULT
    vals = []
    for parte in crudo.split(","):
        try:
            v = int(parte.strip())
        except (TypeError, ValueError):
            continue
        if v in MATERIAL_POR_BODEGA:
            vals.append(v)
    return tuple(sorted(set(vals))) if vals else BODEGAS_DEFAULT


#: Cache corto de `despachos_sin_of`. TMT 2026-08-24 (dueña: *"qué más
#: podemos hacer más rápido?"*): esta consulta no tenía cache y la pagaban
#: TRES pantallas en cada visita —Inventario en proceso (/stock/fabricacion-tc,
#: 1,7 s medidos, casi todos de acá), el Balance y Tejeduría—. No es un dato
#: que cambie por segundo: son despachos de hilo que alguien tiene que ir a
#: cargar. Con 120 s, lo peor que puede pasar es ver un despacho dos minutos
#: tarde; el vigía de la campanita corre cada 15 min, así que ni lo nota.
_CACHE: dict[tuple, tuple[float, list[dict]]] = {}
_CACHE_TTL_DEFAULT = 120.0


def _cache_ttl() -> float:
    """Segundos que vale la respuesta. `HILO_SIN_OF_CACHE_SECS=0` lo apaga."""
    try:
        return float(os.environ.get("HILO_SIN_OF_CACHE_SECS",
                                    _CACHE_TTL_DEFAULT))
    except (TypeError, ValueError):
        return _CACHE_TTL_DEFAULT


def reset_cache() -> None:
    """Olvida lo cacheado. La usan los tests y cualquiera que quiera el dato
    fresco sin esperar los 120 s."""
    _CACHE.clear()


def despachos_sin_of(dias: int | None = None,
                     min_kg: float | None = None) -> list[dict]:
    """Despachos de hilo/tela cruda sin NINGUNA orden de fabricación colgada.

    Fail-soft: [] si Asinfo no contesta. Una alarma que no puede leer no
    inventa — el mismo criterio que el resto de los bridges.
    """
    from modules._lib import metabase_client

    dias = int(dias if dias is not None else _dias())
    min_kg = float(min_kg if min_kg is not None else _min_kg())
    bodegas = ", ".join(str(b) for b in _bodegas())

    clave = (dias, min_kg, bodegas)
    ttl = _cache_ttl()
    ahora = _t.monotonic()
    guardado = _CACHE.get(clave)
    if ttl > 0 and guardado and (ahora - guardado[0]) < ttl:
        return guardado[1]

    # Las dos junctions: `orden_fabricacion_orden_salida_material` es la que
    # alimenta el balance (fabricacion_proceso); la de detalle es la que usa
    # stock_en_proceso(). Se exige que las DOS estén vacías.
    sql = f"""
        WITH desp AS (
            SELECT osm.id_orden_salida_material  AS id_osm,
                   osm.numero                    AS numero,
                   osm.fecha_creacion            AS creado,
                   osm.usuario_creacion          AS usuario,
                   osm.descripcion               AS descripcion,
                   d.id_bodega                   AS id_bodega,
                   SUM(ISNULL(d.cantidad_despachada, 0)) AS kg
              FROM orden_salida_material osm
              JOIN detalle_orden_salida_material d
                ON d.id_orden_salida_material = osm.id_orden_salida_material
             WHERE osm.fecha_creacion >= DATEADD(day, -{dias}, CAST(GETDATE() AS date))
               AND d.id_bodega IN ({bodegas})
             GROUP BY osm.id_orden_salida_material, osm.numero, osm.fecha_creacion,
                      osm.usuario_creacion, osm.descripcion, d.id_bodega
        )
        SELECT x.numero, x.id_bodega, x.kg, x.usuario, x.descripcion,
               CONVERT(varchar(16), x.creado, 120) AS creado
          FROM desp x
         WHERE x.kg >= {min_kg}
           AND NOT EXISTS (SELECT 1 FROM orden_fabricacion_orden_salida_material j
                            WHERE j.id_orden_salida_material = x.id_osm)
           AND NOT EXISTS (SELECT 1
                             FROM detalle_orden_salida_material dd
                             JOIN detalle_orden_salida_material_orden_fabricacion jd
                               ON jd.id_detalle_orden_salida_material
                                = dd.id_detalle_orden_salida_material
                            WHERE dd.id_orden_salida_material = x.id_osm)
         ORDER BY x.creado DESC
    """
    try:
        rows = metabase_client.fetch_dataset(2, sql, max_results=500)
    except Exception as e:  # noqa: BLE001
        _LOG.warning("no pude leer los despachos sin orden: %s", e)
        return []

    out = []
    for r in rows or []:
        try:
            kg = float(r.get("kg") or 0)
            bodega = int(r.get("id_bodega") or 0)
        except (TypeError, ValueError):
            continue
        if kg < min_kg:
            continue
        out.append({
            "numero": str(r.get("numero") or "").strip(),
            "id_bodega": bodega,
            "material": MATERIAL_POR_BODEGA.get(bodega, "material"),
            "kg": round(kg, 2),
            "usuario": str(r.get("usuario") or "").strip(),
            "descripcion": _limpiar(r.get("descripcion")),
            "creado": str(r.get("creado") or "").strip(),
        })
    # Sólo se guarda si Asinfo CONTESTÓ. Si no contestó (`rows` vacío por
    # error de red) la lista también sale vacía, y cachear eso 2 minutos
    # sería sostener un "no hay nada" que en realidad es "no pude preguntar".
    if ttl > 0 and rows:
        _CACHE[clave] = (ahora, out)
    return out


def _limpiar(descripcion) -> str:
    """Saca el `[#OF:NR]` y las barras del pipe que Asinfo mete en la glosa.

    Lo que queda es lo que escribió la persona ("A PONCE PENDIENTE 180/C KW22"),
    que es el único pedazo que sirve para ir a buscar el despacho.
    """
    s = str(descripcion or "").strip()
    if not s:
        return ""
    if "]" in s:
        s = s.split("]", 1)[1]
    return s.replace("|Matriz|", "").replace("|", " ").strip(" ·-").strip()


#: Hora de Ecuador a la que el placeholder deja de sostener los kilos. Dueña
#: 2026-08-11: *"al final del día si no fue cargada manda nuevamente aviso y
#: ahí sí proceder a bajarla"*. A las 18 y no a medianoche: así la baja cae en
#: horario, en la traza del día que corresponde, después del aviso de las 17 y
#: ANTES de la foto de cierre — el mes nunca se cierra con el placeholder
#: adentro.
HORA_CORTE = 18


def _ahora_ec() -> datetime:
    """Ahora en Ecuador (UTC−5, sin horario de verano) — igual que today_ec()."""
    return datetime.now(UTC) - timedelta(hours=5)


def _hora_corte() -> int:
    try:
        h = int(os.environ.get("HILO_PLACEHOLDER_CORTE", str(HORA_CORTE)))
    except (TypeError, ValueError):
        return HORA_CORTE
    return h if 0 <= h <= 23 else HORA_CORTE


def placeholder_activo() -> bool:
    """¿Está encendido el placeholder? ENCENDIDO desde el 11/08/2026.

    Nació apagado a propósito —cambia la utilidad— y se prendió recién con el
    dry-run aprobado por la dueña: última foto 10:58, utilidad 64.001,66,
    hilado 2.014.809,4 kg a 3,0443. Los cuatro despachos del día suman
    12.291,5 kg = 37.419,01, y ese es EXACTAMENTE el efecto de encenderlo.

    `HILO_PLACEHOLDER=0` lo apaga sin deploy, que es el motivo de que la llave
    siga existiendo: si algún día sostiene algo que no debía, se baja en el
    acto y la bodega vuelve a ser el saldo puro de Asinfo.
    """
    return os.environ.get("HILO_PLACEHOLDER", "1").strip() != "0"


def esperando_orden_kg() -> dict[int, float]:
    """{bodega: kg} que el balance sostiene hasta el corte. `{}` si está apagado.

    Devuelve `{}` —y no ceros— en tres casos que NO son lo mismo y conviene no
    confundir: apagado, pasada la hora de corte, o Asinfo mudo. En los tres el
    llamador suma cero, pero sólo el tercero merece el "último valor bueno".
    """
    if not placeholder_activo():
        return {}
    if _ahora_ec().hour >= _hora_corte():
        return {}                      # el corte del día ya pasó: se dan de baja
    out: dict[int, float] = {}
    # ⚠ dias=0 es HOY. Con dias=1 el rango arranca AYER a las 00:00 y el
    # placeholder sostendría despachos de otro día — justo lo que la dueña
    # descartó ("no vamos para atrás de agosto", y el corte es diario).
    for c in despachos_sin_of(dias=0):
        out[c["id_bodega"]] = out.get(c["id_bodega"], 0.0) + float(c["kg"] or 0)
    return out


#: "A PONCE PENDIENTE 180/C KW22" → "Ponce". Sólo la palabra en MAYÚSCULAS que
#: viene justo después de una "A " sola: es la forma en que la persona que
#: despacha escribe el destino, y la única pista que hay.
#:
#: 🚨 Deliberadamente conservador (dueña 2026-08-11: *"si decía Ponce, ponéselo;
#: si no, vacío"*). Asinfo NO guarda a quién se despachó —no hay destino, ni
#: transportista, ni centro de costo, está mirado— así que fuera de la glosa no
#: hay de dónde sacarlo. Dos de los seis despachos del día la tienen VACÍA, y
#: ahí el aviso no dice destino en vez de inventarlo.
_RE_DESTINO = re.compile(r"^A\s+([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ]{2,})\b")


def destino_de(glosa: str) -> str:
    """"A PONCE PENDIENTE 180/C" → "Ponce". "" cuando no se puede saber."""
    m = _RE_DESTINO.match((glosa or "").strip())
    return m.group(1).capitalize() if m else ""


def _titulo(caso: dict) -> str:
    """El texto que pidió la dueña, palabra por palabra (2026-08-11)."""
    from filters import num_es
    destino = destino_de(caso.get("descripcion") or "")
    a_quien = f" a {destino}" if destino else ""
    return (f"Salieron {num_es(caso['kg'], 0)} kg de {caso['material']}"
            f"{a_quien} — falta cargar orden de fabricación")


def _detalle(caso: dict) -> str:
    """Dos renglones y se acabó.

    🚨 TMT 2026-08-11, viendo la campanita: *"está muy largo el mensaje"*. La
    primera versión explicaba el mecanismo entero —qué es el material en
    proceso, por qué la bodega baja— arriba de un título que ya lo decía todo.
    En un buzón, lo que no se lee de un vistazo no se lee. Queda sólo lo que
    hace falta para ir a arreglarlo: cuál despacho, de quién, a qué hora.
    """
    partes = [caso["numero"]]
    if caso.get("descripcion"):
        partes.append(caso["descripcion"])
    if caso.get("creado"):
        partes.append(caso["creado"][-5:])       # la hora; el día ya está arriba
    return " · ".join(partes) + "\nCargale la orden en Asinfo y vuelven."


def ordenes_de(numeros: set[str]) -> dict[str, str]:
    """{OSM: OFT} de los despachos que YA tienen orden colgada. Fail-soft: {}."""
    from modules._lib import metabase_client

    limpios = [n.replace("'", "") for n in numeros if n]
    if not limpios:
        return {}
    lista = ", ".join(f"'{n}'" for n in limpios)
    sql = f"""
        SELECT osm.numero AS osm, MIN(ofr.numero) AS oft
          FROM orden_salida_material osm
          JOIN orden_fabricacion_orden_salida_material j
            ON j.id_orden_salida_material = osm.id_orden_salida_material
          JOIN orden_fabricacion ofr
            ON ofr.id_orden_fabricacion = j.id_orden_fabricacion
         WHERE osm.numero IN ({lista})
         GROUP BY osm.numero
    """
    try:
        rows = metabase_client.fetch_dataset(2, sql, max_results=500)
    except Exception as e:  # noqa: BLE001
        _LOG.warning("no pude leer las órdenes cargadas: %s", e)
        return {}
    return {str(r.get("osm") or "").strip(): str(r.get("oft") or "").strip()
            for r in rows or [] if r.get("osm")}


def _resolver_avisos(abiertos_ahora: set[str]) -> int:
    """Da vuelta los avisos de despachos que YA tienen su orden cargada.

    `abiertos_ahora` son los OSM que siguen sin orden. Todo aviso vivo cuya
    clave `hilo-sin-of:<osm>` no esté ahí describe algo que se arregló: pasa a
    ✅ con el número de la orden que lo resolvió.

    🚨 Esto se consulta DIRECTO y no por `avisos.listar()`. La primera versión
    usaba `listar(fuente="stock")` y no resolvía nunca: `listar` no devuelve la
    columna `clave` — devuelve id, fuente, nivel, título, detalle, importe,
    cantidad y poco más—, así que la comparación se hacía siempre contra un
    string vacío y ningún aviso matcheaba. Fallaba en silencio, que es lo peor:
    la campanita seguía en ⚠ y nada en los logs. Antes de leer un campo de un
    helper ajeno, mirar su SELECT.

    `nivel <> 'ok'` hace de freno: los ya resueltos no se vuelven a tocar.
    """
    import db

    try:
        vivos = db.fetch_all(
            """
            SELECT id_aviso, clave, cantidad
              FROM scintela.aviso
             WHERE clave LIKE 'hilo-sin-of:%%'
               AND nivel <> 'ok'
            """
        ) or []
    except Exception as e:  # noqa: BLE001 -- nunca frena el ciclo
        _LOG.warning("no pude leer los avisos abiertos: %s", e)
        return 0

    mios = {}
    for a in vivos:
        clave = str(a.get("clave") or "")
        # El prefijo se vuelve a chequear en Python aunque el SQL ya filtre:
        # el día que alguien reuse esta función con otro WHERE, `split(":")`
        # sobre una clave ajena devolvería basura y la daría por nuestra.
        if not clave.startswith("hilo-sin-of:"):
            continue
        osm = clave.split(":", 1)[1].strip()
        if osm and osm not in abiertos_ahora:
            mios[osm] = a
    if not mios:
        return 0

    from modules.avisos import queries as avisos

    ofts = ordenes_de(set(mios))
    n = 0
    for osm, a in mios.items():
        oft = ofts.get(osm)
        titulo = (f"{_kg_txt(a.get('cantidad') or 0)} kg de hilo — "
                  "orden cargada")
        detalle = f"{osm} → {oft}" if oft else osm
        try:
            if avisos.resolver(int(a["id_aviso"]), titulo=titulo,
                               detalle=detalle):
                n += 1
        except Exception as e:  # noqa: BLE001
            _LOG.warning("no pude resolver %s: %s", osm, e)
    if n:
        _LOG.info("hilo sin orden: %s aviso(s) resueltos", n)
    return n


def _kg_txt(kg) -> str:
    from filters import num_es
    return num_es(kg, 0)


def revisar_si_toca() -> dict:
    """Corre cada `_FRENO_SECS` y deja un aviso por despacho. Nunca levanta."""
    global _ultima_corrida
    if os.environ.get("HILO_SIN_OF", "1").strip() == "0":
        return {"corrio": False, "motivo": "apagado"}
    ahora = _t.time()
    with _lock:
        if (ahora - _ultima_corrida) < _FRENO_SECS:
            return {"corrio": False, "motivo": "freno"}
        _ultima_corrida = ahora

    try:
        casos = despachos_sin_of()
    except Exception as e:  # noqa: BLE001
        _LOG.warning("revisión falló: %s", e)
        return {"corrio": True, "avisados": 0, "error": str(e)[:200]}

    from modules.avisos import queries as avisos

    # Primero cerrar los que se arreglaron: el aviso viejo dejó de ser cierto.
    resueltos = _resolver_avisos({c["numero"] for c in casos if c["numero"]})

    n = 0
    for c in casos:
        if not c["numero"]:
            continue
        if avisos.avisar(
            fuente="stock",
            nivel="alerta",
            titulo=_titulo(c)[:200],
            detalle=_detalle(c),
            cantidad=int(c["kg"]),
            url="/stock/fabricacion-tc",
            clave=f"hilo-sin-of:{c['numero']}",
        ):
            n += 1
    if n:
        _LOG.info("hilo sin orden de fabricación: %s aviso(s) nuevos de %s caso(s)",
                  n, len(casos))
    return {"corrio": True, "casos": len(casos), "avisados": n,
            "resueltos": resueltos}
