"""NOVEDADES — el buzón único de avisos (TMT 2026-07-30).

Dueña: *"la campanita debería funcionar también para cargas de tejeduría, de
fórmulas, no la hagas para compras. hacé notificaciones globales"*.

Lo que se prueba es lo que hace que el buzón no moleste:
  · `avisar` NUNCA levanta (un aviso roto no puede tumbar la carga que avisaba);
  · la `clave` no deja entrar el mismo aviso dos veces (los procesos de fondo
    reintentan lo mismo cada 30 min);
  · cada fuente escribe en castellano y sin siglas internas.
"""
import contextlib
from unittest.mock import patch

from modules import avisos


def test_avisar_guarda_y_devuelve_true():
    with patch.object(avisos.queries.db, "execute_returning",
                      return_value={"id_aviso": 7}) as f:
        ok = avisos.avisar(fuente="tejeduria", titulo="Reyes · $ 1.630,00",
                           detalle="Se cargaron 2 compras · 1.618,00 kg",
                           importe=1630, cantidad=2, clave="tejeduria:RY:OFT-1")
    assert ok is True
    sql, params = f.call_args[0][0], f.call_args[0][1]
    assert "ON CONFLICT (clave) DO NOTHING" in sql
    assert params[0] == "tejeduria" and params[1] == "ok"


def test_el_indice_de_la_clave_no_es_parcial():
    """Postgres no acepta un índice PARCIAL como árbitro de ON CONFLICT (clave).

    Tiraría 42P10 y, como `avisar()` es fail-soft, el error se tragaría en
    silencio: las cargas seguirían andando y la campanita muda. Pasó en la 0139
    y lo arregla la 0140 — este test evita que vuelva.
    """
    from pathlib import Path

    sql = "\n".join(p.read_text() for p in sorted(Path("migrations").glob("014*.sql"))
                    + sorted(Path("migrations").glob("0139*.sql")))
    assert "CREATE UNIQUE INDEX IF NOT EXISTS aviso_clave_uk ON scintela.aviso (clave);" in sql
    assert "aviso_clave_uk\n    ON scintela.aviso (clave) WHERE" not in sql


def test_la_clave_repetida_no_entra_de_nuevo():
    """ON CONFLICT DO NOTHING → RETURNING no trae fila → False."""
    with patch.object(avisos.queries.db, "execute_returning", return_value=None):
        assert avisos.avisar(fuente="tejeduria", titulo="x",
                             clave="tejeduria:RY:OFT-1") is False


def test_avisar_nunca_levanta():
    def explota(*a, **k):
        raise RuntimeError("la base se cayó")

    with patch.object(avisos.queries.db, "execute_returning", explota):
        assert avisos.avisar(fuente="quimicos", titulo="x") is False


def test_nivel_invalido_cae_en_ok():
    with patch.object(avisos.queries.db, "execute_returning",
                      return_value={"id_aviso": 1}) as f:
        avisos.avisar(fuente="quimicos", titulo="x", nivel="URGENTÍSIMO")
    assert f.call_args[0][1][1] == "ok"


def test_listar_resuelve_icono_y_nombre_de_la_fuente():
    fila = {"fuente": "tejeduria", "nivel": "alerta", "titulo": "t",
            "detalle": "d", "importe": 1, "cantidad": 1, "url": None,
            "leido": False, "cuando": "30/07 15:00", "creado_en": "x"}
    with patch.object(avisos.queries.db, "fetch_all", return_value=[fila]):
        out = avisos.listar()
    assert out[0]["icono"] == "⚠️"
    assert out[0]["fuente_label"] == "Tejeduría"


def test_listar_filtra_por_fuente_y_no_leidos():
    with patch.object(avisos.queries.db, "fetch_all", return_value=[]) as f:
        avisos.listar(solo_no_leidos=True, fuente="quimicos", limite=5)
    sql, params = f.call_args[0][0], f.call_args[0][1]
    assert "NOT leido" in sql and "fuente = %s" in sql
    assert params == ("quimicos", 5)


