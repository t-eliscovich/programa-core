"""Proformas (cotizaciones a clientes).

scintela.proforma_cabecera: id_proforma, id_cliente, fecha_emision, subtotal,
   porcentaje_descuento_volumen, monto_descuento_volumen, subtotal_con_descuento,
   aplica_descuento_contado, monto_descuento_contado, total_final, observaciones

scintela.proforma_detalle: id_detalle, id_proforma, id_subcategoria_producto,
   nombre_producto, color, clase, cantidad_kilos, precio_unitario, precio_total

Alta (cotización) — réplica del flujo de FACTURAR.PRG del dBase, pero SOLO para
cotizar (no factura, no stock, no contabilidad):
  - Cada línea: Tipo (una de las 12 telas de scintela.precios) + Clase de color
    (1..5 = BLANCO/BAJOS/MEDIOS/JASPEADOS/FUERTES) → precio de lista sugerido
    desde la matriz de precios (editable) → Kg → Importe = Kg × Precio.
  - Al final, descuento por VOLUMEN (%) y por CONTADO (%) EN CASCADA, igual que
    PROCEDURE FACTURO (primero volumen, después contado sobre el ya rebajado).
"""
import db
from modules._lib import busqueda

# Las 12 telas de la matriz de precios, en el orden del dBase (= precios.TELAS).
# (columna en scintela.precios, etiqueta que ve el usuario / nombre_producto).
TELAS: list[tuple[str, str]] = [
    ("jersey", "JERSEY"),
    ("pique", "PIQUE"),
    ("toper", "TOPER"),
    ("alemania", "ALEMANIA"),
    ("rib", "RIB"),
    ("cuellos", "CUELLOS"),
    ("lycra", "LYCRA"),
    ("falso", "FLEECE"),
    ("kiana", "KIANA"),
    # TMT 2026-08-04, dueña: "borrar medical". La tela salió de la lista de
    # precios (pantalla, hoja impresa y proforma). La COLUMNA `medical` de
    # scintela.precios NO se toca: los precios cargados siguen ahí y para
    # volver a ofrecerla alcanza con descomentar esta línea.
    # ("medical", "MEDICAL"),
    ("micro", "MICRO"),
    ("james", "JAMES"),
]
COLUMNAS_TELA: set[str] = {col for col, _ in TELAS}
_LABEL_POR_COL: dict[str, str] = {col: lab for col, lab in TELAS}


def matriz_precios() -> dict:
    """Clases de color + precio de lista por (clase, tela), para el buscador de
    precios del formulario. Estructura lista para serializar a JSON:

        {
          "clases": [{"clase": 1, "descripcio": "BLANCO"}, ...],
          "telas":  [{"col": "jersey", "label": "JERSEY"}, ...],
          "precios": {"1": {"jersey": 9.12, ...}, ...},
          "iva_pct": 15.0,
        }

    🚨 Los precios salen de acá CON IVA. En scintela.precios están guardados
    NETOS, pero la proforma es el papel que ve el CLIENTE y tiene que hablar
    el mismo idioma que la hoja de precios que se le entrega (dueña
    2026-08-05: "en la proforma está reflejando los valores sin IVA" → "con
    IVA siempre"). El IVA lo pone `precios.precio_con_iva`, nunca un `* 1.15`
    escrito acá.
    """
    from modules.precios import queries as precios_queries

    cols = ", ".join(col for col, _ in TELAS)
    filas = db.fetch_all(
        f"SELECT clase, descripcio, {cols} FROM scintela.precios ORDER BY clase ASC"
    ) or []
    clases = [{"clase": int(f["clase"]), "descripcio": f["descripcio"]} for f in filas]
    precios: dict[str, dict] = {}
    for f in filas:
        precios[str(int(f["clase"]))] = {
            col: (
                round(precios_queries.precio_con_iva(float(f[col])), 4)
                if f.get(col) is not None
                else None
            )
            for col, _ in TELAS
        }
    return {
        "clases": clases,
        "telas": [{"col": c, "label": lab} for c, lab in TELAS],
        "precios": precios,
        "planos": telas_planas(),
        "iva_pct": precios_queries.IVA_PCT,
    }


