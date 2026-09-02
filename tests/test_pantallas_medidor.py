"""El termómetro de las pantallas — `modules/_lib/medidor.py` y /admin/pantallas.

TMT 2026-08-26 (dueña): *"cómo se podría evaluar las pantallas del programa y
hacerlas más rápido"*.

Nació de un error propio, y por eso el módulo existe: pasé una sesión midiendo
en una base local sembrada a ojo y le dije a la dueña que comisiones estaba
lento cuando ya se había arreglado en agosto. El programa ya medía cada request
y cada consulta, pero lo escribía en un log del servidor Windows que no lee
nadie.

Lo que protegen estos tests, en orden de qué tan caro sería que se rompa:

  1. Que MEDIR NUNCA rompa nada. Un contador que puede tirar una excepción
     adentro de `db._t` se lleva puesta una consulta de cobranza.
  2. Que agrupe por la REGLA (`/facturas/<numf>`) y no por la URL: si agrupara
     por URL, cada factura sería una pantalla distinta y la tabla no sumaría.
  3. Que no crezca sin techo ni guarde datos de nadie.
"""
from __future__ import annotations

import inspect
import time

import pytest

from modules._lib import medidor


@pytest.fixture(autouse=True)
def _limpio():
    medidor.limpiar()
    yield
    medidor.limpiar()


def _visita(ruta, ms, consultas=(), metodo="GET", codigo=200):
    medidor.arrancar()
    for q_ms, sql in consultas:
        medidor.anotar_consulta(q_ms, sql)
    medidor.cerrar(ruta, metodo, ms, codigo)


# ---------------------------------------------------------------------------
# 1. Medir no puede romper nada
# ---------------------------------------------------------------------------


def test_una_consulta_de_un_hilo_de_fondo_no_se_cuenta():
    """`db._t` también corre desde el calentador y la auto-carga de facturas,
    donde no hay request. Ahí esto no cuenta nada — y sobre todo, no explota."""
    medidor.anotar_consulta(50, "SELECT 1")     # sin arrancar()
    medidor.cerrar("/x", "GET", 10)             # cerrar sin arrancar tampoco
    assert medidor.resumen() == []


def test_el_observador_de_db_no_puede_tirar_una_consulta(monkeypatch):
    """⭐ El más importante. `db._t` llama al medidor en CADA consulta: si el
    medidor tira, se lleva puesta la consulta de un usuario. Por eso la llamada
    va adentro de un try — y este test es el que lo sostiene."""
    import db

    def _explota(ms, sql):
        raise RuntimeError("me rompí")

    monkeypatch.setattr(db, "OBSERVADOR", _explota)
    db._t("SELECT 1", None, 0.0)                # no tira: eso es todo el test


def test_db_le_avisa_al_medidor_de_cada_consulta(monkeypatch):
    """Y que el cable esté enchufado: si nadie llama al observador, la pantalla
    muestra 0 consultas para siempre y nadie se entera."""
    import db

    vistas = []
    monkeypatch.setattr(db, "OBSERVADOR", lambda ms, sql: vistas.append(sql))
    db._t("SELECT 42", None, 0.0)
    assert vistas == ["SELECT 42"]


def test_sin_observador_db_sigue_andando(monkeypatch):
    import db

    monkeypatch.setattr(db, "OBSERVADOR", None)
    db._t("SELECT 1", None, 0.0)


# ---------------------------------------------------------------------------
# 2. Lo que mide
# ---------------------------------------------------------------------------


def test_cuenta_visitas_consultas_y_tiempo():
    _visita("/cheques", 100, [(10, "SELECT a"), (20, "SELECT b")])
    _visita("/cheques", 300, [(5, "SELECT a")])

    f = medidor.resumen()[0]
    assert f["ruta"] == "/cheques"
    assert f["visitas"] == 2
    assert f["mediana_ms"] == 200          # mediana de 100 y 300
    assert f["ms_max"] == 300
    assert f["consultas_prom"] == 1.5
    assert f["consultas_max"] == 2


def test_ordena_por_lo_que_SE_LLEVA_y_no_por_la_mas_lenta():
    """⭐ La decisión de la pantalla. Una de 3 s que se abre una vez molesta
    menos que una de 400 ms que se abre 200 veces, y la segunda es la que
    conviene arreglar."""
    _visita("/rara-vez", 3000)
    for _ in range(20):
        _visita("/todo-el-dia", 400)

    filas = medidor.resumen()
    assert filas[0]["ruta"] == "/todo-el-dia"     # 20 × 400 ms = 8 s
    assert filas[1]["ruta"] == "/rara-vez"        # 3 s
    assert filas[1]["ms_max"] == 3000             # y su pico se sigue viendo


