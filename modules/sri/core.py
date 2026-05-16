"""Helpers puros para SRI — sin Flask, sin DB, sin red.

Todo lo que vive acá es determinista y testeable sin mocks:
    - Clave de acceso de 49 dígitos (algoritmo oficial SRI).
    - Dígito verificador módulo 11 con los casos borde (10→1, 11→0).
    - Validación y limpieza de RUC ecuatoriano.
    - Enums de ambiente / tipo de comprobante / tipo de emisión.

Referencia: Ficha técnica SRI "Comprobantes Electrónicos" versión 2.21,
apartado "Formato de Clave de Acceso".
"""
from __future__ import annotations

import random
import re
from datetime import date

# =====================================================================
# Enums / constantes
# =====================================================================

# Ambiente SRI
AMBIENTE_CERTIFICACION = "1"   # sandbox — celcer.sri.gob.ec
AMBIENTE_PRODUCCION    = "2"   # real   — cel.sri.gob.ec

# Tipo de comprobante
TIPO_FACTURA        = "01"
TIPO_NOTA_CREDITO   = "04"
TIPO_NOTA_DEBITO    = "05"
TIPO_GUIA_REMISION  = "06"
TIPO_RETENCION      = "07"

# Tipo de emisión
EMISION_NORMAL         = "1"
EMISION_CONTINGENCIA   = "2"   # raramente usado — offline, sube después

# Pesos cíclicos para módulo 11, aplicados de derecha a izquierda.
_MOD11_PESOS = (2, 3, 4, 5, 6, 7)


# =====================================================================
# Módulo 11 — el dígito verificador de la clave de acceso
# =====================================================================

def digito_verificador_modulo_11(cadena_digitos: str) -> str:
    """Calcula el dígito verificador módulo 11 de una cadena numérica.

    Reglas SRI (ficha técnica):
        1. Multiplicar cada dígito, de derecha a izquierda, por los pesos
           2, 3, 4, 5, 6, 7 en ciclo.
        2. Sumar los productos.
        3. `residuo = suma mod 11`.
        4. `dv = 11 - residuo`.
        5. Casos borde:
              - dv == 11 → 0
              - dv == 10 → 1
           (de otro modo no entraría en 1 dígito).

    >>> digito_verificador_modulo_11("12345")
    '5'
    """
    if not cadena_digitos or not cadena_digitos.isdigit():
        raise ValueError("cadena_digitos debe contener sólo dígitos")

    total = 0
    # Enumerar de derecha a izquierda.
    for i, ch in enumerate(reversed(cadena_digitos)):
        peso = _MOD11_PESOS[i % len(_MOD11_PESOS)]
        total += int(ch) * peso

    residuo = total % 11
    dv = 11 - residuo
    if dv == 11:
        return "0"
    if dv == 10:
        return "1"
    return str(dv)


# =====================================================================
# Clave de acceso — 49 dígitos
# =====================================================================

