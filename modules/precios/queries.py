"""Consultas de la lista de precios (réplica de PRECIOS.DBF)."""
import db

# Columnas de tela en el orden del dBase (PRECIOS.DBF). Cada tupla es
# (columna en la tabla scintela.precios, etiqueta que ve el usuario).
TELAS: list[tuple[str, str]] = [
    ("jersey", "JERSEY"),
    ("pique", "PIQUE"),
    ("toper", "TOPER"),
    ("alemania", "ALEMANIA"),
    ("rib", "RIB"),
    ("cuellos", "CUELLOS"),
    ("lycra", "LYCRA"),
    # TMT 2026-08-04 (dueña: "no es Falso es fleece"): `falso` es el NOMBRE DE
    # COLUMNA que viene de PRECIOS.DBF, no la tela. La tela es FLEECE — así ya
    # estaba etiquetada en Proformas (modules/proformas/queries.py). La columna
    # sigue llamándose `falso` en la tabla; sólo cambia lo que ve el usuario.
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

# Nombres de columna válidos para el update inline (whitelist — nunca
# interpolar el nombre de columna que llega del request sin validar).
COLUMNAS_TELA: set[str] = {col for col, _ in TELAS}


def matriz() -> list[dict]:
    """Las clases de color con sus 12 precios, ordenadas por clase."""
    cols = ", ".join(col for col, _ in TELAS)
    return db.fetch_all(
        f"""
        SELECT clase, descripcio, {cols}
          FROM scintela.precios
         ORDER BY clase ASC
        """
    ) or []


def actualizar_precio(clase: int, columna: str, valor, usuario: str) -> None:
    """Actualiza una celda (clase, tela). `columna` DEBE estar en COLUMNAS_TELA.

    `valor` puede ser None (borra el precio de esa celda) o un número.
    """
    if columna not in COLUMNAS_TELA:
        raise ValueError(f"columna inválida: {columna!r}")
    db.execute(
        f"""
        UPDATE scintela.precios
           SET {columna} = %(valor)s,
               actualizado = CURRENT_TIMESTAMP,
               usuario_edita = %(usuario)s
         WHERE clase = %(clase)s
        """,
        {"valor": valor, "usuario": usuario, "clase": clase},
    )


def subir_porcentaje(pct: float, usuario: str) -> None:
    """Sube TODOS los precios de la matriz un `pct` % (× (1 + pct/100)).

    Es la forma normal de actualizar la lista: se aplica a las 12 columnas de
    tela en todas las clases de color, redondeando a 4 decimales. Las celdas
    vacías (NULL) quedan como están.

    🚨 Cuatro decimales, no dos (migración 0177): la matriz se guarda NETA
    y la hoja la re-multiplica por 1,15. Con dos decimales el redondeo se
    come hasta un centavo al volver — así fue como 4 celdas dejaron de dar
    igual que el dBase.
    """
    factor = 1.0 + (float(pct) / 100.0)
    sets = ", ".join(
        f"{col} = ROUND({col} * %(factor)s::numeric, 4)" for col, _ in TELAS
    )
    db.execute(
        f"""
        UPDATE scintela.precios
           SET {sets},
               actualizado = CURRENT_TIMESTAMP,
               usuario_edita = %(usuario)s
        """,
        {"factor": factor, "usuario": usuario},
    )
    # Las telas de precio único suben igual — si no, un aumento general las
    # deja atrás en silencio (las de `ref_col` suben solas: miran la matriz).
    bootstrap_precio_plano()
    db.execute(
        """
        UPDATE scintela.precio_plano
           SET precio = ROUND(precio * %(factor)s::numeric, 4),
               actualizado = CURRENT_TIMESTAMP,
               usuario_edita = %(usuario)s
         WHERE precio IS NOT NULL
        """,
        {"factor": factor, "usuario": usuario},
    )


def sumar_monto(monto: float, usuario: str) -> None:
    """Suma un `monto` fijo (USD) a TODOS los precios de la matriz.

    Alternativa al aumento porcentual: agrega el mismo importe (p.ej. 0,10 =
    diez centavos) a las 12 columnas de tela en todas las clases, redondeando a
    4 decimales (ver `subir_porcentaje`). Las celdas vacías (NULL) quedan como
    están (NULL + n = NULL).
    """
    sets = ", ".join(
        f"{col} = ROUND({col} + %(monto)s::numeric, 4)" for col, _ in TELAS
    )
    db.execute(
        f"""
        UPDATE scintela.precios
           SET {sets},
               actualizado = CURRENT_TIMESTAMP,
               usuario_edita = %(usuario)s
        """,
        {"monto": monto, "usuario": usuario},
    )
    bootstrap_precio_plano()
    db.execute(
        """
        UPDATE scintela.precio_plano
           SET precio = ROUND(precio + %(monto)s::numeric, 4),
               actualizado = CURRENT_TIMESTAMP,
               usuario_edita = %(usuario)s
         WHERE precio IS NOT NULL
        """,
        {"monto": monto, "usuario": usuario},
    )


# Los tramos de precio: basico (precio de lista, sin descuento) y luego
# descuentos EN CASCADA (sucesivos). "5%+9%" = un 5% de descuento y luego un
# 9% adicional sobre el YA REBAJADO, es decir lista * 0,95 * 0,91 — NO un 14%
# de una (eso daría 0,86, casi dos centavos menos por kilo).
#
# TMT 2026-08-04, dueña: "dame la opcion de calcular descuentos desde 5%+4% y
# 5%+5% hasta 5%+14% todas" → antes había cuatro sueltos (4, 9 y 14); ahora
# está la escala COMPLETA punto por punto. Cada tramo es (etiqueta, factor
# sobre la lista).
TRAMOS_DESCUENTO: list[tuple[str, float]] = [
    ("Basico", 1.0),
    ("5%", 0.95),
    *[(f"5%+{n}%", 0.95 * (1.0 - n / 100.0)) for n in range(4, 15)],
]


# IVA vigente en Ecuador (15%). La lista de PRECIOS.DBF está SIN IVA; la hoja
# que se le entrega al cliente sale con IVA (el Excel de la dueña se titula
# "LISTA DE PRECIOS IVA 15%" y multiplica por 1,15).
IVA_PCT: float = 15.0


def precio_con_iva(monto: float | None) -> float | None:
    """Un precio NETO (como se guarda) llevado a precio CON IVA (como se le
    muestra al cliente: la hoja impresa y la Factura Proforma).

    Es la ÚNICA forma de agregarle IVA a un número en toda la app — junto con
    `factor_hoja`, que la usa para la hoja. Nunca escribir `* 1.15` suelto:
    así se coló un IVA de más más de una vez.
    """
    if monto is None:
        return None
    return float(monto) * (1.0 + IVA_PCT / 100.0)


# ---------------------------------------------------------------------------
# Telas de PRECIO ÚNICO (scintela.precio_plano) — migración 0159.
#
# La dueña (2026-08-04, foto por chat) usa aparte de la matriz una tablita de
# telas que no tienen precio por clase de color: uno solo para todas.
# `ref_col` = "cobra lo mismo que esta columna de la matriz" (JERSEY 3,5 y
# JERSEY 3 = jersey), para que suban solas cuando sube el jersey.
#
# 🚨 LAS CIFRAS DE LA FOTO SON NETAS (sin IVA). El primer intento las dividió
# por 1,15 leyendo su "tiene iva incluido" como que venían con IVA, y ella lo
# corrigió: "los precios que subimos recien, les sacamos doblemente IVA, ya
# estaban sin IVA". Se ve solo mirando la matriz: KIANA vale 8,88 y NATY
# también 8,88 — están en la MISMA escala. Van tal cual, netas como la
# matriz; el IVA lo agrega la hoja al imprimir.
# ---------------------------------------------------------------------------
_PLANO_SEED: list[tuple[int, str, float | None, str | None, str]] = [
    (1, "JERSEY 3,5", None, "jersey", "Precio de JERSEY"),
    (2, "JERSEY 3", None, "jersey", "Precio de JERSEY"),
    (3, "SCUBA", 11.25, None, "Todos los colores"),
    (4, "SUPLEX", 10.21, None, "Todos los colores"),
    (5, "BELTIS", 11.49, None, "Todos los colores"),
    (6, "NATY", 8.88, None, "Todos los colores"),
]

_plano_listo = False


def bootstrap_precio_plano() -> None:
    """Crea y siembra scintela.precio_plano si no existe.

    El deploy NO corre migraciones (ver 0159 y el precedente de
    modules/cheques/concepto_cobro.py), así que la tabla se crea en caliente
    la primera vez que alguien abre la pantalla. Idempotente y barato: una
    sola vez por proceso.
    """
    global _plano_listo
    if _plano_listo:
        return
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS scintela.precio_plano (
            id            SERIAL PRIMARY KEY,
            orden         INTEGER      NOT NULL DEFAULT 0,
            tela          VARCHAR(40)  NOT NULL UNIQUE,
            precio        NUMERIC(12, 4),
            ref_col       VARCHAR(20),
            nota          VARCHAR(60)  NOT NULL DEFAULT 'Todos los colores',
            actualizado   TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
            usuario_edita VARCHAR(50)
        )
        """
    )
    for orden, tela, precio, ref_col, nota in _PLANO_SEED:
        db.execute(
            """
            INSERT INTO scintela.precio_plano (orden, tela, precio, ref_col, nota)
                 VALUES (%(orden)s, %(tela)s, %(precio)s, %(ref_col)s, %(nota)s)
            ON CONFLICT (tela) DO NOTHING
            """,
            {
                "orden": orden,
                "tela": tela,
                "precio": precio,
                "ref_col": ref_col,
                "nota": nota,
            },
        )
    _plano_listo = True


def precio_plano() -> list[dict]:
    """Las telas de precio único, en el orden en que las escribió la dueña."""
    bootstrap_precio_plano()
    return db.fetch_all(
        """
        SELECT id, orden, tela, precio, ref_col, nota
          FROM scintela.precio_plano
         ORDER BY orden ASC, id ASC
        """
    ) or []


def actualizar_precio_plano(id_fila: int, valor, usuario: str) -> None:
    """Actualiza el precio (SIN IVA) de una tela de precio único."""
    db.execute(
        """
        UPDATE scintela.precio_plano
           SET precio = %(valor)s,
               actualizado = CURRENT_TIMESTAMP,
               usuario_edita = %(usuario)s
         WHERE id = %(id)s
           AND ref_col IS NULL
        """,
        {"valor": valor, "usuario": usuario, "id": id_fila},
    )


def resolver_plano(planos: list[dict], filas: list[dict]) -> list[dict]:
    """Resuelve el precio final de cada tela de precio único.

    Las filas con `ref_col` no tienen número propio: cobran lo mismo que esa
    columna de la matriz. Como la matriz SÍ varía por clase de color, se
    muestra el rango (mínimo–máximo) y se deja dicho de dónde sale.
    Devuelve `precio` (float|None) y `rango` ((min, max)|None).
    """
    out: list[dict] = []
    for p in planos:
        precio = float(p["precio"]) if p.get("precio") is not None else None
        rango = None
        if p.get("ref_col"):
            vals = [
                float(f[p["ref_col"]])
                for f in filas
                if f.get(p["ref_col"]) is not None
            ]
            if vals:
                rango = (min(vals), max(vals))
                if rango[0] == rango[1]:
                    precio, rango = rango[0], None
        out.append({**p, "precio": precio, "rango": rango})
    return out


def telas_con_precio(filas: list[dict]) -> list[tuple[str, str]]:
    """Las telas que tienen AL MENOS un precio cargado, en el orden del dBase.

    En la hoja que se le entrega al cliente una columna entera vacía es ruido
    (hoy pasa con MEDICAL). Si mañana le cargan precio, vuelve sola.
    """
    return [
        (col, label)
        for col, label in TELAS
        if any(f.get(col) is not None for f in filas)
    ]


def columnas_hoja(filas: list[dict], planos: list[dict] | None = None) -> list[dict]:
    """TODAS las columnas de la hoja: la matriz + las telas de precio único.

    TMT 2026-08-04, corrección de la dueña sobre el primer intento ("¿para qué
    bloque vas a agregar? sumar a como está, no una foto así el screenshot"):
    las telas de precio único NO van en una tablita aparte — van como
    COLUMNAS más de la misma tabla, que es como están en su Excel (ahí la
    cifra se repite igual en las 5 clases de color).

    Dos tipos de columna:
      * `col`  — columna de la matriz: el precio cambia por clase de color.
      * `fijo` — cifra única: el mismo número en las 5 filas.
    Las de `ref_col` (JERSEY 3,5 / JERSEY 3) son columnas `col`='jersey': se
    dibujan PEGADAS a su tela de referencia y varían igual que ella.
    """
    fijas: list[dict] = []
    for p in planos or []:
        # TMT 2026-08-04 (Alex por WhatsApp): *"en la lista de precios
        # mantengamos un solo jersey no los 3 tipos"*. Las telas con `ref_col`
        # (JERSEY 3,5 y JERSEY 3, ambas = JERSEY) duplicaban la columna JERSEY
        # con el mismo número, ensuciando la hoja impresa. Se ocultan de la
        # hoja Y del selector de descuentos: son la misma columna que su
        # referencia. Siguen existiendo en scintela.precio_plano para que la
        # proforma pueda cotizarlas por su propio nombre.
        if p.get("ref_col"):
            continue
        if p.get("precio") is not None:
            fijas.append(p)

    cols: list[dict] = []
    for col, label in TELAS:
        if not any(f.get(col) is not None for f in filas):
            continue  # columna entera vacía: no va al papel (hoy, MEDICAL)
        cols.append({"key": col, "label": label, "col": col, "fijo": None})
    for p in fijas:
        cols.append(
            {
                "key": f"pp{p['id']}",
                "label": p["tela"],
                "col": None,
                "fijo": float(p["precio"]),
            }
        )
    return cols


def factor_hoja(tramo_idx: int, con_iva: bool) -> float:
    """El multiplicador de la hoja impresa: tramo de descuento (+ IVA)."""
    if not 0 <= tramo_idx < len(TRAMOS_DESCUENTO):
        raise ValueError(f"tramo inválido: {tramo_idx!r}")
    factor = TRAMOS_DESCUENTO[tramo_idx][1]
    if con_iva:
        factor *= 1.0 + (IVA_PCT / 100.0)
    return factor


def tabla_impresion(
    filas: list[dict],
    tramo_idx: int,
    con_iva: bool,
    columnas: list[dict] | None = None,
) -> list[dict]:
    """La matriz (clases x columnas) lista para imprimir.

    Aplica UN tramo de descuento (índice en TRAMOS_DESCUENTO) y, si
    `con_iva`, el IVA sobre el neto. Devuelve una fila por clase con
    `valores` alineado al orden de `columnas` (ver `columnas_hoja`; por
    default, las 12 de la matriz). None donde no hay precio. Es derivado: no
    se guarda nada.
    """
    cols = (
        columnas
        if columnas is not None
        else [{"key": c, "label": lab, "col": c, "fijo": None} for c, lab in TELAS]
    )
    factor = factor_hoja(tramo_idx, con_iva)
    out: list[dict] = []
    for f in filas:
        valores = []
        for c in cols:
            if c.get("fijo") is not None:
                valores.append(round(float(c["fijo"]) * factor, 2))
                continue
            bruto = f.get(c["col"]) if c.get("col") else None
            valores.append(
                round(float(bruto) * factor, 2) if bruto is not None else None
            )
        out.append(
            {
                "clase": int(f["clase"]),
                "descripcio": f["descripcio"],
                "valores": valores,
            }
        )
    return out


def tabla_descuentos_columna(
    filas: list[dict], col: dict, con_iva: bool = False
) -> list[dict]:
    """Igual que `tabla_descuentos` pero para una columna de `columnas_hoja`.

    Sirve tanto para una tela de la matriz (el precio cambia por clase) como
    para una de precio único (`fijo`: la misma cifra en las 5 clases), así el
    selector de la pantalla las ofrece a todas por igual.

    Si `con_iva=True`, los precios devueltos ya vienen con el IVA aplicado
    (misma escala que la hoja imprimible). Federico, 2026-08-07: *"El prg
    muestra con IVA y sin IVA. Eso está fuera de nuestra historia. Siempre es
    con IVA incluido"*. En la BD siguen NETOS.
    """
    iva_mult = (1.0 + IVA_PCT / 100.0) if con_iva else 1.0
    if col.get("fijo") is not None:
        fijo = float(col["fijo"])
        return [
            {
                "clase": int(f["clase"]),
                "descripcio": f["descripcio"],
                "lista": round(fijo * iva_mult, 2),
                "netos": [
                    round(fijo * factor * iva_mult, 2)
                    for _, factor in TRAMOS_DESCUENTO
                ],
            }
            for f in filas
        ]
    return tabla_descuentos(filas, col["col"], con_iva=con_iva)


def tabla_descuentos(
    filas: list[dict], columna: str, con_iva: bool = False
) -> list[dict]:
    """Precio de lista (Basico) y neto a los 4 tramos en cascada (5%, 5%+9%,
    5%+14%), por clase de color, para UNA tela (`columna`). Solo lectura -- los
    descuentos son derivados, no se guardan.

    Si `con_iva=True`, los valores devueltos vienen con el IVA aplicado (misma
    escala que la hoja imprimible). Los precios en BD siempre están NETOS.
    """
    if columna not in COLUMNAS_TELA:
        raise ValueError(f"columna invalida: {columna!r}")
    iva_mult = (1.0 + IVA_PCT / 100.0) if con_iva else 1.0
    out: list[dict] = []
    for f in filas:
        lista = f.get(columna)
        netos: list[float | None] = []
        if lista is not None:
            lista = float(lista)
            for _, factor in TRAMOS_DESCUENTO:
                netos.append(round(lista * factor * iva_mult, 2))
            lista_display = round(lista * iva_mult, 2)
        else:
            netos = [None for _ in TRAMOS_DESCUENTO]
            lista_display = None
        out.append(
            {
                "clase": int(f["clase"]),
                "descripcio": f["descripcio"],
                "lista": lista_display,
                "netos": netos,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Colores sin CLASE de precio — sugerida por el precio de lista de Asinfo
# ---------------------------------------------------------------------------
# TMT 2026-08-12. Alex: *"hay tonos nuevos por ejemplo un cocoa no existe en el
# listado"* → *"puedes adicionarlos desde ASINFO?"*.
#
# El listado de colores de la proforma sale de scintela.tinto_costos FILTRADO
# por `clase BETWEEN 1 AND 5`: un color sin clase no se puede cotizar (no hay
# de qué fila de la matriz sacarle el precio) y por eso NO APARECE. De 692
# colores del catálogo, 374 están así.
#
# La clase vivía sólo en COSTOS.DBF, que se retiró el 05/08. Asinfo no tiene
# una columna "clase" y la `categoria` de formulas_app tampoco sirve: no
# distingue MEDIOS (COCOA figura ahí como "Color Bajo" y se vende como MEDIO).
#
# 🚨 Lo que SÍ tiene Asinfo es el PRECIO: cada producto es tela + color y trae
# `precio_ultima_venta`, que es el precio de LISTA de esa tela para la clase
# del color. Comparado contra la matriz, la fila que coincide ES la clase.
# Verificado contra los 318 colores que ya tenían clase cargada: 205 aciertos
# sobre 207 comparables (99,0%).
#
# ⚠️ Sólo se cuenta un precio cuando identifica UNA sola clase en esa tela.
# ALEMANIA, KIANA, MICRO y JAMES cobran lo mismo en las cinco clases, y LYCRA
# comparte cifra entre BAJOS/MEDIOS/JASPEADOS: ahí el precio no dice nada y la
# fila se descarta sola, sin lista negra que mantener.

#: Categoría de producto en Asinfo → columna de tela en scintela.precios.
CATEGORIA_ASINFO_A_TELA: dict[str, str] = {
    "Jersey": "jersey",
    "Pique": "pique",
    "Toper": "toper",
    "Alemania": "alemania",
    "Rib": "rib",
    "Cuellos": "cuellos",
    "Lycra": "lycra",
    "Fleece": "falso",   # `falso` es el nombre de columna de PRECIOS.DBF; la tela es FLEECE
    "Kiana": "kiana",
    "Micro": "micro",
    "James": "james",
}

_COLORES_TTL_SECS = 300
_COLORES_CACHE: dict = {}

#: Para decir que un color YA CARGADO está en la clase equivocada no alcanza
#: con una factura suelta: una venta a precio raro no puede reclasificar un
#: color. Se pide que TODAS las líneas que caen en una fila de la matriz caigan
#: en la misma, y que sean unas cuantas. Con estos valores, el 12/08/2026
#: saltaron exactamente 2 colores sobre 201 comparables: AVELLANA (413 de 413)
#: y PURPURA (42 de 42), los dos al 100%.
DISCREPANCIA_MIN_LINEAS = 5
DISCREPANCIA_MIN_PUREZA = 0.9


def _precio_a_clase_por_tela(filas: list[dict]) -> dict[tuple[str, float], int]:
    """{(categoria_asinfo, precio_redondeado): clase} — sólo los precios que
    identifican UNA sola clase dentro de esa tela.

    Los precios de la matriz están NETOS, que es la misma escala en la que
    Asinfo guarda `precio_ultima_venta` (el IVA lo suma la factura aparte).
    """
    por_tela: dict[str, dict[float, list[int]]] = {}
    for f in filas or []:
        clase = int(f["clase"])
        for categoria, col in CATEGORIA_ASINFO_A_TELA.items():
            val = f.get(col)
            if val is None:
                continue
            por_tela.setdefault(categoria, {}).setdefault(round(float(val), 2), []).append(clase)
    out: dict[tuple[str, float], int] = {}
    for categoria, precios in por_tela.items():
        for precio, clases in precios.items():
            if len(set(clases)) == 1:
                out[(categoria, precio)] = clases[0]
    return out


def _catalogo_colores() -> dict[str, tuple[str, int | None]]:
    """{cod: (nombre, clase o None)} de TODO el catálogo de colores.

    Hacen falta también los que YA tienen clase: son los que pueden estar
    cargados con una clase distinta de la que Asinfo factura (ver
    `colores_para_revisar`).
    """
    rows = db.fetch_all(
        "SELECT cod, COALESCE(color,'') AS color, clase FROM scintela.tinto_costos "
        "WHERE cod IS NOT NULL AND TRIM(cod) <> ''"
    ) or []
    out: dict[str, tuple[str, int | None]] = {}
    for r in rows:
        cod = (r["cod"] or "").strip().upper()
        if not cod:
            continue
        # El sync de formulas_app le anexa " · Categoria" al nombre; para esta
        # pantalla alcanza con el color.
        nombre = (r["color"] or "").split(" · ")[0].strip() or cod
        clase = r.get("clase")
        out[cod] = (nombre, int(clase) if clase in (1, 2, 3, 4, 5) else None)
    return out


def colores_para_revisar() -> dict[str, list[dict]]:
    """Los colores que Asinfo VENDE y que en el catálogo no están como se
    facturan. Dos grupos:

      `faltan`   — sin clase: la Factura Proforma no los puede cotizar.
      `discrepan`— con una clase distinta de la que Asinfo cobra. Dueña
                   2026-08-12: *"como lo cotice asinfo en las facturas reales"*.

    Cada fila: {cod, color, clase_hoy, sugerida, sugerida_desc, votos,
    evidencia, lineas}.  `sugerida` es None cuando el color se vende pero el
    precio no alcanza para decidir; la fila igual aparece, para ponerla a mano.

    🚨 Dos fuentes de precio, sumadas: la FICHA del producto
    (`precio_ultima_venta`, la lista de hoy) y las LÍNEAS DE FACTURA de los
    últimos 12 meses. Hace falta las dos: de los 19.640 productos de Asinfo
    sólo 5.082 tienen precio en la ficha, así que sola se pierde la mayoría; y
    las facturas solas dejan de matchear el día que sube la lista, hasta que se
    acumulen ventas nuevas.

    Los colores que NO se venden no entran: son fórmulas de tintura que nunca
    se cotizaron (350 de los 374 sin clase) y no hay nada que decidir sobre
    ellos.

    Fail-soft: si Asinfo no contesta devuelve las dos listas vacías.
    """
    import time as _time

    cached = _COLORES_CACHE.get("v2")
    if cached and (_time.time() - cached[0]) < _COLORES_TTL_SECS:
        return cached[1]

    vacio: dict[str, list[dict]] = {"faltan": [], "discrepan": []}
    catalogo = _catalogo_colores()
    if not catalogo:
        return vacio
    tabla = _precio_a_clase_por_tela(matriz())
    if not tabla:
        return vacio

    from modules._lib import metabase_client

    # El color va al final del nombre del producto ("Jersey 3.5 BLA").
    col_expr = ("UPPER(RIGHT(LTRIM(RTRIM(p.nombre)), "
                "CHARINDEX(' ', REVERSE(LTRIM(RTRIM(p.nombre))) + ' ') - 1))")
    sql = f"""
        SELECT cod, categoria, precio, SUM(n) AS n, SUM(lineas) AS lineas FROM (
            SELECT {col_expr} AS cod, cp.nombre AS categoria,
                   ROUND(p.precio_ultima_venta, 2) AS precio,
                   COUNT(*) AS n, 0 AS lineas
              FROM producto p
              JOIN categoria_producto cp
                ON cp.id_categoria_producto = p.id_categoria_producto
             WHERE p.precio_ultima_venta > 0
             GROUP BY {col_expr}, cp.nombre, ROUND(p.precio_ultima_venta, 2)
            UNION ALL
            SELECT {col_expr} AS cod, cp.nombre AS categoria,
                   ROUND(dfc.precio, 2) AS precio,
                   COUNT(*) AS n, COUNT(*) AS lineas
              FROM detalle_factura_cliente dfc
              JOIN factura_cliente fc
                ON fc.id_factura_cliente = dfc.id_factura_cliente
              JOIN producto p ON p.id_producto = dfc.id_producto
              JOIN categoria_producto cp
                ON cp.id_categoria_producto = p.id_categoria_producto
             WHERE fc.id_documento = 7 AND fc.estado IN (1, 4, 16)
               AND fc.fecha >= DATEADD(month, -12, GETDATE())
               AND dfc.precio > 0
             GROUP BY {col_expr}, cp.nombre, ROUND(dfc.precio, 2)
        ) u
        GROUP BY cod, categoria, precio
    """
    rows = metabase_client.fetch_dataset(2, sql, max_results=60000)
    if not rows:
        return vacio

    votos: dict[str, dict[int, int]] = {}
    lineas: dict[str, int] = {}
    for r in rows:
        cod = (str(r.get("cod") or "")).strip().upper()
        if cod not in catalogo:
            continue
        try:
            precio = round(float(r.get("precio") or 0), 2)
            n = int(r.get("n") or 0)
            lin = int(r.get("lineas") or 0)
        except (TypeError, ValueError):
            continue
        # Se vende: cuenta aunque el precio no diga la clase.
        lineas[cod] = lineas.get(cod, 0) + max(lin, 0)
        clase = tabla.get((str(r.get("categoria") or "").strip(), precio))
        if clase is None or n <= 0:
            continue
        v = votos.setdefault(cod, {})
        v[clase] = v.get(clase, 0) + n

    faltan: list[dict] = []
    discrepan: list[dict] = []
    for cod, lin in lineas.items():
        v = votos.get(cod) or {}
        if lin <= 0 and not v:
            continue
        nombre, clase_hoy = catalogo[cod]
        sugerida = max(v.items(), key=lambda kv: (kv[1], -kv[0]))[0] if v else None
        fila = {
            "cod": cod,
            "color": nombre,
            "clase_hoy": clase_hoy,
            "clase_hoy_desc": CLASES_DESC.get(clase_hoy, "") if clase_hoy else "",
            "sugerida": sugerida,
            "sugerida_desc": CLASES_DESC.get(sugerida, "") if sugerida else "",
            "votos": v,
            "lineas": lin,
            "evidencia": _frase_evidencia(v, lin),
        }
        if clase_hoy is None:
            faltan.append(fila)
        elif sugerida is not None and sugerida != clase_hoy and _es_discrepancia(v):
            fila["evidencia"] = _frase_discrepancia(clase_hoy, sugerida, v)
            discrepan.append(fila)

    # Los sin sugerencia van al final: son los que piden una decisión.
    faltan.sort(key=lambda d: (d["sugerida"] is None, d["sugerida"] or 0, -d["lineas"]))
    discrepan.sort(key=lambda d: -sum(d["votos"].values()))
    out = {"faltan": faltan, "discrepan": discrepan}
    _COLORES_CACHE["v2"] = (_time.time(), out)
    return out


def _es_discrepancia(votos: dict[int, int]) -> bool:
    """¿Alcanza la evidencia para decir que un color ya cargado está mal?

    Una venta suelta a precio raro NO puede reclasificar un color: se pide
    volumen y que casi todas las líneas caigan en la MISMA fila de la matriz.
    """
    total = sum(votos.values())
    if total < DISCREPANCIA_MIN_LINEAS:
        return False
    return (max(votos.values()) / total) >= DISCREPANCIA_MIN_PUREZA


def _frase_discrepancia(clase_hoy: int, sugerida: int, votos: dict[int, int]) -> str:
    """"cargado MEDIOS · se factura FUERTES en 413 de 413"."""
    total = sum(votos.values())
    n = votos.get(sugerida, 0)
    return (f"cargado {CLASES_DESC.get(clase_hoy, clase_hoy)} · se factura "
            f"{CLASES_DESC.get(sugerida, sugerida)} en {n} de {total}")


#: Los nombres que ve el usuario. Mismo orden que scintela.precios.
CLASES_DESC: dict[int, str] = {
    1: "BLANCO", 2: "BAJOS", 3: "MEDIOS", 4: "JASPEADOS", 5: "FUERTES",
}


def _frase_evidencia(votos: dict[int, int], lineas: int = 0) -> str:
    """Por qué el sistema sugiere esa clase. CORTA: entra en un renglón.

    Dueña 2026-08-12: *"cada fila es muy grande"* — la frase larga hacía que
    cada color ocupara tres líneas y la tabla no se pudiera barrer de un
    vistazo. Queda lo que decide: a qué precio se vendió y cuántas veces.

    Sin votos dice que el color SE VENDE pero que su precio no distingue la
    clase — pasa cuando sólo sale en ALEMANIA, KIANA, MICRO o JAMES, que
    cobran lo mismo en las cinco.
    """
    fact = f"{lineas:,}".replace(",", ".") + " fact." if lineas else ""
    if not votos:
        return "el precio no distingue la clase" + (f" · {fact}" if fact else "")
    partes = sorted(votos.items(), key=lambda kv: (-kv[1], kv[0]))
    frase = ", ".join(f"{n} a {CLASES_DESC.get(c, c)}" for c, n in partes)
    return frase + (f" · {fact}" if fact else "")


def asignar_clase_color(cod: str, clase: int, usuario: str) -> None:
    """Graba la clase de precio de un color del catálogo."""
    cod = (cod or "").strip().upper()
    if not cod or clase not in CLASES_DESC:
        raise ValueError("Color o clase inválidos.")
    db.execute(
        "UPDATE scintela.tinto_costos SET clase=%s, fecha_modifica=CURRENT_TIMESTAMP, "
        "usuario_modifica=%s WHERE UPPER(TRIM(cod))=%s",
        (clase, usuario, cod),
    )
    _COLORES_CACHE.pop("v1", None)
