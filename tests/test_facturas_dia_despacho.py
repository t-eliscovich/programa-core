"""El cuadre del día: lo despachado contra lo facturado, y por qué difieren.

TMT 2026-08-18 (dueña), mirando el pin de la campanita: *"¿por qué hay más
facturado que despachado?"* (19.552,51 contra 19.469,06 kg). Y después:
*"¿deberíamos dejarlo accesible para el usuario en algún lugar? no quiero más
tabs al costado"* → `/facturas/dia`, sin entrada en el menú: se llega
clickeando el renglón **Despachado**, en la campanita y en el recuadro del
inicio.

Lo que estos tests protegen:

· **La cuenta CIERRA.** Despachado + lo facturado sin guía de hoy − lo
  despachado sin facturar = Facturado. Sin ese invariante son dos números y
  una hipótesis.
· **Los kilos los pone PC, no Asinfo.** El detalle de Asinfo trae renglones
  que no son kilos (unidades sueltas que suman 1,00): sumarlos daba 21.791,41
  contra los 19.552,51 del pin. A Asinfo se le pregunta sólo de qué guía viene
  cada kilo.
· **Una NOTA DE ENTREGA no es una factura sin guía**: no existe en
  `factura_cliente`, así que buscarla ahí la contaría entera como faltante.
· **La fecha se valida antes de entrar al SQL**: va como literal (el
  `GETDATE()` de Asinfo está en UTC y correría el día cinco horas).
· **Asinfo nunca tumba la pantalla.**
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from modules.facturas import dia_despacho as dd

DIA = date(2026, 8, 18)

#: Las guías del día. Las tres facturadas, como el 18/08 de verdad.
_GUIAS = [
    {"guia": "DES-95512", "hora": "08:14", "cliente": "TJC",
     "facturada": True, "kg": 612.35},
    {"guia": "DES-95513", "hora": "08:31", "cliente": "AJO",
     "facturada": True, "kg": 318.90},
]

#: Los documentos que PC tiene cargados ese día.
_DOCS = [
    {"numf": 181963, "numf_completo": "001-099-000181963", "codigo_cli": "POS",
     "kg": 83.45, "importe": 612.40},
    {"numf": 182010, "numf_completo": "001-099-000182010", "codigo_cli": "TJC",
     "kg": 612.35, "importe": 4000.00},
    {"numf": 10869, "numf_completo": "NTEN-10869", "codigo_cli": "AJO",
     "kg": 318.90, "importe": 0.0},
]


def _pc(docs):
    """Los documentos como los devuelve `_documentos_pc` (kg de NUESTRA base)."""
    return [{"numf": d["numf"], "doc": d["numf_completo"],
             "cliente": d["codigo_cli"], "kg": d["kg"], "importe": d["importe"]}
            for d in docs]


def _login(app, fake_db, perms=("facturas.ver",)):
    rid = fake_db.add_role("Tester", list(perms))
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def _cuadre(guias=None, ligado=None, docs=None):
    """`cuadre()` con las dos puntas falseadas: PC por un lado, Asinfo por otro."""
    with patch.object(dd, "_documentos_pc",
                      return_value=_pc(docs if docs is not None else _DOCS)), \
         patch.object(dd, "_guias",
                      return_value=guias if guias is not None else _GUIAS), \
         patch.object(dd, "_kg_con_guia_de_hoy",
                      return_value=ligado if ligado is not None else
                      {"001-099-000182010": 612.35}):
        return dd.cuadre(DIA)


# ── El invariante ───────────────────────────────────────────────────────────

def test_la_cuenta_cierra_y_el_residuo_da_cero():
    d = _cuadre()
    assert d["despachado"]["kg"] == 931.25
    assert d["facturado"]["kg"] == 1014.70
    assert d["diferencia"] == 83.45
    # despachado + sin_guia − sin_factura = facturado
    assert round(d["despachado"]["kg"] + d["sin_guia"]["kg"]
                 - d["sin_factura"]["kg"], 2) == d["facturado"]["kg"]
    assert d["residuo"] == 0.0


def test_el_faltante_es_la_factura_sin_guia_con_su_cliente():
    """El 18/08 fueron 83,45 kg de UNA factura, la de POS."""
    d = _cuadre()
    assert d["sin_guia"]["kg"] == 83.45
    (f,) = d["sin_guia"]["items"]
    assert f["doc"] == "001-099-000181963"
    assert f["cliente"] == "POS"
    assert f["kg_sin_guia"] == 83.45


def test_una_nota_de_entrega_no_cuenta_como_facturado_sin_guia():
    """La NTEN es la guía misma documentada: no vive en `factura_cliente`, así
    que preguntar por ella ahí la daría entera como faltante (318,90 kg de
    diferencia inventada)."""
    d = _cuadre()
    assert all(not f["doc"].startswith("NTEN") for f in d["sin_guia"]["items"])


def test_lo_despachado_sin_facturar_sale_con_su_guia_y_su_hora():
    guias = [*_GUIAS, {"guia": "DES-95999", "hora": "17:51",
                       "cliente": "HOM", "facturada": False, "kg": 204.15}]
    d = _cuadre(guias=guias)
    (g,) = d["sin_factura"]["items"]
    assert (g["guia"], g["hora"], g["kg"]) == ("DES-95999", "17:51", 204.15)
    assert d["sin_factura"]["kg"] == 204.15


def test_un_resto_de_redondeo_no_arma_un_renglon():
    """Los renglones de unidades sueltas de Asinfo dejan diferencias de
    centésimas: eso es ruido, no un caso."""
    d = _cuadre(ligado={"001-099-000182010": 612.34,
                        "001-099-000181963": 83.45})
    assert d["sin_guia"]["items"] == []


# ── La fecha, que entra al SQL como literal ─────────────────────────────────

@pytest.mark.parametrize("mala", ["2026-13", "hoy", "2026-08-18'; DROP", ""])
def test_una_fecha_que_no_es_una_fecha_no_llega_al_sql(mala):
    with pytest.raises(ValueError):
        dd._dia(mala)


def test_la_fecha_valida_pasa_como_iso():
    assert dd._dia(DIA) == "2026-08-18"
    assert dd._dia("2026-08-18") == "2026-08-18"


# ── Asinfo nunca tumba la pantalla ──────────────────────────────────────────

def test_si_asinfo_no_contesta_queda_el_lado_nuestro():
    with patch.object(dd, "_documentos_pc", return_value=[
            {"numf": 1, "doc": "001-099-000181963", "cliente": "POS",
             "kg": 83.45, "importe": 612.40}]), \
         patch.object(dd, "_guias", side_effect=RuntimeError("Metabase caído")):
        d = dd.cuadre(DIA)
    assert d["asinfo_ok"] is False
    assert d["facturado"]["kg"] == 83.45      # lo nuestro se muestra igual
    assert d["despachado"]["kg"] is None      # un cero se leería como "no salió nada"
    assert d["diferencia"] is None and d["residuo"] is None
    assert d["sin_guia"]["items"] == [] and d["sin_factura"]["items"] == []


# ── Las queries que le vamos a mandar a Asinfo ──────────────────────────────

def test_la_query_de_guias_es_el_mismo_universo_que_el_pin():
    """Si el WHERE se despegara de `despacho_fisico_dia_info`, el total de esta
    pantalla dejaría de ser EL número de la campanita."""
    from modules._lib import metabase_client
    with patch.object(metabase_client, "fetch_dataset", return_value=[]) as m:
        dd._guias("2026-08-18")
    sql = m.call_args[0][1]
    assert "'2026-08-18'" in sql
    assert "fecha_anulacion IS NULL" in sql
    assert "id_bodega = 53" in sql


def test_la_query_de_facturas_deja_afuera_las_anuladas():
    """`fc.estado = 0` es una emisión no autorizada por el SRI que se re-emitió
    con otro número: contarla duplica kilos."""
    from modules._lib import metabase_client
    with patch.object(metabase_client, "fetch_dataset", return_value=[]) as m:
        dd._kg_con_guia_de_hoy("2026-08-18")
    sql = m.call_args[0][1]
    assert "fc.estado <> 0" in sql
    # el vínculo bueno es por RENGLÓN, no por cabecera
    assert "id_detalle_despacho_cliente" in sql


# ── La puerta: no hay tab, se entra por el número ───────────────────────────

def test_se_llega_desde_la_campanita_y_desde_el_recuadro_del_inicio():
    """*"no quiero más tabs al costado"*: la pantalla no está en el menú, así
    que si estos dos links se caen, deja de existir para quien la usa."""
    base = Path("templates/base.html").read_text()
    inicio = Path("modules/historial/templates/historial/operaciones.html").read_text()
    assert "/facturas/dia?fecha=" in base
    assert "/facturas/dia?fecha=" in inicio
    # …y el link del Despachado no puede quedar ADENTRO de otro <a>: el
    # navegador parte el bloque en pedazos y el pin deja de ser clickeable.
    ini = base.index('class="campanita-pin"')
    pin = base[ini:base.index("campanita-lista", ini)]
    assert pin.count("<a ") == 2 and pin.count("</a>") == 2


def test_la_pantalla_abre_y_muestra_el_cuadre(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(dd, "_documentos_pc", return_value=[
            {"numf": 181963, "doc": "001-099-000181963", "cliente": "POS",
             "kg": 83.45, "importe": 612.40}]), \
         patch.object(dd, "_guias", return_value=_GUIAS), \
         patch.object(dd, "_kg_con_guia_de_hoy", return_value={}):
        r = c.get("/facturas/dia?fecha=2026-08-18")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "001-099-000181963" in body and ">POS<" in body
    assert "Facturado hoy sin guía de despacho de hoy" in body


def test_una_fecha_basura_en_la_url_cae_en_hoy_y_no_revienta(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(dd, "_documentos_pc", return_value=[]), \
         patch.object(dd, "_guias", return_value=[]), \
         patch.object(dd, "_kg_con_guia_de_hoy", return_value={}):
        r = c.get("/facturas/dia?fecha=cualquier-cosa")
    assert r.status_code == 200


def test_sin_permiso_de_facturas_la_pantalla_no_existe(app, fake_db):
    """Mismo criterio que el resto: sin el permiso, 404 (no "no tenés acceso")."""
    c = _login(app, fake_db, perms=["compras.ver"])
    assert c.get("/facturas/dia").status_code == 404
