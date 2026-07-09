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
    ("medical", "MEDICAL"),
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
        }
    """
    cols = ", ".join(col for col, _ in TELAS)
    filas = db.fetch_all(
        f"SELECT clase, descripcio, {cols} FROM scintela.precios ORDER BY clase ASC"
    ) or []
    clases = [{"clase": int(f["clase"]), "descripcio": f["descripcio"]} for f in filas]
    precios: dict[str, dict] = {}
    for f in filas:
        precios[str(int(f["clase"]))] = {
            col: (float(f[col]) if f.get(col) is not None else None)
            for col, _ in TELAS
        }
    return {
        "clases": clases,
        "telas": [{"col": c, "label": lab} for c, lab in TELAS],
        "precios": precios,
    }


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
    aplica_contado: bool = False,
    pct_contado: float = 5.0,
) -> dict:
    """Réplica de la cascada de PROCEDURE FACTURO (dBase), función PURA.

    subtotal            = Σ (kg × precio)
    desc. volumen       = subtotal × pct_volumen/100
    subtotal c/desc     = subtotal − desc. volumen
    desc. contado       = (subtotal c/desc) × pct_contado/100   [si aplica]
    total               = subtotal c/desc − desc. contado

    No toca la DB — testeable en aislamiento (el sandbox no corre contra RDS).
    """
    subtotal = 0.0
    for ln in lineas:
        kg = float(ln.get("cantidad_kilos") or 0)
        pu = float(ln.get("precio_unitario") or 0)
        subtotal += kg * pu
    subtotal = round(subtotal, 2)

    pct_volumen = max(0.0, float(pct_volumen or 0))
    monto_vol = round(subtotal * pct_volumen / 100.0, 2)
    subtotal_desc = round(subtotal - monto_vol, 2)

    monto_contado = 0.0
    if aplica_contado:
        pct_contado = max(0.0, float(pct_contado or 0))
        monto_contado = round(subtotal_desc * pct_contado / 100.0, 2)
    total = round(subtotal_desc - monto_contado, 2)

    return {
        "subtotal": subtotal,
        "porcentaje_descuento_volumen": round(pct_volumen, 2),
        "monto_descuento_volumen": monto_vol,
        "subtotal_con_descuento": subtotal_desc,
        "aplica_descuento_contado": bool(aplica_contado),
        "monto_descuento_contado": monto_contado,
        "total_final": total,
    }


def crear(
    *,
    codigo_cli: str,
    fecha,
    lineas: list[dict],
    pct_volumen: float = 0.0,
    aplica_contado: bool = False,
    pct_contado: float = 5.0,
    observaciones: str | None = None,
    usuario: str = "web",
) -> dict:
    """Inserta una cotización (cabecera + detalle) en una sola transacción.

    `lineas` = [{tela, nombre_producto, color, clase, cantidad_kilos,
    precio_unitario}, ...]. El importe por línea y los totales se calculan acá
    (no se confía en lo que mande el browser). Devuelve {id_proforma}.
    """
    cli = cliente_defaults(codigo_cli)
    if not cli:
        raise ValueError(f"El cliente {codigo_cli!r} no existe.")

    limpias: list[dict] = []
    for ln in lineas:
        kg = float(ln.get("cantidad_kilos") or 0)
        pu = float(ln.get("precio_unitario") or 0)
        if kg == 0 and pu == 0:
            continue  # línea vacía — la ignoramos (como el KG=0 del dBase)
        tela = (ln.get("tela") or "").strip().lower()
        nombre = (ln.get("nombre_producto") or _LABEL_POR_COL.get(tela, "") or "").strip()
        clase = ln.get("clase")
        try:
            clase = int(clase) if clase not in (None, "") else None
        except (TypeError, ValueError):
            clase = None
        limpias.append({
            "nombre_producto": nombre[:60],
            "color": (ln.get("color") or "").strip()[:60],
            "clase": clase,
            "cantidad_kilos": round(kg, 2),
            "precio_unitario": round(pu, 4),
            "precio_total": round(kg * pu, 2),
        })

    if not limpias:
        raise ValueError("La cotización no tiene ninguna línea con datos.")

    tot = calcular_totales(limpias, pct_volumen, aplica_contado, pct_contado)

    with db.tx() as conn:
        cab = db.execute_returning(
            """
            INSERT INTO scintela.proforma_cabecera
                (id_cliente, fecha_emision, subtotal,
                 porcentaje_descuento_volumen, monto_descuento_volumen,
                 subtotal_con_descuento, aplica_descuento_contado,
                 monto_descuento_contado, total_final, observaciones, usuario_crea)
            VALUES (%(id_cliente)s, %(fecha)s, %(subtotal)s,
                    %(pct_vol)s, %(monto_vol)s, %(sub_desc)s, %(aplica)s,
                    %(monto_cont)s, %(total)s, %(obs)s, %(usuario)s)
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
                "total": tot["total_final"],
                "obs": (observaciones or "").strip() or None,
                "usuario": usuario,
            },
            conn=conn,
        )
        id_proforma = cab["id_proforma"]
        for ln in limpias:
            db.execute(
                """
                INSERT INTO scintela.proforma_detalle
                    (id_proforma, nombre_producto, color, clase,
                     cantidad_kilos, precio_unitario, precio_total)
                VALUES (%(id)s, %(nombre)s, %(color)s, %(clase)s,
                        %(kg)s, %(pu)s, %(pt)s)
                """,
                {
                    "id": id_proforma,
                    "nombre": ln["nombre_producto"],
                    "color": ln["color"],
                    "clase": ln["clase"],
                    "kg": ln["cantidad_kilos"],
                    "pu": ln["precio_unitario"],
                    "pt": ln["precio_total"],
                },
                conn=conn,
            )
    return {"id_proforma": id_proforma}


def buscar(
    q: str = "",
    desde: str | None = None,
    hasta: str | None = None,
    limite: int = 300,
) -> list[dict]:
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    return db.fetch_all(
        """
        SELECT h.id_proforma, h.fecha_emision, h.id_cliente,
               COALESCE(c.codigo_cli, '') AS codigo_cli,
               COALESCE(c.nombre, '')     AS cliente,
               h.subtotal, h.monto_descuento_volumen, h.subtotal_con_descuento,
               h.aplica_descuento_contado, h.monto_descuento_contado, h.total_final,
               h.observaciones
        FROM scintela.proforma_cabecera h
        LEFT JOIN scintela.cliente c ON c.id_cliente = h.id_cliente
        WHERE (%(q)s IS NULL
               OR UPPER(COALESCE(c.nombre,'')) LIKE UPPER(%(like)s)
               OR UPPER(COALESCE(c.codigo_cli,'')) LIKE UPPER(%(like)s)
               OR CAST(h.id_proforma AS TEXT) LIKE %(like)s)
          AND (%(desde)s::date IS NULL OR h.fecha_emision >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR h.fecha_emision <= %(hasta)s::date)
        ORDER BY h.fecha_emision DESC, h.id_proforma DESC
        LIMIT %(limite)s
        """,
        {
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
        SELECT id_detalle, nombre_producto, color,
               cantidad_kilos, precio_unitario, precio_total
        FROM scintela.proforma_detalle
        WHERE id_proforma = %s
        ORDER BY id_detalle
        """,
        (id_proforma,),
    )
    return {"cabecera": cabecera, "items": items}