def test_se_queda_con_la_consulta_mas_lenta_de_la_pantalla():
    """Los milisegundos dicen QUE duele; la peor consulta, DÓNDE."""
    _visita("/x", 500, [(5, "SELECT rapida"), (400, "SELECT lenta")])
    _visita("/x", 100, [(9, "SELECT otra")])

    f = medidor.resumen()[0]
    assert f["peor_sql"] == "SELECT lenta"
    assert f["peor_sql_ms"] == 400


def test_el_sql_se_guarda_en_una_linea_y_cortado():
    """Entra en una pantalla y no se lleva un `WITH` de 200 líneas a memoria."""
    _visita("/x", 10, [(50, "SELECT\n   uno,\n   dos" + " x" * 400)])
    guardado = medidor.resumen()[0]["peor_sql"]
    assert "\n" not in guardado
    assert len(guardado) <= 300


def test_las_visitas_lentas_se_guardan_una_por_una(monkeypatch):
    """La misma pantalla puede andar bien todo el día y tardar con UN cliente
    grande. El promedio se lo come; la lista de lentas, no."""
    monkeypatch.setattr(medidor, "LENTA_MS", 500.0)
    _visita("/facturas/<numf>", 120)
    _visita("/facturas/<numf>", 900, [(700, "SELECT gorda")])

    lentas = medidor.lentas()
    assert len(lentas) == 1
    assert lentas[0]["ms"] == 900
    assert lentas[0]["peor_sql"] == "SELECT gorda"


def test_de_las_lentas_se_recuerdan_las_ULTIMAS(monkeypatch):
    monkeypatch.setattr(medidor, "MAX_LENTAS", 3)
    for i in range(6):
        _visita(f"/p{i}", 1000)
    assert [v["ruta"] for v in medidor.lentas()] == ["/p5", "/p4", "/p3"]


# ---------------------------------------------------------------------------
# 3. Que no crezca sin techo
# ---------------------------------------------------------------------------


def test_hay_un_techo_de_pantallas(monkeypatch):
    """Un bicho que genere rutas infinitas no se puede comer la memoria del
    servidor que atiende la app."""
    monkeypatch.setattr(medidor, "MAX_RUTAS", 3)
    for i in range(10):
        _visita(f"/p{i}", 10)
    assert len(medidor.resumen()) == 3


def test_solo_se_guardan_las_ultimas_muestras_de_cada_pantalla(monkeypatch):
    monkeypatch.setattr(medidor, "MUESTRAS_POR_RUTA", 5)
    for ms in range(1, 21):
        _visita("/x", ms * 10)
    f = medidor.resumen()[0]
    assert f["visitas"] == 20                 # las visitas se cuentan todas
    assert f["mediana_ms"] == 180             # la mediana, de las últimas 5

def test_empezar_de_cero_deja_todo_en_cero():
    _visita("/x", 900)
    medidor.limpiar()
    assert medidor.resumen() == [] and medidor.lentas() == []
    assert medidor.estado()["visitas"] == 0


# ---------------------------------------------------------------------------
# 4. La pantalla
# ---------------------------------------------------------------------------


def _login(app, fake_db, perms=("admin_dbase.ver",)):
    rid = fake_db.add_role("Tester", list(perms))
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def test_la_pantalla_muestra_lo_medido(app, fake_db):
    _visita("/informes/estado-cuenta/<codigo_cli>", 250,
            [(200, "SELECT * FROM scintela.factura")])
    html = _login(app, fake_db).get("/admin/pantallas").get_data(as_text=True)

    assert "/informes/estado-cuenta/&lt;codigo_cli&gt;" in html
    assert "250 ms" in html
    assert "scintela.factura" in html


def test_la_pantalla_sin_nada_medido_lo_explica(app, fake_db):
    html = _login(app, fake_db).get("/admin/pantallas").get_data(as_text=True)
    assert "Todavía no hay nada medido" in html


def test_la_pantalla_es_de_admin(app, fake_db):
    """Muestra el SQL del programa: no es para cualquiera.

    Sin el permiso contesta 404 y no 403 — decisión de la dueña del 22/05: que
    quien no puede entrar no se entere de que la sección existe."""
    r = _login(app, fake_db, perms=("informes.ver",)).get("/admin/pantallas")
    assert r.status_code == 404, r.status_code


def test_el_boton_de_empezar_de_cero_borra(app, fake_db):
    _visita("/x", 900)
    c = _login(app, fake_db)
    r = c.post("/admin/pantallas/reiniciar")
    assert r.status_code in (302, 303)
    # Lo medido antes se fue. (El propio POST sí queda: se mide como cualquier
    # otro request, después de que la vista limpió.)
    assert "/x" not in [f["ruta"] for f in medidor.resumen()]