def telas_planas() -> list[dict]:
    """Telas de PRECIO ÚNICO (scintela.precio_plano, migración 0159).

    Pedido de la dueña 2026-08-04 ("a proforma y precios agregar esto"): son
    telas que no tienen precio por clase de color. Dos formas:
      - `precio` → misma cifra para todo color.
      - `col` (JERSEY 3,5 / JERSEY 3) → cobran lo que esa columna de la
        matriz, así que el precio SÍ varía por clase y sale de PRECIOS.
    Fail-soft: si la tabla no está, la proforma sigue andando con las 12 de
    siempre.

    🚨 `precio` sale de acá CON IVA, igual que la matriz de `matriz_precios`
    — en la tabla está guardado NETO. Las dos ramas tienen que estar en la
    MISMA escala o el mismo formulario cotizaría unas telas con IVA y otras
    sin.
    """
    from modules.precios import queries as precios_queries

    try:
        filas = precios_queries.precio_plano()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for f in filas:
        out.append(
            {
                "v": "pp_" + str(f["id"]),
                "label": str(f["tela"]).upper(),
                "col": f.get("ref_col"),
                "precio": (
                    round(precios_queries.precio_con_iva(float(f["precio"])), 4)
                    if f.get("precio") is not None and not f.get("ref_col")
                    else None
                ),
            }
        )
    return out


_CLASE_DESC: dict[int, str] = {
    1: "BLANCO", 2: "BAJOS", 3: "MEDIOS", 4: "JASPEADOS", 5: "FUERTES",
}


def colores_catalogo() -> list[dict]:
    """Catálogo de colores con su CLASE de precio (1..5), desde
    scintela.tinto_costos — la réplica de COSTOS.DBF (el mismo archivo que lee
    el dBase). El COLOR determina la CLASE (autoritativo; incluye MEDIOS, que
    formulas no distingue). Fail-soft: si falla devuelve [].

    Devuelve [{cod, color, clase, clase_desc}] ordenado por color.
    """
    try:
        rows = db.fetch_all(
            "SELECT cod, color, clase FROM scintela.tinto_costos "
            "WHERE clase BETWEEN 1 AND 5 "
            "  AND color IS NOT NULL AND TRIM(color) <> '' "
            "ORDER BY color"
        )
    except Exception:
        rows = []
    out: list[dict] = []
    for r in rows or []:
        color = (r.get("color") or "").strip()
        if not color:
            continue
        clase = int(r["clase"])
        out.append({
            "cod": (r.get("cod") or "").strip(),
            "color": color,
            "clase": clase,
            "clase_desc": _CLASE_DESC.get(clase, ""),
        })
    return out


def cliente_defaults(codigo_cli: str) -> dict | None:
    """Datos del cliente para prellenar la cotización: id, nombre y los
    descuentos que el dBase aplica solo (DESCUENTO por volumen, y 5% de contado
    si PAGO='C'). Devuelve None si el cliente no existe.
    """
    codigo_cli = (codigo_cli or "").upper().strip()
    if not codigo_cli:
        return None
    row = db.fetch_one(
        """
        SELECT id_cliente, codigo_cli, nombre, descuento, pago
          FROM scintela.cliente WHERE codigo_cli = %s
        """,
        (codigo_cli,),
    )
    if not row:
        return None
    desc_raw = str(row.get("descuento") or "").strip().replace(",", ".")
    try:
        desc = float(desc_raw) if desc_raw else 0.0
    except ValueError:
        desc = 0.0
    pago = str(row.get("pago") or "").strip().upper()
    return {
        "id_cliente": row["id_cliente"],
        "codigo_cli": row["codigo_cli"],
        "nombre": row["nombre"],
        "descuento_volumen": desc,
        "aplica_contado": pago.startswith("C"),
    }