@contextlib.contextmanager
def _con_columna(hay: bool):
    """Simula que la mig 0145 está (o no) aplicada."""
    avisos.queries.reset_cache()
    try:
        with patch.object(avisos.queries, "_tiene_archivado", return_value=hay):
            yield
    finally:
        avisos.queries.reset_cache()


def test_listar_todo_no_filtra_salvo_archivados():
    """Sin filtros la lista trae todo — menos lo archivado (mig 0145)."""
    with _con_columna(True), \
         patch.object(avisos.queries.db, "fetch_all", return_value=[]) as f:
        avisos.listar(solo_no_leidos=False)
    sql = f.call_args[0][0]
    assert "NOT archivado" in sql
    assert "NOT leido" not in sql and "fuente = %s" not in sql


def test_listar_archivados_muestra_solo_esos():
    with _con_columna(True), \
         patch.object(avisos.queries.db, "fetch_all", return_value=[]) as f:
        avisos.listar(solo_no_leidos=False, archivados=True)
    sql = f.call_args[0][0]
    assert "WHERE archivado" in sql and "NOT archivado" not in sql


# ── El deploy NO corre migraciones (son manuales, por SSM). Entre que sube el
# código y se aplica la 0145 hay una ventana en la que `archivado` no existe.
# TMT 2026-07-30: en esa ventana la pantalla se vio VACÍA y la dueña escribió
# **"ME BORRASTE TODAS!!"**. No faltaba ni un aviso: `listar` es fail-soft y el
# error de columna inexistente se convertía en una lista vacía, que es
# indistinguible de "no hay novedades". Estos tests clavan que la pantalla
# funciona IGUAL que antes mientras la migración no esté.

def test_sin_la_migracion_la_lista_sigue_andando():
    with _con_columna(False), \
         patch.object(avisos.queries.db, "fetch_all", return_value=[]) as f:
        avisos.listar(solo_no_leidos=False)
    sql = f.call_args[0][0]
    assert "archivado" not in sql.replace("FALSE AS archivado", "")
    assert "WHERE" not in sql            # exactamente la query de antes


def test_sin_la_migracion_no_hay_archivados_que_mostrar():
    with _con_columna(False), \
         patch.object(avisos.queries.db, "fetch_all", return_value=[]) as f:
        assert avisos.listar(archivados=True) == []
    f.assert_not_called()                # ni se le pega a la DB


def test_sin_la_migracion_el_contador_no_nombra_la_columna():
    with _con_columna(False), \
         patch.object(avisos.queries.db, "fetch_one", return_value={"n": 3}) as f:
        assert avisos.queries.n_no_leidos() == 3
    assert "archivado" not in f.call_args[0][0]


def test_sin_la_migracion_archivar_no_explota():
    with _con_columna(False), \
         patch.object(avisos.queries.db, "execute", return_value=None) as e:
        assert avisos.queries.archivar(7) is False
    e.assert_not_called()


def test_archivar_y_deshacer():
    """Archivar NO borra: la fila queda y el ↺ la devuelve."""
    with _con_columna(True), \
         patch.object(avisos.queries.db, "execute", return_value=None) as e:
        assert avisos.queries.archivar(7, "tamara") is True
        sql, params = e.call_args[0][0], e.call_args[0][1]
        assert "UPDATE scintela.aviso" in sql and "DELETE" not in sql.upper()
        assert params[0] is True and params[-1] == 7

        avisos.queries.archivar(7, "tamara", deshacer=True)
        assert e.call_args[0][1][0] is False


def test_archivar_es_fail_soft():
    def explota(*a, **k):
        raise RuntimeError("no anda")

    with _con_columna(True), patch.object(avisos.queries.db, "execute", explota):
        assert avisos.queries.archivar(7) is False


def test_el_contador_de_la_campanita_ignora_los_archivados():
    with _con_columna(True), \
         patch.object(avisos.queries.db, "fetch_one", return_value={"n": 3}) as f:
        assert avisos.queries.n_no_leidos() == 3
    assert "NOT archivado" in f.call_args[0][0]


def test_listar_fail_soft_devuelve_vacio():
    def explota(*a, **k):
        raise RuntimeError("timeout")

    with patch.object(avisos.queries.db, "fetch_all", explota):
        assert avisos.listar() == []


