"""Una factura del proveedor repartida en varias importaciones, con la plata
colgada de una sola.

TMT 2026-08-21 (dueña, sobre MH 68/69/70): *"llegaron varias importaciones 68,
69, 70; el pago de 160k era de todo ese hilo. Se registra que llegó pero no que
valía eso"*.

Los 160.400,78 quedaron en MH 68 (24.300 kg → 6,60 US$/kg) y MH 69 y MH 70
salieron "sin cargar". Los 47.580 kg de esas dos están en la bodega sin plata y
`mov_hilado_valuacion` los deja fuera del divisor a propósito, así que la tarifa
del hilado —y con ella todo el stock— queda alta hasta que se acomode.

Las filas de acá NO se escriben a mano: se arman con el parser y el agrupador
de verdad (`parse_nota_importacion` + `adjuntar_grupo_partidas`), que es lo que
separa este caso del de una importación PARTIDA (AI 15), donde las dos mitades
comparten código y son un solo grupo.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from concepto_parser import parse_nota_importacion
from filters import today_ec
from modules.importaciones import service as svc
from modules.importaciones import vigilancia as vig


def _filas(spec, *, dias_atras=0, recepcion=None):
    """`spec` = [(nota, kg, importe), ...] → filas como las devuelve el cruce."""
    rec = recepcion or str(today_ec() - timedelta(days=dias_atras))
    rows = []
    for i, (nota, kg, importe) in enumerate(spec, start=1):
        r = {
            "im_numero": f"IM-{i:07d}",
            "nota": nota,
            "kg": float(kg),
            "fecha": str(today_ec() - timedelta(days=60)),
            "fecha_recepcion": rec if isinstance(rec, str) else str(rec),
            "prov_cod_asinfo": "MH",
            "recibida": True,
            "compra": ({"items": [{"id_compra": i, "fecha": rec,
                                   "importe": float(importe)}]}
                       if importe else None),
            "anticipo": None,
        }
        c = parse_nota_importacion(nota)
        r.update(codigo=c.get("codigo"), prov=c.get("prov"),
                 numero=c.get("numero"), numero_hasta=c.get("numero_hasta"))
        rows.append(r)
    svc.adjuntar_grupo_partidas(rows)
    return rows


def _casos(rows, techo=None):
    return vig.facturas_con_plata_en_una_sola(rows=rows, techo=techo)


# El caso real ───────────────────────────────────────────────────────────────
MH_68_69_70 = [
    ("INV HY3821-26-1 ( MH 68 )", 24300, 160400.78),
    ("INV HY3821-26-1 ( MH 69)", 23430, 0),
    ("INV HY3821-26-1 ( MH 70)", 24150, 0),
]


def test_mh_68_69_70_avisa_con_los_numeros_de_la_dueña():
    casos = _casos(_filas(MH_68_69_70))
    assert len(casos) == 1
    c = casos[0]
    assert c["factura"] == "INV HY3821-26"
    assert c["codigos"] == ["MH 68", "MH 69", "MH 70"]
    assert c["con_plata"] == ["MH 68"]
    assert c["sin_plata"] == ["MH 69", "MH 70"]
    assert c["importe"] == 160400.78
    assert c["kg"] == 71880.0
    assert c["kg_con_plata"] == 24300.0
    assert c["kg_sin_plata"] == 47580.0
    # Los dos números de la conversación: el que muestra la pantalla y el que
    # daría la factura entera.
    assert c["usd_kg"] == 6.6009
    assert c["usd_kg_repartido"] == 2.2315


def test_avisa_el_mismo_dia_que_llega():
    """A diferencia de la alarma de "le falta plata" (30 días de maduración),
    ésta no espera: no está diciendo "todavía no cargaron", está diciendo "lo
    cargado no le corresponde a esos kilos". La tarifa del hilado ya está alta
    hoy."""
    assert len(_casos(_filas(MH_68_69_70, dias_atras=0))) == 1


# Lo que NO tiene que avisar ─────────────────────────────────────────────────
def test_si_cada_una_tiene_su_plata_no_avisa():
    casos = _casos(_filas([
        ("INV HY3821-26-1 ( MH 68 )", 24300, 80200.39),
        ("INV HY3821-26-1 ( MH 69)", 23430, 77000.00),
        ("INV HY3821-26-1 ( MH 70)", 24150, 79000.00),
    ]))
    assert casos == []


def test_mientras_se_van_cargando_de_a_una_no_avisa():
    """Andrés carga la plata de un contenedor por vez: la primera queda en
    banda (3,30) y las otras en cero. Eso es maduración normal, no una mala
    atribución — y es el falso positivo que llenaría la campanita."""
    casos = _casos(_filas([
        ("INV HY3821-26-1 ( MH 68 )", 24300, 80200.39),
        ("INV HY3821-26-1 ( MH 69)", 23430, 0),
        ("INV HY3821-26-1 ( MH 70)", 24150, 0),
    ]))
    assert casos == []


def test_si_ninguna_tiene_plata_no_avisa():
    """Sin un solo movimiento no hay nada que comparar: eso lo mira (a los 30
    días) `importaciones_fuera_de_banda`, y avisar acá era el ruido de 2024."""
    assert _casos(_filas([
        ("INV HY3821-26-1 ( MH 68 )", 24300, 0),
        ("INV HY3821-26-1 ( MH 69)", 23430, 0),
    ])) == []


def test_una_importacion_cara_pero_bien_cargada_no_avisa():
    """AC 57 real: 71.091,56 US$ / 20.034 kg = 3,55 — arriba de la banda, pero
    bien cargada. Repartirla entre los 40.000 kg de la factura la mandaría a
    1,77, o sea MÁS LEJOS de la banda: eso dice que el número alto no es una
    mala atribución. Sin este guard, la alarma salta sobre toda importación
    cara que comparta factura con una que todavía no se cargó."""
    assert _casos(_filas([
        ("ACMT/EXP/2026-27/8202 ( AC 57 )", 20034, 71091.56),
        ("ACMT/EXP/2026-27/8202 ( AC 58 )", 20000, 0),
    ])) == []


def test_las_dos_mitades_de_una_partida_no_son_dos_importaciones():
    """AI 15: IM-571 e IM-572 son la MISMA mercadería partida en dos documentos
    (mismo código, misma factura), y el costo vive en una sola mitad. Eso ya lo
    resuelve el agrupador de partidas: es UN grupo, no dos importaciones, y acá
    no tiene que decir nada."""
    filas = _filas([
        ("AYF02653 ( AI 15 ) ----1", 11289.39, 0),
        ("AYF02653 ( AI 15 ) ----2", 11289.39, 74000.00),
    ])
    assert len({f["grupo_id"] for f in filas}) == 1      # el agrupador ya juntó
    assert _casos(filas) == []


def test_una_sola_importacion_por_factura_no_avisa():
    assert _casos(_filas([("INV HY3821-26-1 ( MH 68 )", 24300, 160400.78)])) == []


def test_llegadas_de_meses_distintos_no_se_juntan():
    """Dos campañas que reusan el número de factura no son una sola llegada."""
    filas = _filas([("INV HY3821-26-1 ( MH 68 )", 24300, 160400.78)],
                   recepcion="2026-08-21")
    filas += _filas([("INV HY3821-26-1 ( MH 69)", 23430, 0)],
                    recepcion="2026-06-10")
    assert _casos(filas, techo=0) == []


def test_no_se_mira_lo_que_esta_en_transito():
    filas = _filas(MH_68_69_70)
    for f in filas:
        f["recibida"] = False
    assert _casos(filas) == []


def test_el_techo_de_antiguedad_no_trae_el_historico():
    """La misma lección del 31/07: el día que esto se estrene no puede aparecer
    un inventario de casos viejos en la campanita."""
    assert _casos(_filas(MH_68_69_70, dias_atras=200)) == []
    assert len(_casos(_filas(MH_68_69_70, dias_atras=200), techo=0)) == 1


def test_un_anticipo_tambien_cuenta_como_plata():
    filas = _filas([
        ("INV HY3821-26-1 ( MH 68 )", 24300, 0),
        ("INV HY3821-26-1 ( MH 69)", 23430, 0),
    ])
    filas[0]["anticipo"] = {"items": [{"fecha": str(today_ec()),
                                       "importe": 160400.78}]}
    casos = _casos(filas)
    assert len(casos) == 1 and casos[0]["importe"] == 160400.78


# Fail-soft ─────────────────────────────────────────────────────────────────
def test_si_no_se_puede_leer_no_inventa():
    with patch("modules.importaciones.service.importaciones_con_cruce",
               side_effect=RuntimeError("asinfo caido")):
        assert vig.facturas_con_plata_en_una_sola() == []


# El aviso ──────────────────────────────────────────────────────────────────
def test_avisa_una_vez_por_factura_y_a_la_campanita():
    vig._ultima_corrida = 0.0
    caso = _casos(_filas(MH_68_69_70))[0]
    vistos = []
    with patch.object(vig, "_leer_importaciones", return_value=[]), \
         patch.object(vig, "importaciones_fuera_de_banda", return_value=[]), \
         patch.object(vig, "facturas_con_plata_en_una_sola", return_value=[caso]), \
         patch("modules.avisos.queries.avisar",
               side_effect=lambda **kw: vistos.append(kw) or True):
        out = vig.revisar_si_toca()
    assert out["avisados"] == 1 and out["facturas"] == 1
    a = vistos[0]
    assert a["fuente"] == "importaciones" and a["nivel"] == "alerta"
    # Idempotente por FACTURA: una vez, no una por importación.
    assert a["clave"] == "import-factura-en-una:INV HY3821-26"
    # El "ver →" abre las TRES: la factura del proveedor está adentro de la
    # nota de cada una, así que buscarla las trae a las tres.
    assert a["url"] == "/importaciones?anio=todos&q=INV+HY3821-26"
    # El texto, palabra por palabra (dueña 2026-08-26), y sin los IM.
    assert a["titulo"] == "MH 68, MH 69, MH 70 · toda la plata quedó en MH 68"
    assert len(a["titulo"]) <= 200
    assert a["detalle"] == (
        "Una factura (INV HY3821-26) llegó en 3 importaciones. Los "
        "US$ 160.400,78 cargados quedaron en MH 68: 6,60 el kilo. "
        "Repartidos entre las 3, 2,23.")
    assert "IM-" not in a["titulo"] + a["detalle"]


def test_los_dos_chequeos_comparten_una_sola_lectura():
    """Una corrida = una lectura de Asinfo, no dos."""
    vig._ultima_corrida = 0.0
    with patch("modules.importaciones.service.importaciones_con_cruce",
               return_value=[]) as leer, \
         patch("modules.avisos.queries.avisar", return_value=True):
        vig.revisar_si_toca()
    assert leer.call_count == 1
