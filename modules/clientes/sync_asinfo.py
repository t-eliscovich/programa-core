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
  Tampoco `correo` (ya existe el espejo `cliente_mail_asinfo`, con su propio
  filtro de mails de la casa), ni observación/pago/stop.
- **VENDEDOR y DIRECCIÓN: manda ASINFO** (TMT 2026-08-27, Tamara: *"los
  clientes en Asinfo a veces cambian de dirección y de vendedor — lo tiene
  que agarrar"*). Esto REVIERTE la regla del 05/08 que dejaba `vend` y
  dirección quietos:
  · La dirección SÍ está en Asinfo — la medición del 05/08 ("0 de 3.635")
    miró `direccion_empresa.descripcion`, que está vacía; el texto real vive
    en `ubicacion.direccion1` vía `direccion_empresa.id_ubicacion` (medido
    27/08: 3.641 de 3.649 la tienen). Se pisa `direccion1`; si Asinfo no
    tiene, la ficha queda como está.
  · PROVINCIA y CANTÓN (agregados el mismo 27/08, segunda pasada): salen de
    `direccion_empresa.id_ciudad` → `ciudad` (el cantón) → `provincia`. Lo
    de PC venía del dBase truncado a ~10 letras y con typos — tanto que el
    reporte por grupos tiene `_normalizar_provincia()` para adivinarla. Se
    pisan como la dirección; PC guarda varchar(50) y Asinfo mide ≤32.
  · El vendedor vive en `cliente.id_agente_comercial` → `usuario.codigo`,
    y los códigos NO coinciden solos con el `vend` de PC: hay mapa
    (`_VEND_ASINFO_A_PC` — DEB es BED, DENNYS es DJA, ESTEFY es EVB, y
    varios llevan prefijo `V-`). Un código que no está en el mapa no se
    interpreta (misma filosofía que las listas de descuento): se lista y
    se avisa. Cada cambio guarda el valor anterior en `vend_cambiado`.
- **Teléfono: PC gana.** 2.640 de 3.635 teléfonos de Asinfo son basura
  (`2222222` o <7 dígitos) y PC tiene 3.833 reales. Sólo se RELLENA el
  teléfono cuando PC está vacío y el de Asinfo parece real.
- **DESCUENTO: manda ASINFO** (TMT 2026-08-25, dueña: *"el descuento que vale
  es el que está en Asinfo, que haga override del que ya está"*). Asinfo lo
  tiene en una LISTA por cliente (`cliente.id_lista_descuentos` →
  `lista_descuentos.nombre`, con nombres tipo `5%y7%`). El primer tramo es el
  5% de contado, igual para todos; el SEGUNDO es el descuento del cliente, y
  es el que PC guarda en `cliente.descuento`. Medido el 25/08 contra la base
  en vivo: de los 3.644 códigos que cruzan, 3.333 ya coincidían (96%), 166
  estaban vacíos y 145 diferían.
  Primero se decidió rellenar-sin-pisar y listar las diferencias; con las 145
  a la vista la dueña cambió a **pisar siempre**, el mismo día. Lo que Asinfo
  no sabe (lista ausente o con otro formato) SIGUE sin tocarse, y cada cambio
  queda registrado con su valor anterior en el log y en la pantalla.
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

Corre solo cada hora de 07:00 a 19:00 EC (TMT 2026-08-27 — antes eran dos
ventanas, 11:00 y 16:00, y los cambios de Asinfo llegaban tarde), y a
demanda desde la pantalla /clientes/sync-asinfo.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time as _time
from datetime import UTC, datetime, timedelta
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
       LTRIM(RTRIM(COALESCE(ld.nombre, '')))                 AS lista_desc,
       LTRIM(RTRIM(COALESCE(u.codigo, '')))                  AS agente,
       LTRIM(RTRIM(COALESCE(ub.direccion1, '')))             AS dir1,
       LTRIM(RTRIM(COALESCE(pr.nombre, '')))                 AS provincia,
       LTRIM(RTRIM(COALESCE(ci.nombre, '')))                 AS canton
  FROM empresa e
  LEFT JOIN direccion_empresa d
         ON d.id_empresa = e.id_empresa
        AND d.indicador_direccion_principal = 1
        AND d.activo = 1
  LEFT JOIN ubicacion ub
         ON ub.id_ubicacion = d.id_ubicacion
  LEFT JOIN ciudad ci
         ON ci.id_ciudad = d.id_ciudad
  LEFT JOIN provincia pr
         ON pr.id_provincia = ci.id_provincia
  LEFT JOIN cliente cl
         ON cl.id_empresa = e.id_empresa
  LEFT JOIN lista_descuentos ld
         ON ld.id_lista_descuentos = cl.id_lista_descuentos
  LEFT JOIN usuario u
         ON u.id_usuario = cl.id_agente_comercial
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

    Manda Asinfo: si dice algo distinto de lo que hay en la ficha, se escribe.
    Lo único que NO se toca es lo que Asinfo no sabe (sin lista, o una lista
    con un nombre que no se entiende): ahí `asi_val` viene en None y la ficha
    queda como está.
    """
    if asi_val is None:
        return None
    if pc_val is None:
        return float(asi_val)
    if float(pc_val) == float(asi_val):
        return None
    return float(asi_val)


#: Códigos de agente comercial de Asinfo (`usuario.codigo`) → el `vend` de
#: PC. NO coinciden solos (medido en vivo 27/08/2026): Asinfo le pone `V-`
#: a varios, a Héctor Bedón lo llama `DEB` cuando PC lo llama `BED` (852 de
#: 869 clientes de DEB ya estaban como BED), a Dennys Jaramillo `DENNYS`
#: (PC: DJA) y a Estefanía `ESTEFY` (PC: EVB). Un código que no está acá no
#: se interpreta: se lista en `agentes_raros` y se avisa — preferimos no
#: tocar antes que adivinar (misma filosofía que las listas de descuento).
_VEND_ASINFO_A_PC = {
    "PPR": "PPR", "SEP": "SEP", "V-SEP": "SEP", "V-JQU": "JQU",
    "V-EDG": "EDG", "V-RMY": "RMY", "V-FL1": "FL1",
    "DEB": "BED", "DENNYS": "DJA", "ESTEFY": "EVB",
    "EDU": "EDU", "DAN": "DAN",
}

#: El agente 951 de Asinfo es "Intela Cía. Ltda." — la casa, no un vendedor.
_AGENTE_LA_CASA = "INT"


def vend_de_asinfo(agente: str | None) -> tuple[str | None, bool]:
    """`(vend de PC, entendido)`. La casa (INT) es entendido con vend ''.

    '' significa "sin vendedor"; None con entendido=False es "no sé quién
    es" y la ficha no se toca.
    """
    cod = (agente or "").strip().upper()
    if not cod:
        return None, False
    if cod == _AGENTE_LA_CASA:
        return "", True
    pc = _VEND_ASINFO_A_PC.get(cod)
    return (pc, True) if pc else (None, False)


def _vend_a_escribir(pc_vend: str | None, agente: str | None) -> str | None:
    """El vend que hay que GRABAR ('' = quitar), o None si no se toca."""
    nuevo, entendido = vend_de_asinfo(agente)
    if not entendido:
        return None
    actual = (pc_vend or "").strip().upper()
    if nuevo == "":
        # La casa NUNCA borra al vendedor de la ficha — decisión de Tamara
        # (27/08/2026, con los 210 casos medidos a la vista): los clientes
        # con X son de Intela, los de DJA también, los de los vendedores son
        # de los vendedores, y BED agrupa sus clientes aparte sin perfil de
        # vendedor. Si un cliente pasa DE VERDAD a la casa, el vendedor se
        # saca a mano en la ficha: el sync no lo hace.
        return None
    return nuevo if actual != nuevo else None


def _norm_dir(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).upper()


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
    dup_asinfo: [cod], descuentos_puestos, descuentos_pisados,
    desc_cambiado: [...], listas_raras: [...], vend_cambiado: [...],
    agentes_raros: [...], dir_cambiado: [...], sin_tocar,
    leidas_de_asinfo, error?}.
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
            "agente": str(f.get("agente") or "").strip()[:30],
            "dir": str(f.get("dir1") or "").strip()[:200],
            "provincia": str(f.get("provincia") or "").strip()[:50],
            "canton": str(f.get("canton") or "").strip()[:50],
        }
    # Un código que Asinfo tiene repetido no se puede sincronizar: no hay
    # forma de saber cuál de las dos empresas es "la" del código.
    for cod in duplicados:
        por_cod.pop(cod, None)

    # ── Lado PC ─────────────────────────────────────────────────────────
    pc_rows = db.fetch_all(
        "SELECT id_cliente, UPPER(TRIM(codigo_cli)) AS cod, nombre, ruc, "
        "       telefono, descuento, vend, direccion1, provincia, canton "
        "FROM scintela.cliente"
    ) or []
    pc_por_cod = {r["cod"]: r for r in pc_rows if r.get("cod")}
    pc_por_ruc: dict[str, list[str]] = {}
    for r in pc_rows:
        clave = ruc10(r.get("ruc"))
        if clave and r.get("cod"):
            pc_por_ruc.setdefault(clave, []).append(r["cod"])

    # ── Fichas existentes: pisar nombre/RUC/descuento/vendedor/dirección,
    #    rellenar teléfono ────────────────────────────────────────────────
    cambios: list[tuple] = []  # (cod, nombre, ruc, tel, desc, vend, dir) — None = no tocar
    desc_cambiado: list[dict] = []
    listas_raras: list[dict] = []
    vend_cambiado: list[dict] = []
    agentes_raros: list[dict] = []
    dir_cambiado: list[dict] = []
    geo_cambiado: list[dict] = []
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
        if desc_nuevo is not None:
            # queda el valor ANTERIOR: es la única forma de volver atrás uno
            # que se haya pisado mal, y de leer la corrida después
            desc_cambiado.append({
                "cod": cod, "nombre": (p.get("nombre") or "")[:60],
                "antes": None if p.get("descuento") is None else float(p["descuento"]),
                "ahora": float(desc_nuevo),
            })
        vend_nuevo = _vend_a_escribir(p.get("vend"), a["agente"])
        _mapeado, entendido = vend_de_asinfo(a["agente"])
        if not entendido and a["agente"]:
            agentes_raros.append({"cod": cod, "agente": a["agente"]})
        if vend_nuevo is not None:
            # mismo criterio que el descuento: el valor anterior queda
            vend_cambiado.append({
                "cod": cod, "nombre": (p.get("nombre") or "")[:60],
                "antes": ((p.get("vend") or "").strip().upper() or None),
                "ahora": vend_nuevo or None,
            })
        dir_nueva = a["dir"] if (
            a["dir"] and _norm_dir(a["dir"]) != _norm_dir(p.get("direccion1"))
        ) else None
        if dir_nueva is not None:
            dir_cambiado.append({
                "cod": cod,
                "antes": ((p.get("direccion1") or "").strip() or None),
                "ahora": dir_nueva,
            })
        # Provincia y cantón: mismas reglas que la dirección (27/08, segunda
        # pasada del día). Lo de PC venía del dBase truncado a ~10 letras.
        prov_nueva = a["provincia"] if (
            a["provincia"] and _norm_dir(a["provincia"]) != _norm_dir(p.get("provincia"))
        ) else None
        canton_nuevo = a["canton"] if (
            a["canton"] and _norm_dir(a["canton"]) != _norm_dir(p.get("canton"))
        ) else None
        for campo, nuevo, viejo in (("provincia", prov_nueva, p.get("provincia")),
                                    ("canton", canton_nuevo, p.get("canton"))):
            if nuevo is not None:
                geo_cambiado.append({
                    "cod": cod, "campo": campo,
                    "antes": ((viejo or "").strip() or None), "ahora": nuevo,
                })
        if (nombre_nuevo or ruc_nuevo or tel_nuevo or desc_nuevo is not None
                or vend_nuevo is not None or dir_nueva is not None
                or prov_nueva is not None or canton_nuevo is not None):
            cambios.append((cod, nombre_nuevo, ruc_nuevo, tel_nuevo, desc_nuevo,
                            vend_nuevo, dir_nueva, prov_nueva, canton_nuevo))

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
    _avisar_descuentos(desc_cambiado, listas_raras)
    _avisar_vendedores(vend_cambiado, agentes_raros)

    reporte = {
        "ok": True,
        "leidas_de_asinfo": len(filas),
        "clientes_asinfo": len(por_cod),
        "actualizados": actualizados,
        "altas": sorted(altas),
        "conflictos": conflictos,
        "dup_asinfo": sorted(duplicados),
        "descuentos_puestos": sum(1 for c in cambios if c[4] is not None),
        # "pisado" = tenía un descuento de verdad. Vacío y CERO son huecos:
        # llenar un cero no es pisarle el descuento a nadie.
        "descuentos_pisados": sum(1 for d in desc_cambiado if d["antes"]),
        "desc_cambiado": sorted(desc_cambiado, key=lambda d: d["cod"]),
        "listas_raras": sorted(listas_raras, key=lambda d: d["cod"]),
        "vend_cambiado": sorted(vend_cambiado, key=lambda d: d["cod"]),
        "agentes_raros": sorted(agentes_raros, key=lambda d: d["cod"]),
        "direcciones_cambiadas": len(dir_cambiado),
        "dir_cambiado": sorted(dir_cambiado, key=lambda d: d["cod"]),
        "geo_cambiadas": len(geo_cambiado),
        "geo_cambiado": sorted(geo_cambiado, key=lambda d: (d["cod"], d["campo"])),
        "sin_tocar": len(por_cod) - len(cambios) - len(altas) - len(conflictos),
    }
    _guardar_log(usuario, reporte)
    return reporte


def _aplicar_cambios(cambios: list[tuple], usuario: str) -> int:
    """UN solo UPDATE con VALUES — un viaje por fila a RDS ya tiró un 502
    en el cron de mails (TMT 2026-08-03); acá el primer pase toca ~3.400."""
    marcadores = ",".join(["(%s, %s, %s, %s, %s, %s, %s, %s, %s)"] * len(cambios))
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
               vend     = COALESCE(v.vend, c.vend),
               direccion1 = COALESCE(v.direccion1, c.direccion1),
               provincia = COALESCE(v.provincia, c.provincia),
               canton   = COALESCE(v.canton, c.canton),
               fecha_modifica   = CURRENT_TIMESTAMP,
               usuario_modifica = %s
          FROM (VALUES {marcadores})
               AS v(cod, nombre, ruc, telefono, descuento, vend, direccion1,
                    provincia, canton)
         WHERE UPPER(TRIM(c.codigo_cli)) = v.cod
        """,
        tuple([usuario[:60], *planos]),
    )


def _alta(a: dict, usuario: str) -> bool:
    """INSERT del cliente nuevo + campanita para lo que Asinfo no sabe."""
    from modules.avisos.queries import avisar
    from modules.clientes import queries as cli_q

    vend_alta, _entendido = vend_de_asinfo(a.get("agente"))
    try:
        cli_q.crear(
            codigo_cli=a["cod"], nombre=a["nombre"], ruc=a["ruc"] or None,
            telefono=a["tel"] or None, descuento=a.get("desc"),
            vend=vend_alta or None, direccion1=a.get("dir") or None,
            provincia=a.get("provincia") or None,
            canton=a.get("canton") or None,
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


def _avisar_descuentos(cambiado: list[dict], raras: list[dict]) -> None:
    """UNA campanita por cada cosa, no una por cliente.

    El día que se estrenó el override fueron 145 fichas de una: una campanita
    por cliente sería un buzón inservible. El número va en el título y el
    detalle está en la pantalla del sync. `clave` con la fecha adentro para
    que avise UNA vez por día y no repita en la corrida de la tarde.
    """
    from modules.avisos.queries import avisar

    pisados = [d for d in cambiado if d["antes"]]
    if pisados:
        hoy = datetime.now(UTC).strftime("%Y%m%d")
        cods = ", ".join(d["cod"] for d in sorted(pisados, key=lambda d: d["cod"])[:8])
        avisar(
            fuente="clientes", nivel="alerta",
            titulo=f"Asinfo cambió el descuento de {len(pisados)} clientes",
            detalle=f"Se pisó el que tenía la ficha. {cods}"[:200],
            url="/clientes/sync-asinfo",
            clave=f"clientes-desc-pisados-{hoy}",
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


def _avisar_vendedores(cambiado: list[dict], raros: list[dict]) -> None:
    """Mismo criterio que los descuentos: UNA campanita por día, con el
    número en el título y el detalle (con el valor anterior) en la pantalla
    del sync — que es la forma de volver atrás uno pisado por error."""
    from modules.avisos.queries import avisar

    pisados = [d for d in cambiado if d["antes"]]
    if pisados:
        hoy = datetime.now(UTC).strftime("%Y%m%d")
        cods = ", ".join(d["cod"] for d in sorted(pisados, key=lambda d: d["cod"])[:8])
        avisar(
            fuente="clientes", nivel="alerta",
            titulo=f"Asinfo cambió el vendedor de {len(pisados)} clientes",
            detalle=f"Se pisó el que tenía la ficha. {cods}"[:200],
            url="/clientes/sync-asinfo",
            clave=f"clientes-vend-pisados-{hoy}",
        )
    if raros:
        cods = ", ".join(f"{r['cod']} ({r['agente']})" for r in raros[:5])
        avisar(
            fuente="clientes", nivel="alerta",
            titulo=f"{len(raros)} vendedores de Asinfo que no conozco",
            detalle=f"No se tocó el vendedor de: {cods}"[:200],
            url="/clientes/sync-asinfo",
            clave=f"clientes-agentes-raros-{len(raros)}",
        )


def _para_log(reporte: dict) -> dict:
    """El reporte, recortado para que entre en el log SIN romper el JSON.

    Se guarda como texto y se relee con `json.loads`: cortar el STRING a lo
    bruto dejaría un JSON inválido y la corrida se vería vacía en la pantalla.
    Por eso se recortan las LISTAS largas, no el texto.
    """
    chico = dict(reporte)
    for campo in ("desc_cambiado", "listas_raras", "conflictos",
                  "vend_cambiado", "agentes_raros", "dir_cambiado",
                  "geo_cambiado"):
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
# ACÁ se decide si toca. Ventanas: CADA HORA de 07:00 a 19:00 Ecuador
# (TMT 2026-08-27, Tamara: los cambios de dirección y vendedor en Asinfo
# tienen que llegar más seguido; antes eran sólo 11:00 y 16:00).
#
# El guard de "ya corrió en esta ventana" NO es en memoria: mira el log
# (`clientes_sync_asinfo_log`), así un restart del server no lo repite y una
# corrida MANUAL dentro de la ventana también cuenta. SYNC_CLIENTES_AUTO=0
# lo apaga.
# ---------------------------------------------------------------------------

_VENTANA_EC_DESDE = 7    # primera corrida del día: 07:00 EC
_VENTANA_EC_HASTA = 19   # última: 19:00 EC (a la noche no cambia nada)
_CHECK_MIN_SECS = 300    # mirar el log a lo sumo cada 5 min
_auto_lock = threading.Lock()
_auto_ultimo_check = 0.0


def _inicio_ventana_utc(ahora_utc: datetime) -> datetime | None:
    """El comienzo (en UTC) de la ventana horaria vigente, o None de noche.

    Se calcula en hora Ecuador (UTC−5) y se vuelve a UTC sumando 5 h — ojo
    que la ventana de las 19:00 EC ya es el día SIGUIENTE en UTC, por eso
    no sirve el `replace(hour=...)` sobre la hora UTC que se usaba cuando
    las ventanas eran dos.
    """
    ahora_ec = ahora_utc - timedelta(hours=5)
    if not (_VENTANA_EC_DESDE <= ahora_ec.hour <= _VENTANA_EC_HASTA):
        return None
    inicio_ec = ahora_ec.replace(minute=0, second=0, microsecond=0)
    return inicio_ec + timedelta(hours=5)


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