def test_marcar_leidos_y_contador():
    with patch.object(avisos.queries.db, "execute") as ex:
        avisos.marcar_leidos()
    assert "SET leido = TRUE" in ex.call_args[0][0]
    with patch.object(avisos.queries.db, "fetch_one", return_value={"n": 3}):
        assert avisos.n_no_leidos() == 3


# ── Las tres fuentes: cada una escribe en castellano, sin siglas internas ────

def test_tejeduria_avisa_una_linea_por_proveedor():
    from modules.tejeduria_asinfo import service as tej

    carga = {"detalle": [
        {"ok": True, "cod": "RY", "label": "REYES", "kg": 809.0,
         "importe": 815.0, "oft": "OFT-1"},
        {"ok": True, "cod": "RY", "label": "REYES", "kg": 809.0,
         "importe": 815.0, "oft": "OFT-2"},
        {"ok": False, "cod": "AP", "label": "PONCE", "motivo": "sin tarifa"},
    ]}
    puestos = []
    with patch("modules.avisos.avisar",
               side_effect=lambda **kw: puestos.append(kw) or True):
        tej._avisar_carga(carga)
    assert len(puestos) == 1  # una sola línea para las dos compras de Reyes
    a = puestos[0]
    assert a["fuente"] == "tejeduria"
    assert a["titulo"] == "REYES · $ 1.630,00"          # formato EU/Ecuador
    assert a["detalle"] == "Se cargaron 2 compras · 1.618,00 kg"
    assert a["clave"] == "tejeduria:RY:OFT-1,OFT-2"     # anti-repetido por OFs
    assert "OFT" not in a["titulo"] + a["detalle"]      # sin siglas internas


def test_quimicos_avisa_el_total_y_los_proveedores():
    from modules.compras import formulas_bridge as fb

    puestos = []
    with patch("modules.avisos.avisar",
               side_effect=lambda **kw: puestos.append(kw) or True):
        fb._avisar_novedades(
            [{"proveedor": "AQ", "factura": "0133", "importe": 5000.0},
             {"proveedor": "SY", "factura": "0134", "importe": 3450.0}], [])
    a = puestos[0]
    assert a["fuente"] == "quimicos"
    assert a["titulo"] == "Químicos · $ 8.450,00"
    assert a["detalle"] == "Se cargaron 2 compras de AQ, SY"
    assert a["clave"] == "quimicos:0133,0134"


def test_quimicos_avisa_los_errores_como_error():
    from modules.compras import formulas_bridge as fb

    puestos = []
    with patch("modules.avisos.avisar",
               side_effect=lambda **kw: puestos.append(kw) or True):
        fb._avisar_novedades([], [{"proveedor": "AQ", "factura": "0140",
                                   "error": "proveedor inexistente"}])
    assert puestos[0]["nivel"] == "error"
    assert puestos[0]["detalle"] == "proveedor inexistente"


def test_quimicos_sin_nada_no_avisa():
    from modules.compras import formulas_bridge as fb

    with patch("modules.avisos.avisar") as av:
        fb._avisar_novedades([], [])
    av.assert_not_called()


def test_importaciones_manda_el_aviso_al_buzon_con_la_clave_de_la_compra():
    from modules.importaciones import autobap

    puestos = []
    with patch("modules.avisos.avisar",
               side_effect=lambda **kw: puestos.append(kw) or True):
        autobap._al_buzon({
            "tipo": "conversion", "im_numero": "IM-1", "codigo_prov": "AC",
            "importe": 100.0, "n_anticipos": 1, "id_compra": 42,
            "comprobante": "BAP1", "ref_num": 7,
        })
    a = puestos[0]
    assert a["fuente"] == "importaciones" and a["nivel"] == "ok"
    assert a["titulo"] == "IM-1 · AC · $ 100,00"
    assert a["clave"] == "importaciones:compra:42"
    assert "BAP" not in a["titulo"] + (a["detalle"] or "")


def test_importaciones_el_freno_es_alerta_una_por_dia():
    from modules.importaciones import autobap

    puestos = []
    with patch("modules.avisos.avisar",
               side_effect=lambda **kw: puestos.append(kw) or True):
        autobap._al_buzon({"tipo": "freno", "n_anticipos": 2, "importe": 1.0})
    assert puestos[0]["nivel"] == "alerta"
    assert puestos[0]["clave"].startswith("importaciones:freno:")


