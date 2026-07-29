"""Match anticipo↔importación por AÑO — TMT 2026-07-29 (dueña).

El número de la importación se REUSA cada campaña ("AC 58" aparece 3 veces en
las últimas 400 importaciones). El anticipo lleva el año en el concepto
(`58/26`) y el año MANDA sobre la fecha.

Casos reales de producción usados como fixtures (verificados en vivo el
29/07/2026):

  IM-0000622  2026-07-20  "ACMT/EXP/2026-27/8368 ( AC 58)"   → año 2026 (campaña)
  IM-0000527  2026-02-11  "INV HY336-26-1 ( AC 58)"          → año 2026 (fecha)
  IM-0000380  2025-04-02  "ACMT/EXP/2025-26/6611 ( AC 58)"   → año 2025 (campaña)
  IM-0000495  2025-10-13  "ACMT/EXP/205-26/7324 ( AC 77)"    → año 2025 (typo)
"""
from concepto_parser import anio_importacion, parse_ref_anticipo
from modules.importaciones.service import _nearest_import

IM622 = {"im_numero": "IM-0000622", "fecha": "2026-07-20",
         "nota": "ACMT/EXP/2026-27/8368 ( AC 58)"}
IM527 = {"im_numero": "IM-0000527", "fecha": "2026-02-11",
         "nota": "INV HY336-26-1 ( AC 58)"}
IM380 = {"im_numero": "IM-0000380", "fecha": "2025-04-02",
         "nota": "ACMT/EXP/2025-26/6611 ( AC 58)"}
IM495 = {"im_numero": "IM-0000495", "fecha": "2025-10-13",
         "nota": "ACMT/EXP/205-26/7324 ( AC 77)"}


# ── parse_ref_anticipo ──────────────────────────────────────────────────────

def test_ref_con_anio_dos_digitos():
    assert parse_ref_anticipo("58/26 SALDO") == {"numero": 58, "anio": 2026}


def test_ref_con_anio_cuatro_digitos_y_espacios():
    assert parse_ref_anticipo("58 / 2026") == {"numero": 58, "anio": 2026}


def test_ref_con_codigo_adelante():
    assert parse_ref_anticipo("AC 58/26") == {"numero": 58, "anio": 2026}


def test_ref_sin_anio_se_comporta_como_antes():
    """Los anticipos viejos (sin año) no cambian: número sí, año None."""
    assert parse_ref_anticipo("31 SALDO") == {"numero": 31, "anio": None}
    assert parse_ref_anticipo("AC 95") == {"numero": 95, "anio": None}
    assert parse_ref_anticipo("22 MAPFRE") == {"numero": 22, "anio": None}


def test_ref_vacia():
    assert parse_ref_anticipo("") == {"numero": None, "anio": None}
    assert parse_ref_anticipo(None) == {"numero": None, "anio": None}


def test_ref_no_parte_un_numero_largo():
    """"20250191201" no es "2025/0191201"."""
    assert parse_ref_anticipo("INV 20250191201")["anio"] is None


# ── anio_importacion ────────────────────────────────────────────────────────

def test_anio_de_campana_gana_a_la_fecha():
    """La campaña 2026-27 arranca en 2026 aunque la factura sea de julio."""
    a = anio_importacion(IM622["nota"], IM622["fecha"])
    assert a == {"anio": 2026, "de_campana": True}


def test_anio_campana_con_typo_del_proveedor():
    """"205-26" (falta un dígito) = campaña 2025-26 → arranca en 2025."""
    assert anio_importacion(IM495["nota"], IM495["fecha"])["anio"] == 2025
    assert anio_importacion("ACMT/EXP/202-27/8000 ( AC 1)", "2026-01-01") == {
        "anio": 2026, "de_campana": True}


def test_anio_sin_campana_cae_a_la_fecha():
    """"INV HY336-26-1" NO es una campaña (no va entre barras)."""
    assert anio_importacion(IM527["nota"], IM527["fecha"]) == {
        "anio": 2026, "de_campana": False}


def test_anio_sin_nota_ni_fecha():
    assert anio_importacion(None, None)["anio"] is None


# ── _nearest_import con año ────────────────────────────────────────────────

def test_caso_real_ac58_va_a_la_campana_nueva():
    """El caso que lo motivó: el anticipo del 21/04/26 iba a IM-0000527 (más
    cercano en fecha) cuando es de IM-0000622, la de la campaña 2026-27."""
    cands = [dict(IM622), dict(IM527), dict(IM380)]
    assert _nearest_import(cands, "2026-04-21", anio=2026)["im_numero"] == "IM-0000622"


def test_sin_anio_sigue_ganando_la_fecha_mas_cercana():
    """Sin año explícito NO cambia nada (los 136 anticipos vivos de hoy)."""
    cands = [dict(IM622), dict(IM527), dict(IM380)]
    assert _nearest_import(cands, "2026-04-21")["im_numero"] == "IM-0000527"


def test_anio_sin_candidata_devuelve_none():
    """AC 77: no hay ninguna de 2026 → sin match, en vez de pegarse a la de
    2025. Es lo que frena la conversión automática equivocada."""
    assert _nearest_import([dict(IM495)], "2026-06-18", anio=2026) is None


def test_anio_viejo_encuentra_la_vieja():
    assert _nearest_import([dict(IM622), dict(IM380)], "2025-05-01",
                           anio=2025)["im_numero"] == "IM-0000380"


def test_una_sola_del_anio_no_necesita_fecha():
    assert _nearest_import([dict(IM622), dict(IM380)], None,
                           anio=2026)["im_numero"] == "IM-0000622"


def test_desempate_por_fecha_entre_dos_de_la_misma_campana():
    """Dos partidas de la misma campaña (los `--1`/`--2` de Asinfo): gana la
    de fecha más cercana, sin ventana de 300 días."""
    a = {"im_numero": "IM-A", "fecha": "2026-01-10", "nota": "ACMT/EXP/2026-27/1 ( AC 5)"}
    b = {"im_numero": "IM-B", "fecha": "2026-11-20", "nota": "ACMT/EXP/2026-27/2 ( AC 5)"}
    assert _nearest_import([a, b], "2026-11-01", anio=2026)["im_numero"] == "IM-B"
    assert _nearest_import([a, b], "2026-01-05", anio=2026)["im_numero"] == "IM-A"


def test_la_ventana_de_300_dias_no_aplica_con_anio():
    """Con año explícito la ventana no corta: una importación de enero y un
    anticipo de diciembre del MISMO año siguen matcheando."""
    a = {"im_numero": "IM-A", "fecha": "2026-01-10", "nota": "ACMT/EXP/2026-27/1 ( AC 5)"}
    assert _nearest_import([a], "2026-12-20", anio=2026)["im_numero"] == "IM-A"
