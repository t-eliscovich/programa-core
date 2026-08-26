"""Lo que se llevó el cliente, guía por guía (Asinfo).

TMT 2026-08-26, fase 3 del `PLAN_PORTAL_CLIENTE_2026_08_24.md`. El estado de
cuenta contesta *cuánto debo*; esto contesta **qué me mandaron y cuándo** —que
es lo que el cliente mira antes de reclamar—, y lo hace desde el mismo lugar
donde está su saldo.

Programa Core guarda la PLATA de la factura; la MERCADERÍA vive en Asinfo. Acá
se le pregunta lo que sólo él sabe: las guías de despacho, sus rollos y en qué
factura salieron.

⛔ El LOTE de cada rollo estuvo y se sacó (TMT 26/08: *"esto no hace falta"*).
Está impreso en la etiqueta del rollo que el cliente tiene enfrente, y en la
pantalla eran dos números por tela que nadie vino a buscar.

## Lo medido el 26/08/2026, antes de escribir la consulta

1. **Los kilos son los MISMOS que factura la oficina.** Cruzado factura por
   factura contra `scintela.factura.kg`: **515 de 515** de los últimos 7 días,
   sin una sola diferencia. Es la red que hay que volver a correr si esta
   consulta se toca.

2. **Todo lo despachado está en la misma unidad** (`id_unidad_venta = 2`,
   kilos): 60.381 líneas en 3 meses, ni una en unidades. Acá NO existe la
   trampa del renglón con `cantidad = 1` que sí tiene la factura —el servicio
   de logística—, así que sumar `cantidad` es sumar kilos.

3. **Una línea de despacho cae en UNA sola línea de factura** (0 casos con dos,
   3 meses) y **una guía sale en UNA sola factura** (0 casos con dos, 6 meses).
   Por eso el `LEFT JOIN` a la factura no infla los kilos. Si algún día
   apareciera una guía partida en dos facturas, los kilos NO se rompen —salen
   de las líneas de despacho— y en la lista se vería una sola de las dos.

4. **Hay un renglón por ROLLO.** AJO tiene 605 guías y 4.241 renglones en 12
   meses. Por eso la ficha de una guía AGRUPA por tela y cuenta los rollos:
   mostrar 4.241 renglones no es la respuesta a "qué me mandaron".

5. **El nombre del producto ya trae el color** ("Fleece 96 Perchado JFP"). El
   detalle del despacho NO tiene columnas de atributos —cero `id_valor_atributo`,
   al revés que el de la factura—, así que el color legible no se puede armar
   acá. Y está bien: ese es el nombre que el cliente tiene impreso en su guía.

6. **Anulado = `fecha_anulacion IS NOT NULL` = `estado 0`**, las dos cosas a la
   vez: 217 anuladas en 3 meses, todas con estado 0, y las 7.326 vivas con
   estado 4. Filtrar por la fecha alcanza; no hace falta mirar el estado.

## 🚨 Los dos hallazgos de la auditoría del 26/08 — no deshacerlos

**a) El código de 3 letras NO identifica al cliente por sí solo.** Cinco
`nombre_comercial` están repetidos en dos empresas, y dos de esos pares son
**contribuyentes DISTINTOS**: `PRE` (Rodríguez Paredes contra Ponce Chávez) y
`MCS` (Chanatasig, con 24 guías, contra MCS Dyeing Finishing Machinery). Hoy
ninguno de los dos pares llega a mostrar lo del otro por casualidad —al que
choca no le entró ninguna guía todavía—, pero está a UN despacho de que un
cliente vea lo de otro, y esto va a un portal abierto a internet.

Por eso el **RUC va en el WHERE junto con el código**. No cuesta nada: de las
7.326 guías de los últimos 3 meses, las 7.326 matchean por código **y** por
RUC. Y de paso arregla el caso contrario —la misma empresa cargada dos veces
con el mismo nombre comercial (`JUO`, `AR1`)—, que sigue entrando entera.

⭐ **Sin RUC en la ficha no se muestran despachos.** Son 11 clientes de 3.986.
Es a propósito: cuando no podemos probar de quién es la mercadería, la
respuesta correcta no es adivinar por un código de 3 letras que se repite. La
pantalla lo dice y la oficina carga el RUC.

**b) Lo devuelto se marca, pero NO se resta.** `cantidad_devuelta` sí es
mercadería que volvió (875 líneas y 15.033 kg en 3 meses), pero la factura
igual cobra el bruto: la devolución se corrige con OTRO documento —el
`id_documento = 20`, medido en MTR: guía DES-000096186 del 20/08 con las dos
líneas devueltas, factura 001-099-000182331 por $354,99 y devolución
001-099-000011750 por los mismos $354,99 al día siguiente—.

Si acá se restara, esta pantalla diría kilos distintos de los de la factura que
el cliente tiene en la mano, y de los que ven la oficina y el vendedor. Así que
va el bruto —igual que `factura.kg`— **con la marca de lo devuelto al lado**,
que es lo que evita la llamada.

⭐ **El código y el RUC del cliente van SIEMPRE en el WHERE**, también al abrir
una guía suelta. Un número de guía es adivinable —van uno atrás del otro— y sin
el cliente adentro de la consulta, cambiarle un dígito a la URL sería ver el
despacho de otro.

Fail-soft, como todo lo que cuelga de Asinfo: si Metabase no contesta, la
pantalla lo dice y el portal sigue vivo. Nunca levanta.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date, timedelta

_LOG = logging.getLogger("programa_core.asinfo.despachos_cliente")

#: La base de Asinfo dentro de Metabase.
DB_ASINFO = 2

#: Producto Terminado. El mismo de `dia_despacho`, para que los kilos de acá y
#: los del cuadre del día sean los mismos kilos.
BODEGA_PT = 53

#: Cuánto se muestra de entrada. Tres meses es lo que el cliente pregunta; para
#: atrás está el año entero a un click.
MESES_DEFAULT = 3
MESES_MAX = 24

#: Tope de guías por consulta. AJO —el más grande— hace 605 en un año.
TOPE_GUIAS = 900

#: El código de cliente, el RUC y el número de guía son lo ÚNICO que se
#: interpola en el SQL, así que se validan enteros. Un `[:20]` que recorta en
#: vez de rechazar deja pasar la cola sin decirlo.
_COD_RE = re.compile(r"^[A-Z0-9]{1,10}$")
_GUIA_RE = re.compile(r"^[A-Z]{2,6}-\d{4,12}$")

#: Los 9 dígitos de LEK. La cédula tiene 10 y el RUC 13, pero en Asinfo hay una
#: identificación de 9 —medida, no supuesta— y se compara igual porque los dos
#: lados se recortan al MISMO largo. Menos que esto no identifica a nadie.
DIGITOS_MINIMOS = 8

#: Cuánto vale la foto. Una guía vieja no cambia nunca; la de hoy puede recibir
#: su factura en los minutos siguientes.
_TTL_SEGS = 300.0
_TOPE_CACHE = 120

_CACHE: dict[tuple, tuple[float, dict]] = {}
_CANDADO = threading.Lock()


def reset_cache() -> None:
    """Olvida lo cacheado. La usan los tests y el que quiera ver el dato fresco."""
    with _CANDADO:
        _CACHE.clear()


def _hoy_ec() -> date:
    from filters import today_ec

    return today_ec()


def _cacheado(clave: tuple):
    with _CANDADO:
        guardado = _CACHE.get(clave)
        if guardado and (time.monotonic() - guardado[0]) < _TTL_SEGS:
            return guardado[1]
    return None


def _guardar(clave: tuple, valor: dict) -> None:
    # ⚠ Sólo se cachea lo que SALIÓ BIEN: guardar un "no contestó" por cinco
    # minutos convierte un hipo de Metabase en cinco minutos de pantalla vacía.
    if not valor.get("ok"):
        return
    with _CANDADO:
        if len(_CACHE) >= _TOPE_CACHE:
            _CACHE.clear()
        _CACHE[clave] = (time.monotonic(), valor)


def _consultar(sql: str, tope: int) -> list[dict] | None:
    """Le pregunta a Asinfo. `None` es "no contestó", que NO es "no hay nada"."""
    from modules._lib import metabase_client

    try:
        return metabase_client.fetch_dataset(DB_ASINFO, sql, max_results=tope) or []
    except Exception as e:  # noqa: BLE001 -- Asinfo caído no tumba el portal
        _LOG.warning("no pude leer los despachos (%s)", e)
        return None


def _num(valor, redondeo: int = 2) -> float:
    try:
        return round(float(valor or 0), redondeo)
    except (TypeError, ValueError):
        return 0.0


def _limpio(valor) -> str:
    return " ".join(str(valor or "").split())


#: Cuántos dígitos se muestran de un número largo. TMT 26/08: *"pongamos de
#: todo los últimos números, así ocupa menos"*.
DIGITOS_A_LA_VISTA = 6

#: Las categorías de Asinfo que NO se cuentan por rollos. TMT 26/08: *"cuando
#: sea cuellos o RIB ponele una u"*.
#:
#: Medido el 26/08 sobre 3 meses, y el corte no deja dudas: Puños pesa 2,40 kg
#: por renglón, Cuellos 2,72 y Rib 6,09, contra las diez telas que están todas
#: entre 18 y 21,3 — o sea, un rollo. Se va por la CATEGORÍA de Asinfo
#: (`nombre_categoria_producto`) y no por el nombre del producto: el nombre lo
#: escribe una persona y la categoría es la del maestro.
POR_UNIDAD = {"PUNOS", "CUELLOS", "RIB"}


def _por_unidad(categoria: str) -> bool:
    """¿Esto se cuenta en unidades y no en rollos?"""
    limpia = (categoria or "").strip().upper()
    limpia = limpia.replace("Ñ", "N").replace("Á", "A").replace("É", "E") \
                   .replace("Í", "I").replace("Ó", "O").replace("Ú", "U")
    return limpia in POR_UNIDAD


def corto(valor) -> str:
    """El número como lo dice una persona: los últimos dígitos, sin ceros.

    `DES-000096562` → **96562** · `001-099-000182687` → **182687** ·
    `2/8-0004177689` → **177689**.

    Es la misma forma que ya usa la ficha de la factura (`numf_completo`
    partido y sin los ceros de adelante), así que las tres pantallas nombran
    los papeles igual.

    ⚠ Esto es un RÓTULO, no una llave: el número entero sigue viajando en el
    link y en el `title`. Poner el recorte en la URL sería buscar por un número
    que puede repetirse.
    """
    texto = _limpio(valor)
    cola = re.search(r"(\d+)$", texto)
    if not cola:
        return texto
    return cola.group(1)[-DIGITOS_A_LA_VISTA:].lstrip("0") or "0"


def _quien(codigo: str, ruc: str) -> tuple[str, str]:
    """El par que identifica al cliente: código y los dígitos de su RUC.

    Los dos van juntos al WHERE — ver el hallazgo (a). Los dos "no" que
    devuelve son distintos a propósito, porque no se le contestan igual al
    cliente:

    - `("", "")` — el código no es un código. Eso es una URL rota, no un
      cliente sin datos.
    - `("ATE", "")` — el cliente existe pero su ficha no tiene RUC. Ahí la
      pantalla le pide que llame para que se lo carguen.
    """
    cod = (codigo or "").strip().upper()
    digitos = re.sub(r"\D", "", ruc or "")[:10]
    if not _COD_RE.match(cod):
        return "", ""
    return cod, (digitos if len(digitos) >= DIGITOS_MINIMOS else "")


def _filtro_del_cliente(cod: str, r10: str) -> str:
    """El WHERE que ata la guía a ESTE cliente: el código y el RUC.

    El RUC se compara por los primeros dígitos, recortando los DOS lados al
    mismo largo: en Asinfo la identificación viene a veces como cédula (10) y a
    veces como RUC (13) —y una, LEK, con 9—, y en la ficha pasa lo mismo.
    """
    return (f"UPPER(LTRIM(RTRIM(e.nombre_comercial))) = '{cod}'\n"
            f"           AND LEFT(LTRIM(RTRIM(ISNULL(e.identificacion, ''))), "
            f"{len(r10)}) = '{r10}'")


def de_cliente(codigo: str, ruc: str, meses: int = MESES_DEFAULT) -> dict:
    """Las guías del cliente, de la más nueva a la más vieja.

    ``{"ok": bool, "sin_ruc": bool, "guias": [...], "kg": float,
       "devuelto": float, "rollos": int, "desde": date, "meses": int}``

    Cada guía: `numero`, `dia`, `kg`, `rollos`, `devuelto` (kilos que volvieron)
    y `factura` (vacía si todavía no se facturó — pasa todos los días: la
    mercadería va adelante del papel).

    `sin_ruc` es True cuando la ficha del cliente no tiene RUC: sin él no se
    puede probar de quién es la mercadería, y adivinar por el código de 3
    letras es justo lo que la auditoría prohibió.
    """
    meses = MESES_DEFAULT if not meses else max(1, min(int(meses), MESES_MAX))
    vacio = {"ok": False, "sin_ruc": False, "guias": [], "kg": 0.0,
             "devueltos": 0, "rollos": 0, "unidades": 0,
             "desde": _hoy_ec(), "meses": meses}
    cod, r10 = _quien(codigo, ruc)
    if not cod:
        return vacio
    if not r10:
        return {**vacio, "ok": True, "sin_ruc": True}

    guardado = _cacheado(("lista", cod, r10, meses))
    if guardado is not None:
        return guardado

    desde = _hoy_ec() - timedelta(days=int(meses * 30.5))
    # ⚠ Un renglón por GUÍA y por CATEGORÍA: la categoría es la que decide si
    # eso se cuenta en rollos o en unidades, y una guía puede traer las dos
    # cosas (telas y cuellos en el mismo viaje). Se junta acá abajo.
    sql = f"""
        SELECT dc.numero                                      AS guia,
               CONVERT(varchar(10), dc.fecha, 120)            AS dia,
               ISNULL(p.nombre_categoria_producto, '')        AS categoria,
               ROUND(SUM(ISNULL(dd.cantidad, 0)), 2)          AS kg,
               SUM(CASE WHEN ISNULL(dd.cantidad_devuelta, 0) <> 0
                        THEN 1 ELSE 0 END)                    AS devueltos,
               COUNT(*)                                       AS cuantos,
               MAX(ISNULL(fc.numero, ''))                     AS factura
          FROM despacho_cliente dc
          JOIN detalle_despacho_cliente dd
            ON dd.id_despacho_cliente = dc.id_despacho_cliente
          JOIN empresa e ON e.id_empresa = dc.id_empresa
          LEFT JOIN producto p ON p.id_producto = dd.id_producto
          LEFT JOIN detalle_factura_cliente dfc
            ON dfc.id_detalle_despacho_cliente = dd.id_detalle_despacho_cliente
          LEFT JOIN factura_cliente fc
            ON fc.id_factura_cliente = dfc.id_factura_cliente
           AND fc.estado <> 0
         WHERE {_filtro_del_cliente(cod, r10)}
           AND dc.fecha_anulacion IS NULL
           AND dc.fecha >= '{desde.isoformat()}'
         GROUP BY dc.numero, CONVERT(varchar(10), dc.fecha, 120),
                  p.nombre_categoria_producto
         ORDER BY 2 DESC, 1 DESC
    """
    filas = _consultar(sql, TOPE_GUIAS * 3)
    if filas is None:
        return vacio

    juntas: dict[str, dict] = {}
    for f in filas:
        numero = _limpio(f.get("guia"))
        kg = _num(f.get("kg"))
        cuantos = int(_num(f.get("cuantos"), 0))
        if not numero or kg <= 0 or cuantos <= 0:
            continue
        factura = _limpio(f.get("factura"))
        g = juntas.setdefault(numero, {
            "numero": numero, "corto": corto(numero),
            "dia": _limpio(f.get("dia")), "kg": 0.0,
            "rollos": 0, "unidades": 0, "devueltos": 0,
            "factura": factura, "factura_corta": corto(factura),
        })
        if factura and not g["factura"]:
            g["factura"], g["factura_corta"] = factura, corto(factura)
        # Los kilos NO se muestran (TMT 26/08: *"nada de kilos"*), pero se
        # siguen trayendo: son los que tienen que dar iguales a
        # `scintela.factura.kg`, que es la red de esta consulta.
        g["kg"] = round(g["kg"] + kg, 2)
        g["devueltos"] += int(_num(f.get("devueltos"), 0))
        if _por_unidad(f.get("categoria")):
            g["unidades"] += cuantos
        else:
            g["rollos"] += cuantos

    guias = list(juntas.values())
    salida = {
        "ok": True, "sin_ruc": False, "guias": guias, "meses": meses,
        "desde": desde,
        "kg": round(sum(g["kg"] for g in guias), 2),
        "devueltos": sum(g["devueltos"] for g in guias),
        "rollos": sum(g["rollos"] for g in guias),
        "unidades": sum(g["unidades"] for g in guias),
    }
    _guardar(("lista", cod, r10, meses), salida)
    return salida


def guia(codigo: str, ruc: str, numero: str) -> dict:
    """Qué salió en UNA guía del cliente, agrupado por tela.

    ``{"ok": bool, "existe": bool, "dia": str, "factura": str, "kg": float,
       "devuelto": float, "rollos": int, "telas": [...]}``

    `existe` es False cuando esa guía no es de este cliente — que es lo mismo
    que "no existe" desde donde el cliente mira, y a propósito: si dijera "es de
    otro", el portal contestaría de quién es cada número.

    Cada tela trae `devuelto` (kilos que volvieron) y `rollos_devueltos`. Los
    kilos siguen siendo los del papel: ver el hallazgo (b).
    """
    num = (numero or "").strip().upper()
    vacio = {"ok": False, "existe": False, "dia": "", "factura": "",
             "factura_corta": "", "kg": 0.0, "devueltos": 0, "rollos": 0,
             "unidades": 0, "telas": [], "numero": num, "corto": corto(num)}
    cod, r10 = _quien(codigo, ruc)
    if not cod or not r10 or not _GUIA_RE.match(num):
        return vacio

    guardado = _cacheado(("guia", cod, r10, num))
    if guardado is not None:
        return guardado

    sql = f"""
        SELECT CONVERT(varchar(10), dc.fecha, 120)          AS dia,
               ISNULL(p.nombre, '')                         AS producto,
               ISNULL(p.codigo, '')                         AS pcod,
               ISNULL(p.nombre_categoria_producto, '')      AS categoria,
               ROUND(ISNULL(dd.cantidad, 0), 2)             AS kg,
               ROUND(ISNULL(dd.cantidad_devuelta, 0), 2)    AS devuelto,
               ISNULL(fc.numero, '')                        AS factura
          FROM despacho_cliente dc
          JOIN detalle_despacho_cliente dd
            ON dd.id_despacho_cliente = dc.id_despacho_cliente
          JOIN empresa e ON e.id_empresa = dc.id_empresa
          LEFT JOIN producto p ON p.id_producto = dd.id_producto
          LEFT JOIN detalle_factura_cliente dfc
            ON dfc.id_detalle_despacho_cliente = dd.id_detalle_despacho_cliente
          LEFT JOIN factura_cliente fc
            ON fc.id_factura_cliente = dfc.id_factura_cliente
           AND fc.estado <> 0
         WHERE {_filtro_del_cliente(cod, r10)}
           AND dc.numero = '{num}'
           AND dc.fecha_anulacion IS NULL
         ORDER BY dd.orden
    """
    filas = _consultar(sql, 2000)
    if filas is None:
        return vacio
    if not filas:
        return {**vacio, "ok": True}

    telas: dict[str, dict] = {}
    dia = factura = ""
    for f in filas:
        dia = dia or _limpio(f.get("dia"))
        factura = factura or _limpio(f.get("factura"))
        nombre = _limpio(f.get("producto")) or _limpio(f.get("pcod")) or "(sin nombre)"
        t = telas.setdefault(nombre, {
            "producto": nombre, "codigo": _limpio(f.get("pcod")),
            # Cuellos, Rib y Puños se cuentan en unidades; las telas, en
            # rollos. Lo dice la categoría del maestro de Asinfo.
            "por_unidad": _por_unidad(f.get("categoria")),
            "cuantos": 0, "kg": 0.0, "devueltos": 0})
        t["cuantos"] += 1
        t["kg"] = round(t["kg"] + _num(f.get("kg")), 2)
        if _num(f.get("devuelto")):
            t["devueltos"] += 1

    orden = sorted(telas.values(), key=lambda t: -t["kg"])
    salida = {
        "ok": True, "existe": True, "numero": num, "corto": corto(num),
        "dia": dia, "factura": factura, "factura_corta": corto(factura),
        "telas": orden,
        "kg": round(sum(t["kg"] for t in orden), 2),
        "devueltos": sum(t["devueltos"] for t in orden),
        "rollos": sum(t["cuantos"] for t in orden if not t["por_unidad"]),
        "unidades": sum(t["cuantos"] for t in orden if t["por_unidad"]),
    }
    _guardar(("guia", cod, r10, num), salida)
    return salida