# ── La pantalla NOVEDADES ────────────────────────────────────────────────────

def test_la_pantalla_novedades_existe_y_no_tiene_botones_de_operacion():
    """Dueña: "una pantalla novedades nueva" + "el punto es avisar"."""
    from pathlib import Path

    html = Path("modules/avisos/templates/avisos/lista.html").read_text()
    assert "Novedades" in html
    # Nada de operar desde acá: ni convertir, ni topes, ni switches.
    for prohibido in ("Convertir", "convertir", "tope", "Apagar", "Prendida"):
        assert prohibido not in html


def test_leidos_contesta_204_al_fetch_de_la_campanita():
    """La campanita marca leído por fetch y no espera nada: 204, no redirect."""
    import inspect

    from modules.avisos import views

    src = inspect.getsource(views.leidos)
    assert 'request.form.get("ajax") == "1"' in src
    assert 'return ("", 204)' in src
    # Y el camino normal (sin fetch) sí vuelve a la pantalla.
    assert "redirect(" in src


def test_la_campanita_lee_el_buzon_global_y_apunta_a_novedades():
    from pathlib import Path

    base = Path("templates/base.html").read_text()
    assert "novedades()" in base            # ya no avisos_autobap()
    assert "url_for('avisos.lista')" in base  # "Ver todo" va a Novedades
    assert "url_for(\"avisos.leidos\")" in base  # abrir = leer


def test_el_insert_del_aviso_COMMITEA():
    """El bug que dejó el buzón vacío desde que se estrenó (TMT 2026-07-30).

    `db.fetch_one()` no commitea: psycopg2 le hace rollback a la transacción
    implícita cuando la conexión vuelve al pool. El aviso "entraba", `avisar()`
    devolvía True… y no quedaba nada. Encima es fail-soft, así que no había ni
    un error para mirar: la campanita simplemente no decía nada nunca.
    """
    with patch.object(avisos.queries.db, "execute_returning",
                      return_value={"id_aviso": 1}) as er, \
         patch.object(avisos.queries.db, "fetch_one") as fo:
        assert avisos.avisar(fuente="importaciones", titulo="x",
                             clave="importaciones:compra:1") is True
    assert er.called and not fo.called


def test_backfill_de_los_avisos_que_se_perdieron():
    """La 0141 recupera del historial del automático los avisos que se comió el
    rollback — sin inventar texto (usa el mensaje ya redactado) y idempotente."""
    from pathlib import Path

    sql = Path("migrations/0141_backfill_avisos_importaciones.sql").read_text()
    assert "INSERT INTO scintela.aviso" in sql
    assert "ON CONFLICT (clave) DO NOTHING" in sql
    assert "'importaciones:compra:' || l.id_compra::text" in sql
    assert "l.tipo = 'conversion'" in sql


def test_backfill_completo_usa_el_mismo_resumen():
    """La 0142 trae TAMBIÉN las viejas (dueña: *"faltan las del pasado"*): el
    texto se rearma desde las columnas con `autobap.resumen()`, así las del
    29/07 —que tenían el mensaje largo y con siglas— entran limpias."""
    import importlib.util
    from pathlib import Path

    p = Path("migrations/0142_backfill_avisos_historial_completo.py")
    spec = importlib.util.spec_from_file_location("mig0142", p)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    assert mig._clave({"tipo": "conversion", "id_compra": 7, "creado_en": "2026-07-29",
                       "n_anticipos": 1, "im_numero": "IM-1"}) == "importaciones:compra:7"
    assert mig._clave({"tipo": "freno", "id_compra": None, "n_anticipos": 2,
                       "creado_en": "2026-07-29 15:40", "im_numero": None}) \
        == "importaciones:freno:2026-07-29:2"
    assert mig._NIVEL["freno"] == "alerta"