def calcular_totales(
    lineas: list[dict],
    pct_volumen: float = 0.0,
    aplica_contado: bool = True,
    pct_contado: float = 5.0,
    flete: float = 0.0,
) -> dict:
    """La cascada de descuentos, función PURA (no toca la DB).

        subtotal        = Σ (kg × precio)
        desc. contado   = subtotal × 5%            [siempre, dueña 2026-07-09]
        subtotal c/desc = subtotal − desc. contado
        desc. volumen   = (subtotal c/desc) × pct_volumen/100
        total           = subtotal c/desc − desc. volumen + flete

    ⚠️ El ORDEN es CONTADO primero y volumen después, que es el de la pantalla
    y el del papel que recibe el cliente. Hasta el 2026-08-11 esta función lo
    hacía al revés (volumen → contado, como PROCEDURE FACTURO del dBase)
    mientras la pantalla ya hacía contado → volumen. Nunca se notó porque la
    cascada es conmutativa: el TOTAL da igual en los dos órdenes. Lo que
    cambiaba era el desglose — cuánto se le atribuye a cada descuento — y eso
    empieza a importar ahora que se GUARDA: el renglón grabado tiene que decir
    lo mismo que el papel. El flete tampoco existía acá (es de la Factura
    Proforma) y se sumaba sólo en el browser.
    """
    subtotal = 0.0
    for ln in lineas:
        kg = float(ln.get("cantidad_kilos") or 0)
        pu = float(ln.get("precio_unitario") or 0)
        subtotal += kg * pu
    subtotal = round(subtotal, 2)

    monto_contado = 0.0
    if aplica_contado:
        pct_contado = max(0.0, float(pct_contado or 0))
        monto_contado = round(subtotal * pct_contado / 100.0, 2)
    subtotal_desc = round(subtotal - monto_contado, 2)

    pct_volumen = max(0.0, float(pct_volumen or 0))
    monto_vol = round(subtotal_desc * pct_volumen / 100.0, 2)

    flete = round(float(flete or 0), 2)
    total = round(subtotal_desc - monto_vol + flete, 2)

    return {
        "subtotal": subtotal,
        "porcentaje_descuento_volumen": round(pct_volumen, 2),
        "monto_descuento_volumen": monto_vol,
        "subtotal_con_descuento": subtotal_desc,
        "aplica_descuento_contado": bool(aplica_contado),
        "monto_descuento_contado": monto_contado,
        "flete": flete,
        "total_final": total,
    }


def _lineas_limpias(lineas: list[dict]) -> list[dict]:
    """Normaliza las líneas que manda la pantalla y calcula el importe de cada
    una. Compartida por `crear` y `actualizar` — la misma proforma tiene que
    quedar igual se grabe por donde se grabe.
    """
    limpias: list[dict] = []
    for ln in lineas:
        kg = float(ln.get("cantidad_kilos") or 0)
        pu = float(ln.get("precio_unitario") or 0)
        if kg == 0 and pu == 0:
            continue  # línea vacía — la ignoramos (como el KG=0 del dBase)
        tela = (ln.get("tela") or "").strip().lower()[:20]
        nombre = (ln.get("nombre_producto") or _LABEL_POR_COL.get(tela, "") or "").strip()
        clase = ln.get("clase")
        try:
            clase = int(clase) if clase not in (None, "") else None
        except (TypeError, ValueError):
            clase = None
        cant = ln.get("cantidad")
        try:
            cant = round(float(cant), 2) if cant not in (None, "") else None
        except (TypeError, ValueError):
            cant = None
        limpias.append({
            "tela": tela or None,
            "nombre_producto": nombre[:60],
            "color": (ln.get("color") or "").strip()[:60],
            "clase": clase,
            "cantidad": cant,
            "cantidad_kilos": round(kg, 2),
            "precio_unitario": round(pu, 4),
            "precio_total": round(kg * pu, 2),
        })
    if not limpias:
        raise ValueError("La proforma no tiene ninguna línea con datos.")
    return limpias


_SQL_INSERT_DETALLE = """
    INSERT INTO scintela.proforma_detalle
        (id_proforma, tela, nombre_producto, color, clase,
         cantidad, cantidad_kilos, precio_unitario, precio_total)
    VALUES (%(id)s, %(tela)s, %(nombre)s, %(color)s, %(clase)s,
            %(cant)s, %(kg)s, %(pu)s, %(pt)s)
"""


