"""El importe desde la cabecera y la escoba de los centavos.

TMT 2026-08-25: el bloque de la factura decía 2.064,48 y el Importe 2.064,49.
Medido sobre 472 documentos del 19 al 25/08: 136 difieren, ninguno por más de
UN centavo. El síntoma: 90 facturas abiertas con ≤2 centavos (USD 0,94).
"""
from __future__ import annotations

from modules.admin_dbase import debug_asinfo_facturas_view as dv
from modules.admin_dbase import facturas_centavos_view as fc

SQL_CARD = """WITH detalle AS (
  SELECT fc.id_documento,
         CASE WHEN fc.id_documento IN (17, 20, 451, 501, 652)
              THEN -1 * (dfc.precio * dfc.cantidad)
              ELSE (dfc.precio * dfc.cantidad)
         END AS usd_line
    FROM factura_cliente fc
   WHERE fc.id_documento IN (7, 17, 20, 251, 451, 501, 652)
)
SELECT CAST(SUM(d.kg_line)  AS DECIMAL(18,3))         AS kg,
       CAST(SUM(d.usd_line) AS DECIMAL(18,2))         AS usd
  FROM detalle d
"""


# --- el cambio de la card --------------------------------------------------

def test_el_importe_pasa_a_salir_de_la_cabecera():
    nuevo, problema = dv._sql_importe_de_cabecera(SQL_CARD)
    assert problema == ""
    assert "usd_cab" in nuevo
    assert "ISNULL(fc.total, 0) - ISNULL(fc.descuento, 0) + ISNULL(fc.impuesto, 0)" in nuevo
    assert "CAST(MAX(d.usd_cab) AS DECIMAL(18,2))" in nuevo
    assert "SUM(d.usd_line)" not in nuevo          # el de afuera se reemplazó
    assert "END AS usd_line" in nuevo              # el de adentro sigue vivo


def test_las_notas_de_credito_siguen_en_negativo():
    """Si el signo se pierde, una devolución suma en vez de restar."""
    nuevo, _ = dv._sql_importe_de_cabecera(SQL_CARD)
    i = nuevo.index("usd_cab")
    bloque = nuevo[max(0, i - 400):i]
    assert "17, 20, 451, 501, 652" in bloque
    assert "-1 *" in bloque


def test_correrlo_dos_veces_no_rompe_nada():
    nuevo, _ = dv._sql_importe_de_cabecera(SQL_CARD)
    otra, problema = dv._sql_importe_de_cabecera(nuevo)
    assert otra is None and problema == ""


def test_si_la_card_cambió_de_forma_se_dice_y_no_se_toca():
    """Nunca reescribir a ciegas una consulta que no se reconoce."""
    nuevo, problema = dv._sql_importe_de_cabecera("SELECT 1")
    assert nuevo is None and problema


# --- la escoba -------------------------------------------------------------

def test_la_escoba_solo_toca_las_que_ya_cobraron_algo():
    """Una factura de dos centavos que nadie pagó es una factura impaga."""
    assert "COALESCE(abono, 0) + COALESCE(retencion, 0) > 0" in fc._SQL


def test_la_escoba_solo_toca_vivas():
    """Una T ya está cerrada y una X está anulada."""
    assert "IN ('Z', 'A')" in fc._SQL


def test_el_umbral_son_dos_centavos():
    assert fc.UMBRAL == 0.02
    assert "saldo <= %s" in fc._SQL and "saldo > 0" in fc._SQL


def test_cierra_por_la_misma_funcion_que_la_pantalla():
    """Con su foto en mov_doble y reversible desde el estado de cuenta."""
    import inspect

    src = inspect.getsource(fc._run)
    assert "factura_cambiar_stat_a_t" in src
    assert "UPDATE" not in src.upper()


def test_sin_aplicar_no_escribe():
    import inspect

    src = inspect.getsource(fc._run)
    assert "if not aplicar:" in src