def test_la_app_mide_por_la_REGLA_y_no_por_la_URL(app, fake_db):
    """⭐ Si agrupara por URL, cada factura sería una pantalla distinta y la
    tabla no podría sumar nada. Se mide `/informes/estado-cuenta/<codigo_cli>`,
    no `/informes/estado-cuenta/ATE`."""
    c = _login(app, fake_db, perms=("informes.ver", "admin_dbase.ver"))
    medidor.limpiar()
    c.get("/informes/estado-cuenta/ATE")
    c.get("/informes/estado-cuenta/BED")

    rutas = [f["ruta"] for f in medidor.resumen()]
    assert "/informes/estado-cuenta/<codigo_cli>" in rutas, rutas
    assert "/informes/estado-cuenta/ATE" not in rutas
    fila = next(f for f in medidor.resumen()
                if f["ruta"] == "/informes/estado-cuenta/<codigo_cli>")
    assert fila["visitas"] == 2


def test_una_url_que_no_existe_no_es_una_pantalla(app, fake_db):
    """Un 404 no tiene regla: guardarlo sería llenar la tabla con la basura que
    tire cualquier bot."""
    c = _login(app, fake_db)
    medidor.limpiar()
    c.get("/esto-no-existe-en-el-programa")
    assert medidor.resumen() == []


# ---------------------------------------------------------------------------
# Que "volver a empezar" empiece de verdad de cero (26/08/2026).
#
# El CI se puso rojo dos veces seguidas con
# `test_una_consulta_de_un_hilo_de_fondo_no_se_cuenta`, que pasaba local: un
# test anterior dejaba `arrancar()` sin su `cerrar()` —pasa cuando la respuesta
# no sale por el after_request— y `limpiar()` no apagaba el thread-local. El
# `cerrar()` del test siguiente se anotaba como una visita de verdad.
# ---------------------------------------------------------------------------

def test_limpiar_apaga_la_medicion_a_medio_hacer():
    """Si quedó un `arrancar()` abierto, `limpiar()` lo cierra: el próximo
    `cerrar()` no puede anotarse solo."""
    medidor.arrancar()                       # request que nunca cerró
    medidor.limpiar()
    medidor.cerrar("/x", "GET", 10)          # el cerrar del hilo de fondo
    assert medidor.resumen() == []


def test_limpiar_tambien_borra_las_consultas_contadas():
    medidor.arrancar()
    medidor.anotar_consulta(50, "SELECT 1")
    medidor.limpiar()
    medidor.arrancar()
    medidor.cerrar("/y", "GET", 10)
    fila = [f for f in medidor.resumen() if f["ruta"] == "/y"][0]
    assert fila["consultas"] == 0, "arrastró las consultas de antes de limpiar"


# ---------------------------------------------------------------------------
# 5. El puente y el calentador (TMT 2026-09-02: "¿páginas lentas?")
# ---------------------------------------------------------------------------
# Medidas en vivo, las pantallas lentas tenían casi nada de base propia:
# /produccion-terminado-asinfo tardó 10,3 s con 7 ms de consultas. El resto era
# el puente (Metabase / formulas) y la pantalla no lo mostraba.


def test_el_puente_se_cuenta_aparte_de_la_base():
    medidor.arrancar()
    medidor.anotar_consulta(5, "SELECT 1")
    medidor.anotar_puente(700)
    medidor.anotar_puente(300, "formulas")
    medidor.cerrar("/produccion-terminado-asinfo", "GET", 1200)
    f = medidor.resumen()[0]
    assert f["ms_sql_prom"] == 5
    assert f["ms_puente_prom"] == 1000
    assert f["puente_prom"] == 2
    # Y por separado: Asinfo se cachea, formulas no hace falta.
    assert f["ms_asinfo_prom"] == 700 and f["ms_formulas_prom"] == 300
    lenta = medidor.lentas()[0]
    assert lenta["ms_puente"] == 1000 and lenta["puente"] == 2
    assert lenta["ms_asinfo"] == 700 and lenta["ms_formulas"] == 300


def test_una_ida_al_puente_desde_un_hilo_de_fondo_no_se_cuenta():
    medidor.anotar_puente(900)          # sin arrancar(): el calentador
    medidor.arrancar()
    medidor.cerrar("/x", "GET", 10)
    assert medidor.resumen()[0]["ms_puente_prom"] == 0


def test_metabase_le_avisa_al_medidor(monkeypatch):
    """El cable: `_anotar` de metabase_client es lo que cuenta el puente."""
    from modules._lib import metabase_client

    medidor.arrancar()
    metabase_client._anotar(2, 640.0, True)
    medidor.cerrar("/y", "GET", 700)
    assert medidor.resumen()[0]["ms_puente_prom"] == 640