def _params_detalle(id_proforma: int, ln: dict) -> dict:
    return {
        "id": id_proforma,
        "tela": ln["tela"],
        "nombre": ln["nombre_producto"],
        "color": ln["color"],
        "clase": ln["clase"],
        "cant": ln["cantidad"],
        "kg": ln["cantidad_kilos"],
        "pu": ln["precio_unitario"],
        "pt": ln["precio_total"],
    }


def crear(
    *,
    codigo_cli: str,
    fecha,
    lineas: list[dict],
    pct_volumen: float = 0.0,
    aplica_contado: bool = True,
    pct_contado: float = 5.0,
    flete: float = 0.0,
    es_pedido: bool = False,
    id_origen: int | None = None,
    observaciones: str | None = None,
    usuario: str = "web",
) -> dict:
    """Inserta una proforma (cabecera + detalle) en una sola transacción.

    `lineas` = [{tela, nombre_producto, color, clase, cantidad, cantidad_kilos,
    precio_unitario}, ...]. El importe por línea y los totales se calculan acá
    (no se confía en lo que mande el browser). Devuelve {id_proforma}.

    `id_origen` = de qué proforma viene esta versión, cuando se reabrió una
    guardada y se le cambió algo (Alex 2026-08-11: el cliente pide agregar o
    sacar). Se guarda una NUEVA y la anterior queda como estaba — el papel
    viejo ya está en manos del cliente.
    """
    cli = cliente_defaults(codigo_cli)
    if not cli:
        raise ValueError(f"El cliente {codigo_cli!r} no existe.")

    limpias = _lineas_limpias(lineas)

    tot = calcular_totales(limpias, pct_volumen, aplica_contado, pct_contado, flete)

    with db.tx() as conn:
        cab = db.execute_returning(
            """
            INSERT INTO scintela.proforma_cabecera
                (id_cliente, fecha_emision, subtotal,
                 porcentaje_descuento_volumen, monto_descuento_volumen,
                 subtotal_con_descuento, aplica_descuento_contado,
                 monto_descuento_contado, flete, total_final, es_pedido,
                 id_origen, observaciones, usuario_crea)
            VALUES (%(id_cliente)s, %(fecha)s, %(subtotal)s,
                    %(pct_vol)s, %(monto_vol)s, %(sub_desc)s, %(aplica)s,
                    %(monto_cont)s, %(flete)s, %(total)s, %(es_pedido)s,
                    %(id_origen)s, %(obs)s, %(usuario)s)
            RETURNING id_proforma
            """,
            {
                "id_cliente": cli["id_cliente"],
                "fecha": fecha,
                "subtotal": tot["subtotal"],
                "pct_vol": tot["porcentaje_descuento_volumen"],
                "monto_vol": tot["monto_descuento_volumen"],
                "sub_desc": tot["subtotal_con_descuento"],
                "aplica": tot["aplica_descuento_contado"],
                "monto_cont": tot["monto_descuento_contado"],
                "flete": tot["flete"],
                "total": tot["total_final"],
                "es_pedido": bool(es_pedido),
                "id_origen": int(id_origen) if id_origen else None,
                "obs": (observaciones or "").strip() or None,
                "usuario": usuario,
            },
            conn=conn,
        )
        id_proforma = cab["id_proforma"]
        for ln in limpias:
            db.execute(_SQL_INSERT_DETALLE, _params_detalle(id_proforma, ln), conn=conn)
    return {"id_proforma": id_proforma, **tot}