def generar_clave_acceso(
    *,
    fecha_emision: date,
    tipo_comprobante: str,
    ruc: str,
    ambiente: str,
    estab: str = "001",
    pto_emi: str = "001",
    secuencial: int | str,
    codigo_numerico: str | None = None,
    tipo_emision: str = EMISION_NORMAL,
) -> str:
    """Genera la clave de acceso SRI de 49 dígitos.

    Estructura (los números son longitudes):
        8  fecha_emision      (ddmmaaaa)
        2  tipo_comprobante   ('01' factura, '04' nota crédito, …)
        13 ruc_emisor
        1  ambiente           ('1' cert, '2' prod)
        3  estab              ('001' por defecto)
        3  pto_emi            ('001' por defecto)
        9  secuencial         (con zero-padding a la izquierda)
        8  codigo_numerico    (8 dígitos random; si None se genera)
        1  tipo_emision       ('1' normal, '2' contingencia)
        1  digito_verificador (módulo 11 sobre los 48 anteriores)
        --
        49 total

    Args:
        fecha_emision: fecha contable. Se formatea a DDMMAAAA.
        tipo_comprobante: uno de TIPO_* ("01", "04", "05", "06", "07").
        ruc: 13 dígitos. Se valida. No debe tener guiones ni espacios.
        ambiente: "1" o "2". Usar AMBIENTE_CERTIFICACION por defecto.
        estab / pto_emi: 3 dígitos cada uno (normalmente "001").
        secuencial: número entero del comprobante. Se padea a 9 dígitos.
        codigo_numerico: 8 dígitos. Si es None, se genera aleatorio.
            Idealmente debería guardarse en DB para reemitir determinísticamente.
        tipo_emision: EMISION_NORMAL ("1") o EMISION_CONTINGENCIA ("2").

    Raises:
        ValueError: si alguno de los campos no cumple longitud/formato.
    """
    # --- fecha ---
    if not isinstance(fecha_emision, date):
        raise ValueError("fecha_emision debe ser datetime.date")
    fecha_str = fecha_emision.strftime("%d%m%Y")

    # --- tipo_comprobante ---
    tipo_comprobante = str(tipo_comprobante).zfill(2)
    if len(tipo_comprobante) != 2 or not tipo_comprobante.isdigit():
        raise ValueError(f"tipo_comprobante inválido: {tipo_comprobante!r}")

    # --- ruc ---
    ruc_limpio = limpiar_ruc(ruc)
    if len(ruc_limpio) != 13:
        raise ValueError(f"RUC debe tener 13 dígitos, recibió {len(ruc_limpio)}: {ruc!r}")

    # --- ambiente ---
    if ambiente not in (AMBIENTE_CERTIFICACION, AMBIENTE_PRODUCCION):
        raise ValueError(f"ambiente debe ser '1' o '2', no {ambiente!r}")

    # --- estab / pto_emi ---
    estab = str(estab).zfill(3)
    pto_emi = str(pto_emi).zfill(3)
    if len(estab) != 3 or not estab.isdigit():
        raise ValueError(f"estab inválido: {estab!r}")
    if len(pto_emi) != 3 or not pto_emi.isdigit():
        raise ValueError(f"pto_emi inválido: {pto_emi!r}")

    # --- secuencial ---
    secuencial_str = str(secuencial).zfill(9)
    if len(secuencial_str) != 9 or not secuencial_str.isdigit():
        raise ValueError(f"secuencial debe ser <=9 dígitos, recibió {secuencial!r}")

    # --- codigo_numerico ---
    if codigo_numerico is None:
        codigo_numerico = f"{random.randint(0, 99_999_999):08d}"
    codigo_numerico = str(codigo_numerico).zfill(8)
    if len(codigo_numerico) != 8 or not codigo_numerico.isdigit():
        raise ValueError(f"codigo_numerico debe ser 8 dígitos: {codigo_numerico!r}")

    # --- tipo_emision ---
    if tipo_emision not in (EMISION_NORMAL, EMISION_CONTINGENCIA):
        raise ValueError(f"tipo_emision debe ser '1' o '2', no {tipo_emision!r}")

    # --- ensamblado (48 dígitos) ---
    cuerpo = (
        fecha_str +        # 8
        tipo_comprobante + # 2
        ruc_limpio +       # 13
        ambiente +         # 1
        estab +            # 3
        pto_emi +          # 3
        secuencial_str +   # 9
        codigo_numerico +  # 8
        tipo_emision       # 1
    )
    if len(cuerpo) != 48:
        # Sanity — si acá rompe es un bug nuestro, no un input malo.
        raise AssertionError(f"cuerpo de clave de acceso debe ser 48 dígitos, es {len(cuerpo)}")

    dv = digito_verificador_modulo_11(cuerpo)
    clave = cuerpo + dv
    assert len(clave) == 49, "clave de acceso debe ser 49 dígitos"
    return clave


