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


def _cuadre(guias=None, ligado=None, docs=None, por_guia=None):
    """`cuadre()` con las dos puntas falseadas: PC por un lado, Asinfo por otro."""
    with patch.object(dd, "_documentos_pc",
                      return_value=_pc(docs if docs is not None else _DOCS)), \
         patch.object(dd, "_guias",
                      return_value=guias if guias is not None else _GUIAS), \
         patch.object(dd, "_kg_con_guia_de_hoy",
                      return_value=ligado if ligado is not None else
                      {"001-099-000182010": 612.35}), \
         patch.object(dd, "_doc_por_guia",
                      return_value=por_guia if por_guia is not None else
                      {"DES-95512": {"001-099-000182010": 612.35}}):
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


# ── El agujero del 19/08: facturada en Asinfo, sin cargar acá ───────────────

def test_la_guia_facturada_en_asinfo_que_falta_importar_no_es_residuo():
    """El caso real del 19/08 08:28: salieron 272,25 kg en dos guías y PC
    todavía no tenía NINGÚN documento. Una de las dos (12,90 kg de AJO) ya
    estaba facturada en Asinfo con la 182106. Mirando sólo
    `indicador_generado_factura`, esos 12,90 no caían en ningún balde y salían
    por la fila de "sin identificar" — que es justo lo que esa fila NO tiene
    que mostrar cuando la respuesta se sabe."""
    guias = [
        {"guia": "DES-95926", "hora": "07:54", "cliente": "SEF",
         "facturada": False, "kg": 259.35},
        {"guia": "DES-95927", "hora": "08:28", "cliente": "AJO",
         "facturada": True, "kg": 12.90},
    ]
    d = _cuadre(guias=guias, docs=[], ligado={},
                por_guia={"DES-95927": {"001-099-000182106": 12.90}})
    assert d["despachado"]["kg"] == 272.25
    assert d["facturado"]["kg"] == 0.0
    assert d["sin_factura"]["kg"] == 259.35          # nadie la facturó todavía
    assert d["sin_cargar"]["kg"] == 12.90            # existe en Asinfo, falta acá
    (g,) = d["sin_cargar"]["items"]
    assert g["doc"] == "001-099-000182106" and g["cliente"] == "AJO"
    assert d["residuo"] == 0.0                       # ya no queda nada sin nombre


def test_la_guia_cuyo_documento_YA_esta_en_pc_no_resta():
    """Si el documento está cargado, la guía no le baja nada al cuadre."""
    d = _cuadre()
    assert all(g["guia"] != "DES-95512" for g in d["sin_cargar"]["items"])


def test_las_notas_de_entrega_que_faltan_van_en_un_solo_renglon():
    """Una NTEN no tiene número con el que casarla contra su guía, así que se
    compara el TOTAL: lo que falta se dice agrupado, no se esconde."""
    guias = [{"guia": "DES-95513", "hora": "08:31", "cliente": "AJO",
              "facturada": True, "kg": 318.90}]
    d = _cuadre(guias=guias, docs=[], ligado={}, por_guia={})
    assert d["sin_cargar"]["kg_nten"] == 318.90
    assert d["sin_cargar"]["items"] == []
    assert d["residuo"] == 0.0


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
         patch.object(dd, "_guias", side_effect=RuntimeError("Metabase caído")), \
         patch.object(dd, "_doc_por_guia", return_value={}):
        d = dd.cuadre(DIA)
    assert d["asinfo_ok"] is False
    assert d["facturado"]["kg"] == 83.45      # lo nuestro se muestra igual
    assert d["despachado"]["kg"] is None      # un cero se leería como "no salió nada"
    assert d["diferencia"] is None and d["residuo"] is None
    assert d["sin_guia"]["items"] == [] and d["sin_factura"]["items"] == []
    assert d["sin_cargar"]["items"] == [] and d["sin_cargar"]["kg"] == 0.0


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
         patch.object(dd, "_kg_con_guia_de_hoy", return_value={}), \
         patch.object(dd, "_doc_por_guia", return_value={}):
        r = c.get("/facturas/dia?fecha=2026-08-18")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "001-099-000181963" in body and "POS" in body
    # la cuenta NO cierra en este caso, así que el porqué tiene que estar
    assert "Por qué no coinciden" in body


