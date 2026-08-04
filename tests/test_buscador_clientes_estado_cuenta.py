"""El buscador de clientes del estado de cuenta (informes.buscar_clientes).

Reporte dueña 2026-08-04 sobre `/informes/estado-cuenta?q=IRENE%25CONDOR`:
*"esto no funciona si solo busco condor, podes hacerlo mejor"*.

Acá se testea el ARMADO de la query (con la DB mockeada) y el fallback
"¿quisiste decir?". La semántica del match por palabras vive en
tests/test_busqueda_por_palabras.py.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from modules.informes import queries as iq

_RAIZ = Path(__file__).resolve().parents[1]


def _capturar(resultados):
    """Mockea db.fetch_all y guarda cada (sql, params) que se ejecutó."""
    llamadas: list[tuple[str, dict]] = []
    cola = list(resultados)

    def fake(sql, params=None, conn=None):
        llamadas.append((sql, params or {}))
        return cola.pop(0) if cola else []

    return llamadas, fake


def test_la_query_busca_por_palabras_y_sin_acentos():
    llamadas, fake = _capturar([[{"codigo_cli": "ICH"}]])
    with patch.object(iq.db, "fetch_all", side_effect=fake):
        iq.buscar_clientes("irene condor")
    sql, params = llamadas[0]
    assert "TRANSLATE(" in sql, "el nombre tiene que compararse sin acentos"
    assert "ILIKE" not in sql, "quedó el ILIKE de un solo patrón %q%"
    # una entrada por palabra + el ranking + el límite
    assert {"%IRENE%", "%CONDOR%"} <= set(params.values())
    assert params["limite"] == 25


def test_busca_tambien_por_ruc():
    llamadas, fake = _capturar([[{"codigo_cli": "ICH"}]])
    with patch.object(iq.db, "fetch_all", side_effect=fake):
        iq.buscar_clientes("170123456")
    assert "c.ruc" in llamadas[0][0]


def test_sin_termino_no_toca_la_base():
    llamadas, fake = _capturar([])
    with patch.object(iq.db, "fetch_all", side_effect=fake):
        assert iq.buscar_clientes("   ") == []
    assert llamadas == [], "una búsqueda vacía no debería consultar nada"


def test_si_no_hay_nada_exacto_cae_al_quisiste_decir():
    """Typo: 'chicaisa' tiene que traer igual a CHICAIZA, marcado aprox."""
    llamadas, fake = _capturar([
        [],                                                   # búsqueda exacta
        [{"codigo_cli": "ICH", "nombre": "IRENE CHICAIZA CONDOR"}],  # padrón
        [{"codigo_cli": "ICH", "nombre": "IRENE CHICAIZA CONDOR",
          "saldo": 1550.41}],                                 # filas finales
    ])
    with patch.object(iq.db, "fetch_all", side_effect=fake):
        filas = iq.buscar_clientes("chicaisa")
    assert len(filas) == 1 and filas[0]["aprox"] is True
    assert "ANY(" in llamadas[2][0], "las filas aprox se piden por código"


def test_el_quisiste_decir_no_se_dispara_con_dos_letras():
    """Con 2-3 letras el parecido trae basura: no vale la pena ni la query."""
    llamadas, fake = _capturar([[]])
    with patch.object(iq.db, "fetch_all", side_effect=fake):
        assert iq.buscar_clientes("ab") == []
    assert len(llamadas) == 1


def test_el_quisiste_decir_no_inventa_resultados():
    llamadas, fake = _capturar([[], [{"codigo_cli": "ICH", "nombre": "IRENE"}]])
    with patch.object(iq.db, "fetch_all", side_effect=fake):
        assert iq.buscar_clientes("zzzzzzzz") == []
    assert len(llamadas) == 2, "consultó el padrón y paró ahí"


# ── Generalización: que no quede ningún buscador de NOMBRE con el bug ──────

def test_ningun_buscador_de_nombre_quedo_con_el_patron_de_un_solo_like():
    """El bug era estructural, no de una pantalla.

    Si mañana alguien agrega otro `UPPER(x.nombre) LIKE UPPER(%(like)s)`, la
    dueña vuelve a tener que tipear "IRENE%CONDOR" en esa pantalla. Este test
    recorre TODAS las queries del repo (regla del skill: generalizar, no
    parchear el link que ella pisó).
    """
    ofensores = []
    for archivo in sorted(_RAIZ.glob("modules/*/queries.py")):
        for n, linea in enumerate(archivo.read_text().split("\n"), 1):
            texto = linea.strip()
            if texto.startswith("--") or texto.startswith("#"):
                continue
            if "nombre" not in texto.lower():
                continue
            if "LIKE" in texto and ("%(like)s" in texto or "ILIKE %s" in texto):
                ofensores.append(f"{archivo.relative_to(_RAIZ)}:{n}: {texto}")
    # scintela.banco es la única excepción: son 15 bancos, no clientes.
    ofensores = [o for o in ofensores if "no_banco" not in o]
    assert not ofensores, (
        "buscadores por nombre con UN solo patrón %q%% (usá "
        "modules/_lib/busqueda.condicion):\n  " + "\n  ".join(ofensores)
    )


# ── 5. Que ningún SQL salga a la DB con el sentinela sin resolver ──────────

def test_ninguna_query_sale_con_el_sentinela_sin_reemplazar():
    """El patrón `__TEXTO_MATCH__` se resuelve con .replace() antes de la DB.

    Si alguien agrega el sentinela al SQL y se olvida del `.replace()`,
    Postgres tira `column "__texto_match__" does not exist` en producción y
    la pantalla se cae. Acá se ejecuta cada buscador con la DB mockeada y se
    mira el SQL REAL que se habría ejecutado.
    """
    from modules.activos import queries as activos_q
    from modules.cheques import queries as cheques_q
    from modules.clientes import queries as clientes_q
    from modules.facturas import queries as facturas_q
    from modules.gastos import queries as gastos_q
    from modules.posdat import queries as posdat_q
    from modules.proformas import queries as proformas_q
    from modules.proveedores import queries as proveedores_q
    from modules.retenciones import queries as retenciones_q

    casos = [
        (iq, lambda: iq.buscar_clientes("irene condor")),
        (clientes_q, lambda: clientes_q.buscar("irene condor")),
        (clientes_q, lambda: clientes_q.contar("irene condor")),
        (clientes_q, lambda: clientes_q.directorio("irene condor")),
        (proveedores_q, lambda: proveedores_q.buscar("hilos del sur")),
        (proveedores_q, lambda: proveedores_q.contar("hilos del sur")),
        (facturas_q, lambda: facturas_q.buscar("irene condor")),
        (facturas_q, lambda: facturas_q.contar_filtrado("irene condor")),
        (cheques_q, lambda: cheques_q.buscar("irene condor")),
        (cheques_q, lambda: cheques_q.total_buscar("irene condor")),
        (activos_q, lambda: activos_q.buscar("torno cnc")),
        (gastos_q, lambda: gastos_q.buscar("flete guayaquil")),
        (posdat_q, lambda: posdat_q.buscar(q="hilos del sur")),
        (posdat_q, lambda: posdat_q.resumen(q="hilos del sur")),
        (proformas_q, lambda: proformas_q.buscar("irene condor")),
        (retenciones_q, lambda: retenciones_q.buscar("irene condor")),
    ]
    for modulo, correr in casos:
        vistos: list[str] = []

        def espia(sql, params=None, conn=None, _v=vistos):
            _v.append(sql)
            return []

        def espia_one(sql, params=None, conn=None, _v=vistos):
            _v.append(sql)
            return {}

        with patch.object(modulo.db, "fetch_all", side_effect=espia), \
             patch.object(modulo.db, "fetch_one", side_effect=espia_one):
            correr()
        assert vistos, f"{modulo.__name__} no ejecutó ninguna query"
        for sql in vistos:
            assert "__TEXTO_MATCH__" not in sql, modulo.__name__
            assert "__NOMBRE_CLI__" not in sql, modulo.__name__
            assert "__NOMBRE_PRV__" not in sql, modulo.__name__