def test_ningun_insert_returning_usa_fetch_one():
    """El bug de "escribe y se pierde", clavado para todo el repo.

    `db.fetch_one()` no commitea: un INSERT/UPDATE/DELETE ... RETURNING hecho
    con él se pierde en silencio (psycopg2 hace rollback al devolver la conexión
    al pool). El helper correcto es `db.execute_returning()` — salvo que el
    llamador pase `conn=` de un `db.tx()`, que commitea él.

    `PENDIENTES` está vacío a propósito: si algún día hay que dejar uno afuera,
    va acá con el motivo escrito, no se saca el test.
    """
    import re
    from pathlib import Path

    PENDIENTES: set[str] = set()  # 30/07: ya no queda ninguno (dueña: "dale con todo")
    ESCRIBE = re.compile(r"^[\s\"\'f]*(INSERT|UPDATE|DELETE)\b", re.IGNORECASE)

    def llamadas(texto: str):
        """Cada `fetch_one(...)` completo, cerrando paréntesis de verdad."""
        for m in re.finditer(r"fetch_one\(", texto):
            i, prof = m.end(), 1
            while i < len(texto) and prof:
                prof += (texto[i] == "(") - (texto[i] == ")")
                i += 1
            yield texto[m.end():i]

    malos = []
    for p in sorted(Path("modules").rglob("*.py")):
        if p.as_posix() in PENDIENTES:
            continue
        for call in llamadas(p.read_text(encoding="utf-8")):
            if ESCRIBE.match(call.lstrip()) and "conn=" not in call:
                malos.append(p.as_posix())
    assert not malos, f"INSERT/UPDATE ... RETURNING con fetch_one (no commitea): {sorted(set(malos))}"


def test_el_flag_se_entera_de_la_migracion_sin_reiniciar():
    """Aplicada la 0145 por /admin/migraciones, la pantalla se actualiza sola.

    TMT 2026-07-30. El "todavía no está" se re-chequea cada minuto; el "sí
    está" no vence nunca (una columna no se va). Misma lección que la caché de
    Metabase del 29/07: **no cachear el negativo tanto como el positivo**.
    """
    avisos.queries.reset_cache()
    try:
        # 1) Antes de la migración: no está, y se cachea el "no".
        with patch.object(avisos.queries.db, "fetch_one", return_value=None) as f:
            assert avisos.queries._tiene_archivado() is False
            assert avisos.queries._tiene_archivado() is False
            assert f.call_count == 1          # el negativo se cachea…

        # 2) Pasa el TTL y la migración ya corrió → se entera sola.
        avisos.queries._TIENE_ARCHIVADO_TS -= avisos.queries._TTL_SIN_COLUMNA + 1
        with patch.object(avisos.queries.db, "fetch_one", return_value={"ok": 1}):
            assert avisos.queries._tiene_archivado() is True

        # 3) El "sí" no se vuelve a preguntar nunca.
        with patch.object(avisos.queries.db, "fetch_one", return_value=None) as f:
            assert avisos.queries._tiene_archivado() is True
            f.assert_not_called()
    finally:
        avisos.queries.reset_cache()


def test_la_hora_del_aviso_se_muestra_en_ECUADOR():
    """El server corre en UTC (+5): sin convertir, el cierre de las 18:00 se
    mostraba "23:01". Verificado en vivo el 30/07 con el primer aviso real."""
    with patch.object(avisos.queries.db, "fetch_all", return_value=[]) as f:
        avisos.listar()
    sql = f.call_args[0][0]
    assert "AT TIME ZONE 'America/Guayaquil'" in sql
    assert "TO_CHAR(creado_en," not in sql.replace(
        "TO_CHAR(creado_en AT TIME ZONE", "")


# ── mig 0146 — LEÍDO ES POR PERSONA ──────────────────────────────────────────
# Dueña 2026-07-31: *"las notificaciones tienen que ser por persona. si yo la
# leo igual le tienen que aparecer no leídas a federico andres y alex"*.
# Antes `leido` era un booleano de la FILA: el primero que abría la campanita
# la apagaba para los cuatro. Lo que se prueba acá es que ya no.

