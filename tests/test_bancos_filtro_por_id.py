"""`/bancos/<no_banco>?id=<id_transaccion>` tiene que mostrar ESE movimiento.

TMT 2026-08-07 (dueña, sobre los links del historial): *"esos links deberían
venir filtrados por lo que quiero ver"* y, más filoso: *"si al clickear hay
que buscar la fila a ojo, el link no está terminado"*.

`transacciones_bancarias` es el destino de MÁS volumen de todo el Historial —
1.862 movimientos apuntando a 979 filas — y hasta hoy no linkeaba a ningún
lado: `link_origen` devolvía `None` y la columna mostraba el nombre del banco
como texto muerto. No existía NINGUNA pantalla que mostrara un movimiento
bancario: todo lo que toma un `id_transaccion` es una ACCIÓN (mover de banco,
borrar, conciliar, imprimir el cheque). El libro del banco es la pantalla que
ya existe; lo único que le faltaba era poder pedirle UNA fila.

Lo que muerde acá no es el WHERE, son los DEFAULTS que esconden justo esa fila:

  * el **tope de 500**. `queries.movimientos` lista `ORDER BY t.fecha DESC
    LIMIT 500` sin OFFSET y la vista nunca pasa `limite`. Medido: 167 de las
    371 filas linkeadas desde el historial caen más abajo de ese tope — con un
    recorte en Python después de traer las filas, esos 167 links seguirían sin
    llegar a ningún lado. Por eso el filtro va en el WHERE, ANTES del LIMIT.
  * el combo **Conciliado**, que recortaba las filas después del fetch.
  * los **agregados**: "Mostrando N de M" y "Total filtrado" son del universo
    filtrado, así que reciben el id. El SALDO DEL BANCO no: es plata real.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.bancos import queries as bq  # noqa: E402

ID_TX = 8123          # el movimiento que menciona el historial
NO_BANCO = 9          # Pichincha


# ── 1) el SQL de la lista ────────────────────────────────────────────────────

class _CapturaSQL:
    """Guarda TODAS las consultas: `movimientos()` hace varias (conciliación,
    cheques del depósito, nombres de proveedor) y quedarse con la última
    captura la equivocada."""

    def __init__(self):
        self.queries: list[tuple[str, dict]] = []

    def fetch_all(self, sql, params=None):
        self.queries.append((" ".join(sql.split()), params or {}))
        return []

    def fetch_one(self, sql, params=None):
        self.queries.append((" ".join(sql.split()), params or {}))
        return {}

    def principal(self) -> tuple[str, dict]:
        """La consulta que lista los movimientos — la que recibe el id."""
        for sql, params in self.queries:
            if isinstance(params, dict) and "id_transaccion" in params:
                return sql, params
        raise AssertionError(
            "ninguna consulta recibió el parámetro id_transaccion; se hicieron: "
            + " | ".join(q[:70] for q, _ in self.queries))


@pytest.fixture
def cap(monkeypatch):
    c = _CapturaSQL()
    monkeypatch.setattr(bq, "db", c)
    return c


def test_pedir_un_movimiento_filtra_por_ese_id(cap):
    bq.movimientos(NO_BANCO, id_transaccion=ID_TX)
    sql, params = cap.principal()
    assert params["id_transaccion"] == ID_TX
    assert ("%(id_transaccion)s::int IS NULL "
            "OR t.id_transaccion = %(id_transaccion)s::int") in sql


def test_el_filtro_por_id_actua_ANTES_del_tope_de_500(cap):
    """El bug que este test cuida: recortar en Python las 500 filas que ya
    vinieron deja afuera los 167 movimientos linkeados que caen más abajo del
    tope. El predicado tiene que estar en el WHERE, antes del LIMIT."""
    bq.movimientos(NO_BANCO, id_transaccion=ID_TX)
    sql, params = cap.principal()
    pos_id = sql.index("t.id_transaccion = %(id_transaccion)s::int")
    pos_limit = sql.index("LIMIT %(limite)s")
    assert pos_id < pos_limit
    # Y el tope sigue existiendo: subirlo NO es el arreglo (traer 50.000 filas
    # para mostrar una sola sería peor que el problema).
    assert params["limite"] == 500


def test_pedir_por_id_apaga_los_filtros_que_esconderian_la_fila(cap):
    """El link llega sin fechas ni monto, pero el usuario puede tener el
    formulario cargado de la búsqueda anterior. Un movimiento de mayo con el
    rango puesto en agosto no puede desaparecer."""
    bq.movimientos(NO_BANCO, desde="2026-08-01", hasta="2026-08-07",
                   monto_min=1.0, monto_max=1.999999, doc_num="777",
                   cliente="MSS", id_transaccion=ID_TX)
    sql, _ = cap.principal()
    for apagado in (
        "%(id_transaccion)s::int IS NOT NULL OR %(desde)s::date IS NULL",
        "%(id_transaccion)s::int IS NOT NULL OR %(hasta)s::date IS NULL",
        "%(id_transaccion)s::int IS NOT NULL OR %(monto_min)s::numeric IS NULL",
        "%(id_transaccion)s::int IS NOT NULL OR %(doc_like)s IS NULL",
        "%(id_transaccion)s::int IS NOT NULL OR %(cliente_like)s IS NULL",
    ):
        assert apagado in sql, f"no se apaga: {apagado}"


def test_el_BANCO_no_se_apaga_ni_pidiendo_por_id(cap):
    """Si el id pedido es de OTRO banco, la lista sale VACÍA — que es la
    verdad. Saltear `no_banco` haría que /bancos/9?id=<algo de Internacional>
    mostrara una fila del banco equivocado bajo el título de Pichincha."""
    bq.movimientos(NO_BANCO, id_transaccion=ID_TX)
    sql, params = cap.principal()
    assert "WHERE t.no_banco = %(no_banco)s" in sql
    assert "IS NOT NULL OR t.no_banco" not in sql   # nada de bypass
    assert params["no_banco"] == NO_BANCO


def test_sin_id_la_lista_no_cambia(cap):
    """El caso normal sigue igual: el id viaja NULL y no filtra nada."""
    bq.movimientos(NO_BANCO, desde="2026-08-01")
    _, params = cap.principal()
    assert params["id_transaccion"] is None


def test_un_id_vacio_o_cero_no_filtra(cap):
    """`0` no es un id_transaccion válido; tratarlo como filtro daría una
    lista vacía en vez del libro completo."""
    bq.movimientos(NO_BANCO, id_transaccion=0)
    _, params = cap.principal()
    assert params["id_transaccion"] is None


# ── 2) la pantalla ───────────────────────────────────────────────────────────

SALDO_DEL_BANCO = 250_000.55     # plata real de Pichincha
IMPORTE_DEL_MOV = 150.0          # el movimiento que se pidió


class _FakeAgregados:
    """Contesta los dos agregados que hace la vista y se guarda con qué
    parámetros los pidió."""

    def __init__(self):
        self.agg: tuple[str, dict] | None = None
        self.pendientes: tuple[str, dict] | None = None

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split())
        if "AS absoluta" in s:                    # total/contador del filtro
            self.agg = (s, params or {})
            return {"n": 1, "signed": -IMPORTE_DEL_MOV, "absoluta": IMPORTE_DEL_MOV}
        if "banco_conciliacion_match" in s:       # movs pendientes de conciliar
            self.pendientes = (s, params or {})
            return {"n": 3, "signed": 1000.0}
        return None

    def fetch_all(self, sql, params=None, conn=None):
        return []

    def execute(self, sql, params=None, conn=None):
        return 0


@pytest.fixture
def app_logueada(app):
    """La app real con una sesión de Accionista (wildcard de permisos).

    Se registra DESPUÉS de create_app() a propósito: `app.py` importa
    `load_logged_in_user` en el import, así que pisarlo no sirve una vez que
    el módulo ya está cargado."""
    @app.before_request
    def _login_falso():  # pragma: no cover - infra de test
        from flask import g, session
        session["usuario_id"] = 1
        g.user = {"id_usuario": 1, "username": "tamara", "id_rol": 1,
                  "nombre_rol": "Accionista", "activo": True}
        g.permisos = {"*"}

    return app


@pytest.fixture
def pantalla(app_logueada, monkeypatch):
    """Devuelve (client, vistos, agregados, contexto) con la vista enchufada
    a fakes: `queries.movimientos` registra los kwargs recibidos y los dos
    agregados contestan números fijos."""
    import db as _db
    from modules.bancos import views as bv

    vistos: dict = {}
    fake = _FakeAgregados()

    def fake_movimientos(no_banco, desde=None, hasta=None, **kw):
        vistos["no_banco"] = no_banco
        vistos["desde"], vistos["hasta"] = desde, hasta
        vistos.update(kw)
        # Una fila YA CONCILIADA a propósito: si el combo Conciliado siguiera
        # recortando, con ?conciliado=no esta fila se caía de la lista.
        return [{"id_transaccion": ID_TX, "no_banco": no_banco, "no_cta": "",
                 "fecha": "2026-05-02", "documento": "CH", "fechad": None,
                 "concepto": "PAGO PROVEEDOR", "importe": IMPORTE_DEL_MOV,
                 "saldo": SALDO_DEL_BANCO, "stat": "*", "prov": "",
                 "numreferencia": "777", "numreferencia_manual": None,
                 "usuario_crea": "web", "fecha_crea": None,
                 "conciliacion_id": 77}]

    monkeypatch.setattr(bv.queries, "movimientos", fake_movimientos)
    monkeypatch.setattr(bv.queries, "banco_info", lambda nb: {
        "no_banco": nb, "nombre": "PICHINCHA", "saldo_stored": SALDO_DEL_BANCO})
    monkeypatch.setattr(_db, "fetch_one", fake.fetch_one)
    monkeypatch.setattr(_db, "fetch_all", fake.fetch_all)

    ctx: dict = {}
    from flask import template_rendered

    def _capturar(sender, template, context, **extra):  # pragma: no cover - infra
        if template.name == "bancos/movimientos.html":
            ctx.clear()
            ctx.update(context)

    template_rendered.connect(_capturar, app_logueada)
    try:
        yield app_logueada.test_client(), vistos, fake, ctx
    finally:
        template_rendered.disconnect(_capturar, app_logueada)


def test_la_pantalla_le_pasa_el_id_a_la_consulta(pantalla):
    client, vistos, _, _ = pantalla
    r = client.get(f"/bancos/{NO_BANCO}?id={ID_TX}")
    assert r.status_code == 200
    assert vistos["id_transaccion"] == ID_TX
    assert vistos["no_banco"] == NO_BANCO      # el banco de la URL, intacto


def test_un_id_que_no_es_un_numero_se_ignora(pantalla):
    """?id=borrar-todo no puede tirar un 500 ni colarse en el SQL."""
    client, vistos, _, _ = pantalla
    r = client.get(f"/bancos/{NO_BANCO}?id=borrar-todo")
    assert r.status_code == 200
    assert vistos["id_transaccion"] is None


def test_el_combo_conciliado_no_puede_esconder_la_fila_pedida(pantalla):
    """El link del historial no sabe si el movimiento está conciliado; el
    recorte post-fetch por `conciliacion_id` lo hacía desaparecer."""
    client, _, _, ctx = pantalla
    r = client.get(f"/bancos/{NO_BANCO}?id={ID_TX}&conciliado=no")
    assert r.status_code == 200
    assert len(ctx["filas"]) == 1
    assert ctx["filas"][0]["id_transaccion"] == ID_TX


def test_el_contador_y_el_total_del_encabezado_son_de_ESE_movimiento(pantalla):
    """"Mostrando 1 de N" y "Total filtrado" salen de un agregado aparte (sin
    LIMIT). Sin propagarle el id, abrir un movimiento de $150 mostraba el
    total del banco entero arriba de una sola fila."""
    client, _, fake, ctx = pantalla
    client.get(f"/bancos/{NO_BANCO}?id={ID_TX}")
    sql_agg, params_agg = fake.agg
    assert "t.id_transaccion = %(id_tx)s::int" in sql_agg
    assert params_agg["id_tx"] == ID_TX
    assert ctx["n_filtrado"] == 1
    assert ctx["total_filtrado"] == -IMPORTE_DEL_MOV


def test_el_SALDO_DEL_BANCO_no_se_filtra_porque_es_plata_real(pantalla):
    """⚠ Decisión explícita: el "Saldo: $X" del encabezado y el "Saldo
    conciliado" son plata que está en el banco, no un total de lo que se está
    mirando. Filtrarlos diría "Pichincha: $150" al abrir un movimiento de
    $150 — una mentira. Este test los deja clavados sin filtrar."""
    client, _, fake, ctx = pantalla
    client.get(f"/bancos/{NO_BANCO}?id={ID_TX}")
    assert ctx["saldo_banco"] == SALDO_DEL_BANCO
    # el agregado de pendientes (saldo conciliado) no vio el id
    _, params_pend = fake.pendientes
    assert "id_tx" not in params_pend
    assert list(params_pend) == ["no_banco"]
    assert ctx["n_conciliado"] == 3            # los 3 pendientes del banco
    assert ctx["saldo_conciliado"] == round(SALDO_DEL_BANCO - 1000.0, 2)


def test_la_pantalla_filtrada_no_agrega_ningun_cartel(pantalla):
    """DECISIÓN DE LA DUEÑA (2026-08-07, sobre el mismo cambio en /posdat):
    *"sin el cartel pero el resto perfecto"*. Este test está dado vuelta a
    propósito, no borrado: si mañana alguien mete un banner "mostrando una
    sola fila", que falle acá y lea esta decisión antes de re-discutirla.
    El link "Limpiar" que ya existía alcanza para volver al libro entero."""
    client, _, _, _ = pantalla
    html = client.get(f"/bancos/{NO_BANCO}?id={ID_TX}").get_data(as_text=True)
    for cartel in ("Mostrando un solo", "un solo movimiento", "ver todos los movimientos"):
        assert cartel not in html
    assert "Limpiar" in html           # la salida de siempre, sin adornos


def test_sin_id_la_pantalla_se_comporta_igual_que_siempre(pantalla):
    """Regresión: el libro completo no puede haber cambiado."""
    client, vistos, fake, ctx = pantalla
    r = client.get(f"/bancos/{NO_BANCO}?desde=2026-08-01")
    assert r.status_code == 200
    assert vistos["id_transaccion"] is None
    assert vistos["desde"] == "2026-08-01"
    sql_agg, params_agg = fake.agg
    assert "id_tx" not in params_agg
    assert "t.fecha >= %(desde)s::date" in sql_agg
    assert ctx["saldo_banco"] == SALDO_DEL_BANCO