def test_una_fecha_basura_en_la_url_cae_en_hoy_y_no_revienta(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(dd, "_documentos_pc", return_value=[]), \
         patch.object(dd, "_guias", return_value=[]), \
         patch.object(dd, "_kg_con_guia_de_hoy", return_value={}), \
         patch.object(dd, "_doc_por_guia", return_value={}):
        r = c.get("/facturas/dia?fecha=cualquier-cosa")
    assert r.status_code == 200


def test_sin_permiso_de_facturas_la_pantalla_no_existe(app, fake_db):
    """Mismo criterio que el resto: sin el permiso, 404 (no "no tenés acceso")."""
    c = _login(app, fake_db, perms=["compras.ver"])
    assert c.get("/facturas/dia").status_code == 404


def test_la_query_del_documento_por_guia_ata_por_renglon_y_saltea_anuladas():
    from modules._lib import metabase_client
    with patch.object(metabase_client, "fetch_dataset", return_value=[]) as m:
        dd._doc_por_guia("2026-08-19")
    sql = m.call_args[0][1]
    assert "'2026-08-19'" in sql
    assert "id_detalle_despacho_cliente" in sql   # por RENGLÓN, no por cabecera
    assert "fc.estado <> 0" in sql
    assert "dc.fecha_anulacion IS NULL" in sql


def test_cada_guia_lleva_el_numero_de_su_factura():
    """TMT 19/08: *"poneme cada factura de cada despacho así lo pueden usar"*.
    La columna decía "facturada"/"sin factura" —el estado, no el dato—, así que
    la tabla se miraba pero no se podía trabajar con ella."""
    guias = [
        {"guia": "DES-95512", "hora": "08:14", "cliente": "TJC",
         "facturada": True, "kg": 612.35},
        {"guia": "DES-95927", "hora": "08:28", "cliente": "AJO",
         "facturada": True, "kg": 12.90},
        {"guia": "DES-95926", "hora": "07:54", "cliente": "SEF",
         "facturada": False, "kg": 259.35},
    ]
    d = _cuadre(guias=guias,
                por_guia={"DES-95512": {"001-099-000182010": 612.35},
                          "DES-95927": {"001-099-000182106": 12.90}})
    por = {g["guia"]: g for g in d["guias"]}
    # la que está cargada acá: número y marcada como presente
    assert por["DES-95512"]["docs"] == ["001-099-000182010"]
    assert por["DES-95512"]["en_pc"] == ["001-099-000182010"]
    # la que existe en Asinfo pero todavía no se importó: número igual, sin en_pc
    assert por["DES-95927"]["docs"] == ["001-099-000182106"]
    assert por["DES-95927"]["en_pc"] == []
    # la que nadie facturó todavía: sin número que mostrar
    assert por["DES-95926"]["docs"] == []


def test_la_tabla_del_dia_imprime_el_numero_y_no_la_palabra_facturada(app, fake_db):
    c = _login(app, fake_db)
    guias = [{"guia": "DES-95927", "hora": "08:28", "cliente": "AJO",
              "facturada": True, "kg": 12.90}]
    with patch.object(dd, "_documentos_pc", return_value=[]), \
         patch.object(dd, "_guias", return_value=guias), \
         patch.object(dd, "_kg_con_guia_de_hoy", return_value={}), \
         patch.object(dd, "_doc_por_guia",
                      return_value={"DES-95927": {"001-099-000182106": 12.90}}):
        body = c.get("/facturas/dia?fecha=2026-08-19").get_data(as_text=True)
    assert "001-099-000182106" in body
    assert "falta cargarla" in body


def test_cuando_la_cuenta_cierra_la_pantalla_se_calla(app, fake_db):
    """TMT 19/08: *"todo esto mucho más chico y sacar datos no importantes"*.
    Tres secciones diciendo "Nada: …" son media pantalla para informar que no
    pasó nada. Si despachado y facturado coinciden, sólo va la tabla."""
    c = _login(app, fake_db)
    guias = [{"guia": "DES-95512", "hora": "08:14", "cliente": "TJC",
              "facturada": True, "kg": 612.35}]
    docs = [{"numf": 182010, "numf_completo": "001-099-000182010",
             "codigo_cli": "TJC", "kg": 612.35, "importe": 4000.0}]
    with patch.object(dd, "_documentos_pc", return_value=_pc(docs)), \
         patch.object(dd, "_guias", return_value=guias), \
         patch.object(dd, "_kg_con_guia_de_hoy",
                      return_value={"001-099-000182010": 612.35}), \
         patch.object(dd, "_doc_por_guia",
                      return_value={"DES-95512": {"001-099-000182010": 612.35}}):
        body = c.get("/facturas/dia?fecha=2026-08-18").get_data(as_text=True)
    # El `<summary>` de la explicación, no el texto suelto: desde el 19/08 la
    # campanita (que está en TODAS las pantallas) usa la misma frase como
    # tooltip del renglón Despachado.
    assert "cursor-pointer\">Por qué no coinciden" not in body
    assert "Nada:" not in body
    # …pero la guía, que es lo que se viene a ver, sigue estando
    assert "DES-95512" in body and "001-099-000182010" in body


def test_la_guia_va_arriba_y_no_adentro_de_un_desplegable(app, fake_db):
    """La tabla de guías es la pantalla: si vuelve a quedar detrás de un
    `<details>`, hay que abrirla para ver lo único que se viene a ver."""
    c = _login(app, fake_db)
    guias = [{"guia": "DES-95512", "hora": "08:14", "cliente": "TJC",
              "facturada": False, "kg": 612.35}]
    with patch.object(dd, "_documentos_pc", return_value=[]), \
         patch.object(dd, "_guias", return_value=guias), \
         patch.object(dd, "_kg_con_guia_de_hoy", return_value={}), \
         patch.object(dd, "_doc_por_guia", return_value={}):
        body = c.get("/facturas/dia?fecha=2026-08-18").get_data(as_text=True)
    # sólo el contenido de ESTA pantalla: base.html tiene sus propios <details>
    cuerpo = body[body.index("Guías del día"):]
    i_tabla = cuerpo.index("DES-95512")
    i_details = cuerpo.find("<details")
    assert i_details == -1 or i_tabla < i_details


def test_la_tabla_compara_kg_de_despacho_contra_kg_de_factura():
    """TMT 19/08: *"poné kg de despacho y kg de factura"*. Una guía puede
    facturarse por un peso distinto del que salió; con una sola columna de
    kilos eso no se veía."""
    guias = [
        {"guia": "DES-A", "hora": "08:14", "cliente": "TJC",
         "facturada": True, "kg": 612.35},
        {"guia": "DES-B", "hora": "09:01", "cliente": "AJO",
         "facturada": True, "kg": 100.00},
        {"guia": "DES-C", "hora": "10:00", "cliente": "SEF",
         "facturada": False, "kg": 50.00},
    ]
    d = _cuadre(guias=guias, docs=[], ligado={},
                por_guia={"DES-A": {"001-099-000182010": 612.35},
                          "DES-B": {"001-099-000182011": 97.50}})
    por = {g["guia"]: g for g in d["guias"]}
    assert por["DES-A"]["kg_fact"] == 612.35 and por["DES-A"]["difiere"] is False
    # facturó 2,50 kg menos de lo que salió: eso tiene que saltar
    assert por["DES-B"]["kg_fact"] == 97.50 and por["DES-B"]["difiere"] is True
    # sin factura todavía: no hay con qué comparar, y no se inventa un cero
    assert por["DES-C"]["kg_fact"] is None and por["DES-C"]["difiere"] is False


def test_la_fila_con_los_kilos_distintos_sale_en_rojo(app, fake_db):
    """TMT 19/08: *"si hay alguna diferencia entre kg despachados y facturados,
    que se ponga en rojo"*. Y va la fila entera: un solo color por fila."""
    c = _login(app, fake_db)
    guias = [{"guia": "DES-B", "hora": "09:01", "cliente": "AJO",
              "facturada": True, "kg": 100.00}]
    with patch.object(dd, "_documentos_pc", return_value=[]), \
         patch.object(dd, "_guias", return_value=guias), \
         patch.object(dd, "_kg_con_guia_de_hoy", return_value={}), \
         patch.object(dd, "_doc_por_guia",
                      return_value={"DES-B": {"001-099-000182011": 97.50}}):
        body = c.get("/facturas/dia?fecha=2026-08-19").get_data(as_text=True)
    fila = body[body.index("DES-B") - 400:body.index("97,50") + 120]
    assert "bg-red-50" in fila and "text-red-700" in fila
    assert "-2,50" in fila          # cuánto es la diferencia, no sólo que la hay


def test_la_fila_que_coincide_no_se_pinta(app, fake_db):
    """El color tiene que significar algo: si se pintara siempre, no diría nada."""
    c = _login(app, fake_db)
    guias = [{"guia": "DES-A", "hora": "08:14", "cliente": "TJC",
              "facturada": True, "kg": 612.35}]
    with patch.object(dd, "_documentos_pc", return_value=[]), \
         patch.object(dd, "_guias", return_value=guias), \
         patch.object(dd, "_kg_con_guia_de_hoy", return_value={}), \
         patch.object(dd, "_doc_por_guia",
                      return_value={"DES-A": {"001-099-000182010": 612.35}}):
        body = c.get("/facturas/dia?fecha=2026-08-19").get_data(as_text=True)
    cuerpo = body[body.index("Guías del día"):]
    assert "bg-red-50" not in cuerpo