@contextlib.contextmanager
def _como(usuario: str | None):
    """Simula la mig 0146 aplicada y a `usuario` mirando la pantalla.

    `usuario=None` = la migración NO está (o es un proceso de fondo, sin
    request): tiene que comportarse EXACTAMENTE como antes.
    """
    avisos.queries.reset_cache()
    try:
        with patch.object(avisos.queries, "_tiene_leido_por_usuario",
                          return_value=usuario is not None), \
             patch.object(avisos.queries, "usuario_actual",
                          return_value=usuario or ""):
            yield
    finally:
        avisos.queries.reset_cache()


def test_la_campanita_pregunta_por_lo_que_NO_leyo_ESTA_persona():
    with _como("federico"), \
         patch.object(avisos.queries.db, "fetch_all", return_value=[]) as f:
        avisos.listar(solo_no_leidos=True)
    sql, params = f.call_args[0][0], f.call_args[0][1]
    assert "LEFT JOIN scintela.aviso_leido al" in sql
    assert "al.usuario IS NULL" in sql       # nadie más se lo puede marcar
    assert params[0] == "federico"           # el JOIN va primero → su param también


def test_leer_no_marca_la_fila_sino_el_par_aviso_persona():
    with _como("tamara"), patch.object(avisos.queries.db, "execute") as ex:
        avisos.marcar_leidos()
    sql, params = ex.call_args[0][0], ex.call_args[0][1]
    assert "INSERT INTO scintela.aviso_leido" in sql
    assert "UPDATE" not in sql.upper()       # la fila del aviso NO se toca
    assert "ON CONFLICT (id_aviso, usuario) DO NOTHING" in sql
    assert params[0] == "tamara"


def test_reabrir_la_campanita_no_duplica_ni_pisa_la_primera_vez():
    """`ON CONFLICT DO NOTHING`, no `DO UPDATE`: leido_en es la 1ª vez."""
    with _como("alex"), patch.object(avisos.queries.db, "execute") as ex:
        avisos.marcar_leidos()
        avisos.marcar_leidos()
    assert "DO NOTHING" in ex.call_args[0][0]
    assert "DO UPDATE" not in ex.call_args[0][0]


def test_el_contador_es_de_cada_uno():
    with _como("andres"), \
         patch.object(avisos.queries.db, "fetch_one", return_value={"n": 5}) as f:
        assert avisos.n_no_leidos() == 5
    sql, params = f.call_args[0][0], f.call_args[0][1]
    assert "aviso_leido" in sql and "al.usuario IS NULL" in sql
    assert params[0] == "andres"


def test_el_historial_ya_leido_no_reaparece_al_aplicar_la_migracion():
    """La columna vieja `leido` sigue valiendo como 'leído por todos'.

    Sin esto, aplicar la 0146 haría reaparecer de golpe como NUEVO todo lo que
    ya se había leído — el ruido que vacía una campanita.
    """
    with _como("federico"), \
         patch.object(avisos.queries.db, "fetch_all", return_value=[]) as f:
        avisos.listar(solo_no_leidos=True)
    assert "NOT leido AND al.usuario IS NULL" in f.call_args[0][0]


def test_sin_la_migracion_todo_se_comporta_como_antes():
    with _como(None), patch.object(avisos.queries.db, "execute") as ex:
        avisos.marcar_leidos()
    assert "SET leido = TRUE" in ex.call_args[0][0]
    with _como(None), \
         patch.object(avisos.queries.db, "fetch_all", return_value=[]) as f:
        avisos.listar(solo_no_leidos=True)
    sql = f.call_args[0][0]
    assert "aviso_leido" not in sql and "NOT leido" in sql


def test_un_proceso_de_fondo_no_marca_leido_a_nombre_de_nadie():
    """Sin request no hay persona: `usuario_actual()` devuelve ''."""
    assert avisos.queries.usuario_actual() == ""


def test_archivar_sigue_siendo_global_a_proposito():
    """'Ya no aplica' es un hecho del mundo; 'leído' es de cada uno."""
    with _con_columna(True), _como("tamara"), \
         patch.object(avisos.queries.db, "execute", return_value=None) as e:
        avisos.queries.archivar(7, "tamara")
    assert "UPDATE scintela.aviso" in e.call_args[0][0]
    assert "aviso_leido" not in e.call_args[0][0]