def validar_clave_acceso(clave: str) -> bool:
    """Verifica que una clave de acceso de 49 dígitos tenga el DV correcto.

    No valida semántica (RUC existe, ambiente permitido, etc.), sólo la
    estructura y el dígito verificador. Útil para rechazar claves corruptas
    antes de consultar al SRI.
    """
    if not clave or not clave.isdigit() or len(clave) != 49:
        return False
    cuerpo, dv_esperado = clave[:48], clave[48]
    return digito_verificador_modulo_11(cuerpo) == dv_esperado


def desglosar_clave_acceso(clave: str) -> dict:
    """Parte una clave de acceso en sus componentes. Sólo split, no valida.

    Útil para debugging y para el visor de facturas electrónicas.
    """
    if len(clave) != 49:
        raise ValueError(f"clave debe tener 49 dígitos, tiene {len(clave)}")
    return {
        "fecha_emision":    clave[0:8],     # DDMMAAAA
        "tipo_comprobante": clave[8:10],
        "ruc":              clave[10:23],
        "ambiente":         clave[23:24],
        "estab":            clave[24:27],
        "pto_emi":          clave[27:30],
        "secuencial":       clave[30:39],
        "codigo_numerico":  clave[39:47],
        "tipo_emision":     clave[47:48],
        "digito_verificador": clave[48:49],
    }


# =====================================================================
# RUC ecuatoriano
# =====================================================================

_NON_DIGITS = re.compile(r"\D+")


def limpiar_ruc(ruc: str) -> str:
    """Quita guiones, espacios y puntos. No valida DV del RUC."""
    if ruc is None:
        raise ValueError("RUC no puede ser None")
    return _NON_DIGITS.sub("", str(ruc))


def es_ruc_valido(ruc: str) -> bool:
    """Validación estructural de RUC ecuatoriano.

    Reglas mínimas verificables sin consultar al SRI:
        - 13 dígitos.
        - Los dos primeros son el código provincial (01-24).
        - El 13vo debe ser '001' al final (3 dígitos que identifican el establecimiento;
          los primeros 10 son el "número de cédula/identificación").

    NO verifica el dígito verificador del RUC en sí — eso requiere
    distinguir personas naturales (algoritmo módulo 10) de jurídicas
    (módulo 11) y de entidades públicas, y varía. Para facturación
    electrónica la validación estricta la hace el SRI al autorizar.
    """
    ruc = limpiar_ruc(ruc)
    if len(ruc) != 13 or not ruc.isdigit():
        return False
    provincia = int(ruc[:2])
    if not (1 <= provincia <= 24):
        return False
    # Técnicamente otros establecimientos tienen 002, 003, … pero para
    # facturación estándar es siempre 001. Relajar si hace falta.
    return ruc.endswith("001")


# =====================================================================
# Helpers de montos — todo lo que va al XML debe tener exactamente 2
# decimales con punto como separador (el SRI rechaza coma decimal).
# =====================================================================

def fmt_monto(valor) -> str:
    """Formatea un número a string con 2 decimales y punto como separador.

    El XSD del SRI requiere xs:decimal con totalDigits variable pero
    exactamente 2 decimales para importes. `"{:.2f}".format(Decimal)`
    funciona, pero para evitar errores de binding con tipos raros,
    forzamos float (pérdida de precisión aceptable en 2 decimales).
    """
    if valor is None:
        return "0.00"
    try:
        return f"{float(valor):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def fmt_cantidad(valor) -> str:
    """Cantidades en el XSD permiten hasta 6 decimales. Mantenemos 2 si es
    entero/factura típica, o los que trae si es una cantidad fraccional."""
    if valor is None:
        return "0.00"
    try:
        f = float(valor)
    except (TypeError, ValueError):
        return "0.00"
    # 2 decimales por defecto — suficiente para kg de tela.
    return f"{f:.2f}"