def actualizar(
    id_proforma: int,
    *,
    codigo_cli: str,
    fecha,
    lineas: list[dict],
    pct_volumen: float = 0.0,
    aplica_contado: bool = True,
    pct_contado: float = 5.0,
    flete: float = 0.0,
    observaciones: str | None = None,
) -> dict:
    """Regraba una proforma que se acaba de guardar desde la MISMA pantalla.

    Para qué existe: Alex aprieta Imprimir, ve un error en el papel, lo corrige
    y vuelve a apretar. Sin esto quedarían dos proformas casi iguales por cada
    dedazo. La pantalla se queda con el número que le tocó y regraba ese —
    número NUEVO sólo cuando reabre una guardada desde la lista, que es lo que
    la dueña decidió (2026-08-11).

    No toca `es_pedido` ni `id_origen`: la pantalla es la que es.
    """
    cli = cliente_defaults(codigo_cli)
    if not cli:
        raise ValueError(f"El cliente {codigo_cli!r} no existe.")
    limpias = _lineas_limpias(lineas)
    tot = calcular_totales(limpias, pct_volumen, aplica_contado, pct_contado, flete)

    with db.tx() as conn:
        tocadas = db.execute(
            """
            UPDATE scintela.proforma_cabecera
               SET id_cliente = %(id_cliente)s, fecha_emision = %(fecha)s,
                   subtotal = %(subtotal)s,
                   porcentaje_descuento_volumen = %(pct_vol)s,
                   monto_descuento_volumen = %(monto_vol)s,
                   subtotal_con_descuento = %(sub_desc)s,
                   aplica_descuento_contado = %(aplica)s,
                   monto_descuento_contado = %(monto_cont)s,
                   flete = %(flete)s, total_final = %(total)s,
                   observaciones = %(obs)s
             WHERE id_proforma = %(id)s
            """,
            {
                "id": id_proforma,
                "id_cliente": cli["id_cliente"],
                "fecha": fecha,
                "subtotal": tot["subtotal"],
                "pct_vol": tot["porcentaje_descuento_volumen"],
                "monto_vol": tot["monto_descuento_volumen"],
                "sub_desc": tot["subtotal_con_descuento"],
                "aplica": tot["aplica_descuento_contado"],
                "monto_cont": tot["monto_descuento_contado"],
                "flete": tot["flete"],
                "total": tot["total_final"],
                "obs": (observaciones or "").strip() or None,
            },
            conn=conn,
        )
        if not tocadas:
            # Se la llevó el barrido de los 7 días mientras la pantalla estaba
            # abierta: no hay nada que regrabar.
            raise LookupError(f"La proforma {id_proforma} ya no existe.")
        db.execute(
            "DELETE FROM scintela.proforma_detalle WHERE id_proforma = %s",
            (id_proforma,), conn=conn,
        )
        for ln in limpias:
            db.execute(_SQL_INSERT_DETALLE, _params_detalle(id_proforma, ln), conn=conn)
    return {"id_proforma": id_proforma, **tot}


def buscar(
    q: str = "",
    desde: str | None = None,
    hasta: str | None = None,
    limite: int = 300,
) -> list[dict]:
    """Las proformas guardadas, la última creada arriba.

    No hace falta filtrar por "últimos 7 días": las de más de 7 días ya no
    están (las borra `purgar_viejas`, decisión de la dueña 2026-08-11).
    """
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    # TMT 2026-08-04 (dueña "no funciona si solo busco condor"): match por
    # PALABRAS sueltas y sin acentos. Ver modules/_lib/busqueda.py.
    _txt_sql, _txt_params = busqueda.condicion(
        q, ("c.nombre", "c.codigo_cli"), prefijo="bqt")
    return db.fetch_all(
        """
        SELECT h.id_proforma, h.fecha_emision, h.id_cliente, h.creado_en,
               COALESCE(c.codigo_cli, '') AS codigo_cli,
               COALESCE(c.nombre, '')     AS cliente,
               h.subtotal, h.monto_descuento_volumen, h.subtotal_con_descuento,
               h.aplica_descuento_contado, h.monto_descuento_contado,
               h.flete, h.total_final, h.es_pedido, h.id_origen,
               h.usuario_crea, h.observaciones,
               (SELECT COALESCE(SUM(d.cantidad_kilos), 0)
                  FROM scintela.proforma_detalle d
                 WHERE d.id_proforma = h.id_proforma) AS kilos
        FROM scintela.proforma_cabecera h
        LEFT JOIN scintela.cliente c ON c.id_cliente = h.id_cliente
        WHERE (%(q)s IS NULL
               OR (__TEXTO_MATCH__)
               OR CAST(h.id_proforma AS TEXT) LIKE %(like)s)
          AND (%(desde)s::date IS NULL OR h.fecha_emision >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR h.fecha_emision <= %(hasta)s::date)
        ORDER BY h.creado_en DESC NULLS LAST, h.id_proforma DESC
        LIMIT %(limite)s
        """.replace("__TEXTO_MATCH__", _txt_sql or "FALSE"),
        {
            **_txt_params,
            "q": q or None, "like": like,
            "desde": desde or None, "hasta": hasta or None,
            "limite": limite,
        },
    )


