"""La conversión de anticipos dejó de desbalancear (TMT 2026-07-31).

Dueña, después de ver la utilidad saltar +79k y −39k en una mañana:
*"es que cuando se convierte debería hacer todo al mismo tiempo para no
desbalancear"*.

El problema no era la conversión sino el DESFASE entre sus dos patas:

  · Asinfo marca la importación recibida  → los kilos entran al STOCK
  · hasta 30 min después, la conversión   → recién ahí sale el ANTICIPO

En el medio, la misma mercadería estaba contada dos veces (anticipo + stock) y
la utilidad quedaba inflada; cuando la conversión corría, se caía de golpe por
el importe completo del anticipo, porque la compra se crea PAGADA y sin deuda:
el activo desaparecía y no entraba nada en su lugar.

Ahora las dos patas usan la MISMA señal —la recepción— así que se mueven en el
mismo instante y la conversión no toca el patrimonio.
"""
from unittest.mock import patch

import pytest

from modules.informes import queries as q


@pytest.fixture(autouse=True)
def _limpio():
    q._ANTIC_RECIBIDOS_ULTIMO_BUENO = None
    q._ANTIC_RECIBIDOS_VISTOS.clear()   # TMT 2026-07-31: memoria de "ya llegó"
    yield
    q._ANTIC_RECIBIDOS_ULTIMO_BUENO = None
    q._ANTIC_RECIBIDOS_VISTOS.clear()


VIVOS = [
    # En tránsito: todavía es un activo.
    {"id_dolares": 1, "cta": "AC", "importe": 100000.0},
    # Ya llegó a bodega: sus kilos ya están en el stock.
    {"id_dolares": 2, "cta": "AI", "importe": 81574.15},
    {"id_dolares": 3, "cta": "AI", "importe": 82540.01},
]


def _recepcion(filas):
    """Simula el cruce con Asinfo: las dos AI están recibidas."""
    for f in filas:
        f["im_numero"] = f"IM-{f['id_dolares']}"
        f["fecha_recepcion_im"] = ("2026-07-31" if f["cta"] == "AI" else None)


def _con_asinfo(recepcion=_recepcion, vivos=None):
    return (
        patch("modules.dolares.queries.anticipos_vivos",
              return_value=[dict(f) for f in (vivos if vivos is not None else VIVOS)]),
        patch("modules.importaciones.service.adjuntar_recepcion_asinfo",
              side_effect=recepcion),
    )


def test_el_anticipo_de_lo_que_ya_llego_no_cuenta_como_activo():
    p1, p2 = _con_asinfo()
    with p1, p2:
        assert q.anticipos_con_mercaderia_recibida() == 164114.16


def test_el_activo_del_balance_los_descuenta():
    p1, p2 = _con_asinfo()
    with p1, p2, patch.object(q.db, "fetch_one", return_value={"total": 2519273.0}):
        # 2.519.273 vivos − 164.114 con mercadería en bodega
        assert q.anticipos() == 2355158.84


def test_lo_que_esta_EN_TRANSITO_sigue_siendo_activo():
    """Sólo sale del activo lo que ya está en la bodega."""
    def solo_transito(filas):
        for f in filas:
            f["im_numero"] = "IM-x"
            f["fecha_recepcion_im"] = None

    p1, p2 = _con_asinfo(recepcion=solo_transito)
    with p1, p2:
        assert q.anticipos_con_mercaderia_recibida() == 0.0


def test_si_asinfo_no_contesta_NO_salta_el_activo():
    """La red: devolver 0 sería re-crear el salto que vinimos a sacar."""
    p1, p2 = _con_asinfo()
    with p1, p2:
        q.anticipos_con_mercaderia_recibida()      # una vuelta buena

    def sin_cruce(filas):
        for f in filas:                            # Asinfo mudo: ni un match
            f["im_numero"] = None
            f["fecha_recepcion_im"] = None

    p1, p2 = _con_asinfo(recepcion=sin_cruce)
    with p1, p2:
        assert q.anticipos_con_mercaderia_recibida() == 164114.16   # el último bueno


def test_sin_ningun_valor_bueno_previo_no_descuenta_nada():
    """Recién arrancada la app y con Asinfo caído: se comporta como antes."""
    def explota(_filas):
        raise RuntimeError("Asinfo caído")

    p1, p2 = _con_asinfo(recepcion=explota)
    with p1, p2:
        assert q.anticipos_con_mercaderia_recibida() == 0.0


def test_sin_anticipos_vivos_no_hay_nada_que_descontar():
    p1, p2 = _con_asinfo(vivos=[])
    with p1, p2:
        assert q.anticipos_con_mercaderia_recibida() == 0.0


def test_la_conversion_pasa_a_ser_NEUTRA():
    """Antes: convertir bajaba el activo. Ahora ese anticipo ya no estaba.

    Se simula el después de la conversión: el anticipo 2 deja de estar vivo.
    El activo de anticipos tiene que quedar IGUAL que antes de convertir.
    """
    p1, p2 = _con_asinfo()
    with p1, p2, patch.object(q.db, "fetch_one", return_value={"total": 264114.16}):
        antes = q.anticipos()          # 264.114,16 − 164.114,16 = 100.000

    vivos_post = [VIVOS[0], VIVOS[2]]  # el 2 se convirtió (st='B')
    p1, p2 = _con_asinfo(vivos=vivos_post)
    with p1, p2, patch.object(q.db, "fetch_one", return_value={"total": 182540.01}):
        despues = q.anticipos()        # 182.540,01 − 82.540,01 = 100.000

    assert antes == despues == 100000.0