def test_formulas_le_avisa_al_medidor(monkeypatch):
    from unittest.mock import MagicMock

    from modules._lib import formulas_db

    cur = MagicMock()
    cur.fetchall.return_value = [{"a": 1}]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    pool = MagicMock()
    pool.getconn.return_value = conn
    monkeypatch.setattr(formulas_db, "_pool", pool)

    medidor.arrancar()
    formulas_db.fetch_all("SELECT 1")
    formulas_db.fetch_one("SELECT 1")
    medidor.cerrar("/z", "GET", 10)
    assert medidor.resumen()[0]["puente_prom"] == 2
    assert medidor.resumen()[0]["ms_asinfo_prom"] == 0     # fue formulas, no Asinfo


def test_medir_el_puente_no_puede_romper_la_consulta(monkeypatch):
    from modules._lib import formulas_db

    monkeypatch.setattr(medidor, "anotar_puente", lambda ms, *a: 1 / 0)
    formulas_db._medir(0.0)             # no tira: eso es todo el test
    from modules._lib import metabase_client
    metabase_client._anotar(2, 1.0, True)


def test_el_calentador_deja_su_ultimo_ciclo():
    assert medidor.calentador() == {}
    medidor.anotar_calentador(
        [{"paso": "balance", "ms": 900, "error": ""},
         {"paso": "pedidos", "ms": 3000, "error": ""},
         {"paso": "rotativo", "ms": 10, "error": "timeout"}], 3.9)
    c = medidor.calentador()
    assert c["ciclos"] == 1 and c["n_pasos"] == 3 and c["duracion_s"] == 3.9
    assert c["lentos"][0]["paso"] == "pedidos"
    assert [e["paso"] for e in c["errores"]] == ["rotativo"]
    assert c["hace_s"] >= 0
    medidor.anotar_calentador([], 0.1)
    assert medidor.calentador()["ciclos"] == 2
    medidor.limpiar()
    assert medidor.calentador() == {}


def test_un_ciclo_del_calentador_cronometra_cada_paso(monkeypatch):
    """`_warm_once` corre pasos reales contra Asinfo; acá se le cambian todos
    por dos de mentira y se mira que el ciclo quede en el termómetro con un
    paso bueno y uno con error."""
    from modules._lib import warmup

    src = inspect.getsource(warmup._warm_once)
    assert "medidor.anotar_calentador(corridos" in src
    # Y el cable de verdad, sin Asinfo: los pasos fallan todos (no hay bridge),
    # pero el ciclo igual se anota con sus errores.
    warmup._warm_once()
    c = medidor.calentador()
    assert c["ciclos"] == 1 and c["n_pasos"] > 10


def test_la_pantalla_muestra_el_puente_y_el_calentador(app, fake_db):
    medidor.arrancar()
    medidor.anotar_puente(640)
    medidor.cerrar("/produccion-terminado-asinfo", "GET", 700)
    medidor.anotar_calentador([{"paso": "pedidos_pendientes", "ms": 3000, "error": ""}], 3.0)
    html = _login(app, fake_db).get("/admin/pantallas").get_data(as_text=True)
    assert "De eso, puente" in html and "640 ms (1.0)" in html
    assert "El calentador" in html and "pedidos_pendientes" in html


def test_la_pantalla_sin_calentador_lo_dice(app, fake_db):
    html = _login(app, fake_db).get("/admin/pantallas").get_data(as_text=True)
    assert "no terminó ningún ciclo" in html


def test_el_calentador_corre_los_pasos_de_a_varios_y_alinea_primero(monkeypatch):
    """TMT 2026-09-02: un ciclo frío de 88 s en serie dejaba ventanas de más
    de un minuto con las pantallas de Asinfo frías. Los pasos van de a tres,
    menos el primero (alinear el balance), que tira cachés y va solo, antes."""
    import threading

    from modules._lib import warmup

    assert warmup._PASOS_A_LA_VEZ >= 2
    assert warmup._INTERVALO_SECS <= 30
    src = inspect.getsource(warmup._warm_once)
    assert "corridos.append(_correr(pasos[0]))" in src
    assert src.index("_correr(pasos[0])") < src.index("pool.map(_correr, pasos[1:])")

    # Y que de verdad haya más de un hilo trabajando en un ciclo.
    hilos = set()
    from modules.asinfo import service as asvc

    def _paso(*a, **k):
        hilos.add(threading.current_thread().name)
        time.sleep(0.05)
        return []

    for nombre in ("inventario_por_etapa", "movimiento_bodega_mes", "hilado_recibido_mes",
                   "fabricacion_flujo_mes", "despacho_fisico_mes", "importaciones_asinfo",
                   "importaciones_kg", "produccion_tejeduria_mes", "ventas_facturado_kg",
                   "facturas_periodo", "stock_asinfo_lote_totales", "fabricacion_proceso"):
        monkeypatch.setattr(asvc, nombre, _paso, raising=False)
    warmup._warm_once()
    assert len([h for h in hilos if h.startswith("warmup-paso")]) >= 2
