"""Parsers compartidos por todas las vistas de data entry.

Reglas:
- Fechas aceptan ISO (YYYY-MM-DD), DD/MM/YYYY y DD-MM-YYYY. Devuelven `date` o None.
- Montos aceptan "1234.56" y "1.234,56" (es-EC). Devuelven Decimal o None.
- Enteros aceptan texto con espacios. Devuelven int o None.
- Bool aceptan "1"/"0", "true"/"false", "si"/"no", "on".

Por qué aquí y no en cada views.py: antes cada módulo duplicaba los helpers,
y cuando se agregó un formato (DD-MM-YYYY), había que tocar N archivos.
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation


def parse_date(s) -> date | None:
    if s is None:
        return None
    if isinstance(s, date):
        return s
    s = str(s).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_monto(s) -> Decimal | None:
    if s is None:
        return None
    if isinstance(s, int | Decimal):
        return Decimal(s)
    if isinstance(s, float):
        return Decimal(str(s))
    s = str(s).strip()
    if s == "":
        return None
    # Detección de formato (paridad con parseMonto JS — TMT 2026-05-15):
    #   - ES (1.234,56): si la última COMA está a la derecha del último PUNTO,
    #     puntos son miles y coma es decimal.
    #   - US (1,234.56): si el último PUNTO está a la derecha de la última coma,
    #     comas son miles y punto es decimal.
    #   - Sólo coma (1234,56): comportamiento ES.
    #   - Sólo punto (1234.56 o -3.75): comportamiento US (punto decimal).
    # Antes: si había "," intentábamos ES siempre, lo que destruía "1,234.56"
    # (US con miles) convirtiéndolo en "1.234.56" → InvalidOperation → None.
    last_comma = s.rfind(",")
    last_dot = s.rfind(".")
    if last_comma > -1 and last_dot > -1:
        if last_comma > last_dot:
            # ES: puntos = miles, coma = decimal.
            s = s.replace(".", "").replace(",", ".")
        else:
            # US: comas = miles, punto = decimal.
            s = s.replace(",", "")
    elif last_comma > -1:
        # Sólo coma → asumir coma decimal estilo ES.
        s = s.replace(",", ".")
    # Sólo punto o sin separadores → parse directo.
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def monto_rango(s) -> tuple[Decimal, Decimal, bool] | None:
    """Convierte un monto TIPEADO en el RANGO que hay que buscar.

    Semántica única de la casa (dueña 2026-07-12 sobre facturas y cheques;
    extendida a los movimientos de banco el 2026-08-14 — *"que monto se
    busque igual que en cheques, no el exacto"*):

      · entero    "500"    → el DÓLAR ENTERO: 500,00 … 500,99.
      · centavos  "500,51" → exacto (banda de ±medio centavo, para no pelear
        con el redondeo del numeric).

    El tercer elemento dice si el usuario tipeó un MENOS adelante. Donde el
    importe es SIGNADO (transacciones_bancarias: los débitos van en negativo)
    eso decide el lado: sin signo trae los dos (entró o salió), con "-" trae
    SÓLO las salidas. Decisión de la dueña 2026-08-14.

    Devuelve None si el texto no es un monto.
    """
    if s is None:
        return None
    crudo = str(s).strip()
    if crudo == "":
        return None
    val = parse_monto(crudo)
    if val is None:
        return None
    negativo = crudo.startswith("-")
    val = abs(val)
    # ¿Escribió centavos? Hay un separador decimal con al menos un dígito
    # detrás. Ojo: parse_monto trata "1.000" como 1,0 (punto = decimal
    # cuando viene solo), y acá pasa lo mismo — es la MISMA regla que ya
    # corre en cheques y facturas, no una nueva.
    dec_pos = max(crudo.rfind(","), crudo.rfind("."))
    tiene_centavos = dec_pos > -1 and crudo[dec_pos + 1:dec_pos + 2].isdigit()
    if tiene_centavos:
        return (val - Decimal("0.005"), val + Decimal("0.005"), negativo)
    base = Decimal(int(val))
    return (base, base + Decimal("0.999999"), negativo)


def parse_int(s) -> int | None:
    if s is None:
        return None
    if isinstance(s, int):
        return s
    s = str(s).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def parse_bool(s) -> bool:
    if s is None:
        return False
    if isinstance(s, bool):
        return s
    s = str(s).strip().lower()
    return s in ("1", "true", "yes", "si", "sí", "on", "t", "y")
