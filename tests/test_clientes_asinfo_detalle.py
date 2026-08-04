"""/admin/clientes-asinfo/<codigo> — el reparto de facturas por RUC.

TMT 2026-08-04. Para 5 de los 7 códigos repetidos que quedaron abiertos,
saber QUÉ hacer alcanza. Para **BLP** (75 facturas + 3 cheques) y **BRC**
(18) no: los movimientos guardan el CÓDIGO y no la ficha, así que desde PC
solo no hay forma de saber cuál factura es de cuál empresa — y por eso
`plan_cambio_codigo` rechaza el cambio.

El dato existe del otro lado: **Asinfo factura contra el RUC**. Estos tests
fijan el cruce que reparte las facturas, y sobre todo su límite: si alguna
factura no aparece en Asinfo, el reparto **no** se declara concluyente. Un
reparto incompleto presentado como completo es peor que no repartir, porque
la decisión de renombrar se toma mirando justamente esos totales.
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.admin_dbase import clientes_asinfo_detalle_view as d  # noqa: E402

RAMIRO = {"id_cliente": 28929, "nombre": "RAMIRO AYO", "ruc": "1705332268001", "direccion1": "AV. QUITO 100"}
BLANCA = {"id_cliente": 29142, "nombre": "BLANCA PENAHERRERA", "ruc": "1801710425001", "direccion1": ""}


def factura(numf, importe, saldo=0.0, completo=""):
    return {"numf": numf, "numf_completo": completo, "fecha": "2026-07-01",
            "importe": importe, "saldo": saldo, "stat": "Z"}


# ---------------------------------------------------------------------------
# numero_corto — la clave del cruce
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entrada,esperado", [
    ("001-099-000180441", 180441),
    ("180441", 180441),
    (180441, 180441),
    ("001-099-000000001", 1),
])
def test_saca_el_secuencial_del_numero_sri(entrada, esperado):
    assert d.numero_corto(entrada) == esperado


@pytest.mark.parametrize("basura", [None, "", "   ", "abc", "001-099-", "N/A"])
def test_numero_ilegible_no_cruza_con_nada(basura):
    """Devolver 0 acá haría que todas las basuras cruzaran entre sí."""
    assert d.numero_corto(basura) is None


# ---------------------------------------------------------------------------
# Las SQL contra Asinfo — se interpolan a mano, así que el filtro es el guard
# ---------------------------------------------------------------------------

def test_solo_entran_rucs_de_10_digitos_a_la_sql():
    sql = d.sql_facturas(["1705332268", "1801710425", "'; DROP TABLE empresa--", "123", ""])
    dentro_del_in = sql.split(" IN (")[1].split(")")[0]
    assert dentro_del_in == "'1705332268', '1801710425'"
    assert "DROP" not in sql


def test_sin_rucs_usables_no_se_arma_consulta():
    """Un `IN ()` vacío es SQL inválido; mejor no preguntar."""
    assert d.sql_facturas([]) == ""
    assert d.sql_empresas(["abc"]) == ""


def test_las_facturas_anuladas_no_deciden_de_quien_es_el_codigo():
    assert "fc.estado <> 0" in d.sql_facturas(["1705332268"])


def test_la_direccion_sale_de_direccion_empresa_y_prefiere_la_principal():
    """`empresa` no tiene dirección — está en `direccion_empresa.descripcion`.

    Verificado el 04/08 contra INFORMATION_SCHEMA de Asinfo.
    """
    sql = d.sql_empresas(["0992700726"])
    assert "direccion_empresa" in sql
    assert "de.descripcion" in sql
    assert "es_principal DESC" in sql


def test_la_busqueda_por_nombre_limpia_lo_que_se_tipea():
    """Lo que importa no es que la palabra DROP no aparezca — adentro de un
    literal es texto inofensivo — sino que **no pueda cerrar el literal**:
    ni comillas, ni `;`, ni `--`, ni comodines propios.
    """
    sql = d.sql_por_nombre("Pantoja'; DROP TABLE empresa-- %_")
    dentro = sql.split("LIKE '%")[1].split("%'")[0]
    assert dentro == "PANTOJA DROP TABLE EMPRESA"
    for veneno in ("'", ";", "--", "%", "_"):
        assert veneno not in dentro


def test_la_busqueda_por_nombre_conserva_las_tildes_y_la_enie():
    assert "CAÑAMAR" in d.sql_por_nombre("cañamar")


@pytest.mark.parametrize("corto", ["", "a", "ab", "  "])
def test_no_busca_con_menos_de_tres_letras(corto):
    """Un LIKE '%a%' contra el maestro entero no es una búsqueda."""
    assert d.sql_por_nombre(corto) == ""


# ---------------------------------------------------------------------------
# El cruce
# ---------------------------------------------------------------------------

def test_reparte_cada_factura_al_dueño_que_dice_asinfo():
    mapa = {
        101: {"ruc10": "1705332268", "numero": "001-099-000000101", "fecha": None, "total": 0},
        102: {"ruc10": "1801710425", "numero": "001-099-000000102", "fecha": None, "total": 0},
    }
    out = d.cruzar_facturas([RAMIRO, BLANCA],
                            [factura(101, 100.0, 10.0), factura(102, 250.0, 250.0)], mapa)
    por_id = {p["id_cliente"]: p for p in out["por_ficha"]}
    assert por_id[28929]["n"] == 1 and por_id[28929]["importe"] == 100.0
    assert por_id[29142]["n"] == 1 and por_id[29142]["importe"] == 250.0
    assert por_id[29142]["saldo"] == 250.0
    assert out["concluyente"] is True


def test_cruza_igual_cuando_pc_guardo_el_numero_sri_completo():
    """PC guarda `numf_completo` cuando la fila la trajo la carga de Asinfo."""
    mapa = {180441: {"ruc10": "1705332268", "numero": "001-099-000180441", "fecha": None, "total": 0}}
    out = d.cruzar_facturas([RAMIRO, BLANCA],
                            [factura(0, 50.0, completo="001-099-000180441")], mapa)
    assert out["filas"][0]["id_dueno"] == 28929


def test_una_factura_que_asinfo_no_tiene_queda_sin_dueño():
    out = d.cruzar_facturas([RAMIRO, BLANCA], [factura(999, 80.0, 80.0)], {})
    assert out["filas"][0]["estado"] == d.SIN_DUENO
    assert out["filas"][0]["id_dueno"] is None
    assert out["sin_dueno"] == {"n": 1, "importe": 80.0, "saldo": 80.0}


def test_no_declara_concluyente_un_reparto_con_facturas_sueltas():
    """⭐ El invariante que importa.

    La decisión de a quién renombrar se toma mirando los totales de arriba.
    Si quedan facturas sin repartir, esos totales son parciales y decir
    "listo" invita a decidir con la mitad de la plata a la vista.
    """
    mapa = {101: {"ruc10": "1705332268", "numero": "001-099-000000101", "fecha": None, "total": 0}}
    out = d.cruzar_facturas([RAMIRO, BLANCA], [factura(101, 100.0), factura(999, 80.0)], mapa)
    assert out["concluyente"] is False


def test_sin_facturas_no_hay_reparto_que_declarar_concluyente():
    out = d.cruzar_facturas([RAMIRO, BLANCA], [], {})
    assert out["concluyente"] is False
    assert all(p["n"] == 0 for p in out["por_ficha"])


def test_una_factura_de_un_ruc_ajeno_no_se_le_asigna_a_nadie():
    """Asinfo la tiene, pero de una empresa que no es ninguna de estas dos."""
    mapa = {101: {"ruc10": "9999999999", "numero": "001-099-000000101", "fecha": None, "total": 0}}
    out = d.cruzar_facturas([RAMIRO, BLANCA], [factura(101, 100.0)], mapa)
    assert out["filas"][0]["estado"] == d.SIN_DUENO


def test_todas_las_fichas_aparecen_aunque_no_les_toque_ninguna_factura():
    """Un cero explícito es información: dice que esa ficha nunca facturó."""
    mapa = {101: {"ruc10": "1705332268", "numero": "001-099-000000101", "fecha": None, "total": 0}}
    out = d.cruzar_facturas([RAMIRO, BLANCA], [factura(101, 100.0)], mapa)
    por_id = {p["id_cliente"]: p for p in out["por_ficha"]}
    assert por_id[29142]["n"] == 0


# ---------------------------------------------------------------------------
# Direcciones — el caso MTE
# ---------------------------------------------------------------------------

def test_marca_la_direccion_distinta_de_asinfo():
    assert d.direcciones_difieren({"direccion1": "CDLA. ALBORADA MZ 5"},
                                  {"direccion": "CDLA. URDENOR 2 MZ 100"})


@pytest.mark.parametrize("pc,asinfo", [
    ("CDLA. ALBORADA MZ 5", "CDLA ALBORADA MZ 5"),      # puntuación
    ("cdla. alborada mz 5", "CDLA. ALBORADA MZ 5"),     # mayúsculas
    ("CDLA. ALBORADA", "CDLA. ALBORADA MZ 5 SOLAR 2"),  # una contiene a la otra
])
def test_no_marca_la_misma_direccion_escrita_distinto(pc, asinfo):
    """Un rojo por un punto de más es ruido, y el ruido se deja de mirar."""
    assert not d.direcciones_difieren({"direccion1": pc}, {"direccion": asinfo})


@pytest.mark.parametrize("asinfo", [None, {}, {"direccion": ""}])
def test_sin_direccion_de_asinfo_no_se_afirma_nada(asinfo):
    assert not d.direcciones_difieren({"direccion1": "AV. QUITO 100"}, asinfo)


# ---------------------------------------------------------------------------
# La pantalla
# ---------------------------------------------------------------------------


def _app_logueada():
    """`(app, deshacer)` con un usuario falso que tiene todos los permisos.

    El `before_request` propio va DESPUÉS de `create_app()` a propósito:
    `app.py` hace `from auth import load_logged_in_user` en el import, así que
    pisar `auth.load_logged_in_user` sólo funciona si `app.py` todavía no se
    importó. Corriendo el archivo solo pasa; en la suite completa `app.py` ya
    está importado y la ruta contesta 302 al login. Es un gotcha conocido de
    la casa (ver skill `programa-core`).
    """
    from flask import g

    from tests.test_routes_smoke import ALL_PERMS, _make_fake_user, build_app

    app, deshacer = build_app()

    @app.before_request
    def _login_falso():  # pragma: no cover - infraestructura de test
        g.user = _make_fake_user()
        g.permisos = set(ALL_PERMS)

    return app, deshacer


def test_la_pantalla_no_cae_sin_metabase(monkeypatch):
    """Fail-soft: sin el puente igual se ve el lado PC. Nunca 500."""
    monkeypatch.setattr(d, "fichas_del_codigo", lambda cod: [dict(RAMIRO), dict(BLANCA)])
    monkeypatch.setattr(d, "facturas_del_codigo", lambda cod: [factura(101, 100.0)])
    monkeypatch.setattr(d, "_consultar", lambda sql, max_results: ([], False))

    app, deshacer = _app_logueada()
    try:
        r = app.test_client().get("/admin/clientes-asinfo/BLP")
        assert r.status_code == 200
        cuerpo = r.get_data(as_text=True)
        assert "RAMIRO AYO" in cuerpo
        assert "no está disponible" in cuerpo
    finally:
        deshacer()


def test_el_codigo_de_la_url_se_limpia_antes_de_consultar(monkeypatch):
    """La URL es texto libre; lo que llega a la query no."""
    vistos = []
    monkeypatch.setattr(d, "fichas_del_codigo", lambda cod: vistos.append(cod) or [])
    monkeypatch.setattr(d, "facturas_del_codigo", lambda cod: [])
    monkeypatch.setattr(d, "_consultar", lambda sql, max_results: ([], False))

    app, deshacer = _app_logueada()
    try:
        assert app.test_client().get("/admin/clientes-asinfo/b l p'--").status_code == 200
    finally:
        deshacer()
    assert vistos == ["BLP"]
