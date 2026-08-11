"""El agrupado de impuestos no puede tragarse cobranzas de clientes.

Historia (07/08/2026, encontrado el 11/08): el tab Impuestos mandaba al
servidor POSICIONES dentro de `real_only` — la lista de filas del banco sin
match. El endpoint re-corre el matcher en cada POST, así que apenas algo se
concilia esa lista se acorta y las mismas posiciones apuntan a OTRAS filas.
El botón Confirmar del preview no estaba blindado, entró dos veces con 0,66 s
de diferencia, y la segunda agrupó cinco transferencias de clientes por
$7.404,88 debajo del rótulo "Comisiones e impuestos". Los clientes quedaron
debiendo plata que ya habían pagado.

Dos defensas, un test para cada una:
  1. `resolver_por_firmas` identifica la fila por su CONTENIDO, así que no se
     corre aunque la lista cambie abajo.
  2. `crear_transaccion_agrupada_desde_reals` rechaza lo que no sea comisión,
     aunque le lleguen las filas equivocadas por cualquier otro camino.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest

from modules.conciliacion import sesion as _sesion
from modules.conciliacion.matcher_banco import crear_transaccion_agrupada_desde_reals
from modules.conciliacion.parser_banco import MovBanco


def _mov(concepto: str, monto: str, doc: str, tipo: str = "C", dia: int = 5) -> MovBanco:
    return MovBanco(
        fecha=date(2026, 8, dia),
        concepto=concepto,
        documento=doc,
        monto=Decimal(monto),
        saldo=Decimal("0"),
        codigo="001045",
        tipo=tipo,
        oficina="AG. NORTE",
    )


# Las cinco filas reales del extracto del 05/08 que quedaron adentro del bulto.
COBRANZAS = [
    _mov("TRANSFERENCIA DIRECTA DE MARROQUIN ESPINOSA MARCO ANTONIO", "1142.96", "53443956"),
    _mov("2608050E4MXN-BANCO PI-PAG-16359728987", "72.30", "56804542"),
    _mov("TRANSFERENCIA DIRECTA DE ONATE ONATE ANDRES DAVID", "3099.52", "71519723"),
    _mov("TRANSFERENCIA INTERBANCARIA DE DAYIO SPORTS", "2568.54", "55685078"),
    _mov("TRANSFERENCIA DIRECTA DE ERAZO MELENDREZ YOLANDA GRACIELA", "521.56", "59356148"),
]

COMISIONES = [
    _mov("COMISION MANTENIMIENTO CUENTA", "12.50", "9001", tipo="D"),
    _mov("IVA COMISION", "1.88", "9002", tipo="D"),
    _mov("IMPUESTO SALIDA DIVISAS", "50.35", "9003", tipo="D"),
]


# ── Defensa 1: la firma no se corre cuando la lista de abajo cambia ──


def test_la_firma_encuentra_la_fila_aunque_la_lista_se_acorte():
    """Es el corazón del bug: el índice 2 dejó de ser Oñate."""
    todas = COMISIONES + COBRANZAS
    sig_onate = _sesion.firma_mov(COBRANZAS[2])

    # Antes de conciliar nada, el índice 5 y la firma apuntan al mismo lugar.
    assert todas[5] is COBRANZAS[2]

    # Se concilian las tres comisiones → la lista se acorta.
    quedan = COBRANZAS[:]
    assert quedan[5 - 3] is COBRANZAS[2]  # el índice sobrevivió de casualidad

    # Ahora se concilian dos cobranzas más: el índice ya miente.
    quedan_menos = [COBRANZAS[0], COBRANZAS[2], COBRANZAS[4]]
    subset_por_idx = quedan_menos[2]
    assert subset_por_idx is not COBRANZAS[2], "el índice apunta a otra fila"

    # La firma, en cambio, sigue encontrando a Oñate en cualquiera de las tres.
    for lista in (todas, quedan, quedan_menos):
        subset, faltantes = _sesion.resolver_por_firmas(lista, [sig_onate])
        assert faltantes == []
        assert subset == [COBRANZAS[2]]


def test_una_firma_que_ya_no_esta_se_reporta_no_se_ignora():
    """El silencio fue lo que dejó pasar el bug: 3 de 8 índices se
    descartaron sin decir nada. Una firma que no resuelve tiene que VOLVER."""
    sigs = [_sesion.firma_mov(m) for m in COMISIONES]
    subset, faltantes = _sesion.resolver_por_firmas(COMISIONES[:1], sigs)
    assert len(subset) == 1
    assert len(faltantes) == 2


def test_la_firma_distingue_dos_filas_del_mismo_dia_y_monto():
    a = _mov("COMISION A", "10.00", "111", tipo="D")
    b = _mov("COMISION B", "10.00", "222", tipo="D")
    assert _sesion.firma_mov(a) != _sesion.firma_mov(b)
    subset, faltantes = _sesion.resolver_por_firmas([a, b], [_sesion.firma_mov(b)])
    assert subset == [b] and faltantes == []


# ── Defensa 2: el backstop de categoría ──


def test_agrupar_cobranzas_de_clientes_explota():
    with pytest.raises(ValueError) as exc:
        crear_transaccion_agrupada_desde_reals(
            no_banco=10, reals=COBRANZAS, usuario="test",
        )
    msg = str(exc.value)
    assert "sólo para comisiones" in msg
    assert "ONATE" in msg.upper()
    assert "Cobranza" in msg


def test_una_sola_cobranza_colada_entre_comisiones_tambien_explota():
    """No alcanza con mirar el total: una intrusa entre nueve buenas es
    exactamente cómo se cuela la plata de un cliente."""
    with pytest.raises(ValueError) as exc:
        crear_transaccion_agrupada_desde_reals(
            no_banco=10, reals=COMISIONES + [COBRANZAS[2]], usuario="test",
        )
    assert "3,099.52" in str(exc.value)


def test_el_neto_cero_sigue_explotando_por_su_propio_motivo():
    par = [
        _mov("COMISION X", "10.00", "1", tipo="C"),
        _mov("COMISION X", "10.00", "2", tipo="D"),
    ]
    with pytest.raises(ValueError, match="compensan"):
        crear_transaccion_agrupada_desde_reals(no_banco=10, reals=par, usuario="test")


def test_reals_vacio_sigue_explotando():
    with pytest.raises(ValueError, match="vacío"):
        crear_transaccion_agrupada_desde_reals(no_banco=10, reals=[], usuario="test")


# ── El espejo: la firma del HTML y la del server tienen que ser LA MISMA ──


def test_la_firma_del_template_es_igual_a_la_del_server():
    """`data-sig` se arma en Jinja y `firma_mov` en Python: son dos
    implementaciones de la misma cosa. Si una cambia el formato (el `%.2f`,
    el orden, el separador), las firmas dejan de resolver y todo el tab cae
    silenciosamente al fallback por índice — o sea, vuelve el bug.
    """
    import pathlib

    from jinja2 import Environment, FileSystemLoader

    import filters as _filters

    dirs = [str(p) for p in pathlib.Path(".").glob("modules/*/templates")] + ["templates"]
    env = Environment(loader=FileSystemLoader(dirs), autoescape=True)
    env.globals.update(url_for=lambda *a, **k: "#", csrf_token=lambda: "x")
    for nombre in dir(_filters):
        fn = getattr(_filters, nombre)
        if callable(fn) and not nombre.startswith("_"):
            env.filters[nombre] = fn

    tpl = env.get_template("conciliacion/_banco_v2_tab_impuestos.html")
    # Los montos tienen que incluir casos donde el formato IMPORTA: un
    # Decimal("12.5") crudo imprime "12.5" y el server firma "12.50". Con
    # montos de dos decimales exactos el test no distingue nada.
    casos = [
        COMISIONES[0], COMISIONES[2], COBRANZAS[3],
        _mov("COMISION REDONDA", "12.5", "9100", tipo="D"),
        _mov("IVA COMISION", "40", "9101", tipo="D"),
        _mov("IMPUESTO", "1234.5", "9102", tipo="D"),
    ]
    for mov in casos:
        html = tpl.render(
            buckets={"impuestos": [{"mov": mov, "cat": None, "idx": 0}]},
            sesion={"id": 1},
        )
        sig_html = re.search(r'data-sig="([^"]+)"', html)
        assert sig_html, "el checkbox perdió el data-sig"
        assert sig_html.group(1) == _sesion.firma_mov(mov), (
            "la firma del HTML y la del server se desincronizaron"
        )