def detalle(id_proforma: int) -> dict | None:
    cabecera = db.fetch_one(
        """
        SELECT h.*, c.codigo_cli, c.nombre AS cliente, c.ruc, c.telefono
        FROM scintela.proforma_cabecera h
        LEFT JOIN scintela.cliente c ON c.id_cliente = h.id_cliente
        WHERE h.id_proforma = %s
        """,
        (id_proforma,),
    )
    if not cabecera:
        return None
    items = db.fetch_all(
        """
        SELECT id_detalle, tela, nombre_producto, color, clase,
               cantidad, cantidad_kilos, precio_unitario, precio_total
        FROM scintela.proforma_detalle
        WHERE id_proforma = %s
        ORDER BY id_detalle
        """,
        (id_proforma,),
    )
    # ¿La versión de la que salió sigue viva? Si se la llevó el barrido de los
    # 7 días, el número se muestra como texto y NO como link (un link que da
    # 404 es peor que no tenerlo).
    origen_vive = False
    if cabecera.get("id_origen"):
        origen_vive = bool(db.fetch_one(
            "SELECT 1 FROM scintela.proforma_cabecera WHERE id_proforma = %s",
            (cabecera["id_origen"],),
        ))
    return {"cabecera": cabecera, "items": items, "origen_vive": origen_vive}


def para_formulario(id_proforma: int) -> dict | None:
    """Una proforma guardada, en la forma que espera la PANTALLA (no la que
    devuelve la DB): fecha DD/MM/AAAA, porcentajes como texto y las líneas con
    la etiqueta de tela que el formulario sabe resolver.

    Es lo que hace posible reabrirla y cambiarle algo sin volver a tipearla.
    """
    data = detalle(id_proforma)
    if not data:
        return None
    cab = data["cabecera"]
    lineas = [
        {
            "tela": (ln.get("tela") or ""),
            "label": (ln.get("nombre_producto") or ""),
            "color": ln.get("color") or "",
            "clase": ln.get("clase"),
            "cantidad": float(ln["cantidad"]) if ln.get("cantidad") is not None else None,
            "kg": float(ln.get("cantidad_kilos") or 0),
            "precio": float(ln.get("precio_unitario") or 0),
        }
        for ln in data["items"]
    ]
    return {
        "id_origen": cab["id_proforma"],
        "es_pedido": bool(cab.get("es_pedido")),
        "form": {
            "codigo_cli": cab.get("codigo_cli") or "",
            "cliente": cab.get("cliente") or "",
            "fecha": cab["fecha_emision"].strftime("%d/%m/%Y") if cab.get("fecha_emision") else "",
            "descuento_volumen": _pct_txt(cab.get("porcentaje_descuento_volumen")),
            "flete": _pct_txt(cab.get("flete")),
            "observaciones": cab.get("observaciones") or "",
        },
        "lineas": lineas,
    }


def _pct_txt(v) -> str:
    """Número → texto para un input: sin decimales si es entero (5, no 5,00)."""
    try:
        f = float(v or 0)
    except (TypeError, ValueError):
        return "0"
    return str(int(f)) if f == int(f) else ("%.2f" % f).replace(".", ",")


def purgar_viejas(dias: int = 7) -> int:
    """Borra las proformas de más de `dias` días y devuelve cuántas.

    Decisión de la dueña (2026-08-11): las proformas se guardan UNA SEMANA.
    El detalle se va solo por el ON DELETE CASCADE de la migración 0119.
    Se mide contra `creado_en` (cuándo se guardó de verdad) y no contra
    `fecha_emision`, que el usuario puede tipear a mano.
    """
    return db.execute(
        """
        DELETE FROM scintela.proforma_cabecera
         WHERE creado_en < (NOW() - MAKE_INTERVAL(days => %s))
        """,
        (int(dias),),
    )
