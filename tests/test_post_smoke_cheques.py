"""POST smoke del módulo CHEQUES — apretar el botón, no sólo abrir la pantalla.

POR QUÉ
-------
`test_routes_smoke.py` recorre las rutas GET con la base vacía y un stub de
`db`: prueba que la pantalla ABRE. Cheques es la máquina de estados que mueve
la plata y tenía `views.py` en 37% — `endosar`, `desaplicar_factura` y
`reversar_endoso` no las nombraba ningún test. El 500 de la conciliación del
13/08 (una clave que faltaba en el dict que iba al template) es exactamente la
clase de bug que sólo se ve POSTEANDO.

CÓMO
----
Una siembra propia por SQL arma el escenario —un cheque en cada `stat` de
`modules/cheques/estados.py`, un cliente, facturas, bancos y los `mov_doble`
que las pantallas de "deshacer" necesitan para existir— y después cada botón
se aprieta por su RUTA REAL. Sembrar por SQL acá no contradice "operar por las
pantallas": no estamos operando producción, estamos construyendo el escenario.

El barrido junta TODO en un `Barrido` y afirma una sola vez al final. Fallar de
a una obliga a arreglar-correr-arreglar; el barrido entero en un mensaje deja
ver si lo roto es una ruta o un patrón.

Lo que NO se cubre a propósito: `/cheques/diag/...` — herramientas de
diagnóstico de una sola vez, fuera del inventario.
"""
from __future__ import annotations

import io
import json
import os
import traceback
from datetime import date, timedelta

import pytest

from tests._smoke_base import (
    Barrido,
    CazaTragadas,
    app_smoke,
    conexion,
    db_smoke,
)
from tests._smoke_base import getear as _getear_crudo
from tests._smoke_base import postear as _postear_crudo

pytestmark = pytest.mark.db

#: Base propia de este agente. Los otros dos módulos del POST smoke usan
#: `pc_facturas` y `pc_bancos`: nadie pisa la siembra de nadie.
DB = db_smoke()

#: Código de cliente de 3 letras — la casa no usa dos iniciales.
CLI = "CHQ"
PROV = "PRV"

HOY = date.today()
AYER = HOY - timedelta(days=1)
FUTURO = HOY + timedelta(days=30)

#: Los bancos que las rutas buscan POR NOMBRE, no por número: `transicionar`
#: y `depositar_lote` resuelven Pichincha/Internacional con un match de texto
#: justamente porque los `no_banco` legacy no son estables (caso ch14778 BYG,
#: 31/07: un número hardcodeado mandó un depósito a un banco inexistente).
BANCOS = [
    (10, "PICHINCHA"),
    (20, "INTERNACIONAL"),
    (90, "DEP.PICH."),
    (91, "DEP.INTER."),
    (95, "CANCELA ANT."),
    (97, "ANTICIPO"),
    (98, "SALDO A FAVOR"),
    (99, "EFECTIVO"),
]

#: Tablas que la siembra pisa. Se vacían antes para que la corrida sea
#: repetible: un smoke que depende de lo que dejó la corrida anterior miente.
TABLAS = [
    "chequesxfact",
    "chequextransaccion",
    "cheque",
    "factura",
    "mov_doble",
    "caja",
    "posdat",
    "transacciones_bancarias",
    "compra",
    "retencion",
    "cliente",
    "proveedor",
    "aviso",
]


# ══════════════════════════════════════════════════════════════════════════
# SIEMBRA
# ══════════════════════════════════════════════════════════════════════════

def _cheque(cur, *, stat: str, importe: float = 100.0, no_banco: int = 10,
            no_cheque: str | None = None, fechad: date | None = None,
            fecha: date | None = None, prov: str | None = None,
            fechad_original: date | None = None) -> int:
    """Un cheque del cliente de prueba en el `stat` pedido. Devuelve su id.

    Se inserta por SQL y no por `queries.crear` a propósito: `crear` ya tiene
    sus propios tests y acá lo que se necesita es PONER el cheque en un estado
    (por ejemplo 'E' endosado o 'X' anulado) al que la ruta de alta no llega.
    """
    cur.execute(
        "INSERT INTO scintela.cheque "
        "  (no_cheque, fecha, fechad, fechad_original, codigo_cli, importe, "
        "   no_banco, banco, stat, prov, fecha_recibido, usuario_crea) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'smoke') "
        "RETURNING id_cheque",
        (
            no_cheque, fecha or HOY, fechad or HOY, fechad_original, CLI,
            importe, no_banco, "PICHINCHA", stat, prov, fecha or HOY,
        ),
    )
    return int(cur.fetchone()["id_cheque"])


def _factura(cur, *, numf: int, importe: float, abono: float = 0.0,
             stat: str = "Z") -> int:
    """Una factura del cliente. `saldo` se calcula: la cartera lee ese campo."""
    cur.execute(
        "INSERT INTO scintela.factura "
        "  (numf, fecha, codigo_cli, kg, importe, abono, saldo, stat, "
        "   vencimiento, usuario_crea) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'smoke') RETURNING id_factura",
        (numf, AYER, CLI, 10, importe, abono, round(importe - abono, 2), stat,
         HOY + timedelta(days=90)),
    )
    return int(cur.fetchone()["id_factura"])


def _mov(cur, *, tipo: str, id_cheque: int, importe: float,
         metadata: dict) -> int:
    """Un `mov_doble` con la metadata que su pantalla de deshacer exige.

    Las tres rutas de "deshacer" (`protesto/…/deshacer`,
    `anulacion-error-carga/…/deshacer`, `cancelacion-anticipo/…/reversar`) se
    NIEGAN si la metadata no dice de qué estado venía el cheque. Sin estas
    filas no habría manera de posterlas: el 404 del lookup taparía las ~300
    líneas que vienen después.
    """
    cur.execute(
        "INSERT INTO scintela.mov_doble "
        "  (fecha_operacion, tipo, origen_table, origen_id, destino_table, "
        "   destino_id, importe, concepto, usuario, estado, metadata) "
        "VALUES (%s,%s,'cheque',%s,'cheque',%s,%s,%s,'smoke','activo',%s) "
        "RETURNING id_mov_doble",
        (HOY, tipo, id_cheque, id_cheque, importe, f"smoke {tipo}",
         json.dumps(metadata)),
    )
    return int(cur.fetchone()["id_mov_doble"])


def _sembrar(conn) -> dict:
    """El escenario mínimo para que ninguna ruta muera en "no existe".

    Devuelve los ids en un dict — el barrido arma las URLs con eso.
    """
    ids: dict = {}
    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE " + ", ".join("scintela." + t for t in TABLAS)
            + " RESTART IDENTITY CASCADE"
        )
        for no_banco, nombre in BANCOS:
            cur.execute(
                "INSERT INTO scintela.banco (no_banco, nombre, usuario_crea) "
                "VALUES (%s,%s,'smoke') ON CONFLICT (no_banco) DO UPDATE "
                "SET nombre = EXCLUDED.nombre",
                (no_banco, nombre),
            )
        cur.execute(
            "INSERT INTO scintela.cliente "
            "  (codigo_cli, nombre, ruc, cupo, vend, activo, usuario_crea) "
            "VALUES (%s,'CLIENTE SMOKE CHEQUES','9999999999001',50000,'PPR',"
            "        true,'smoke')",
            (CLI,),
        )
        cur.execute(
            "INSERT INTO scintela.proveedor "
            "  (codigo_prov, nombre, ruc, tipo, plazo, activo, usuario_crea) "
            "VALUES (%s,'PROVEEDOR SMOKE','9999999999002','C',30,'1','smoke')",
            (PROV,),
        )

        # ── Facturas ────────────────────────────────────────────────────
        # Una pendiente grande (para aplicar), una ya cancelada con su
        # aplicación viva (para desaplicar) y una NOTA DE CRÉDITO (saldo
        # negativo): el camino "el cliente tiene NC sin usar" de /cheques/nuevo
        # sólo se recorre si existe una factura con saldo < 0.
        ids["factura"] = _factura(cur, numf=9001, importe=1000.0)
        ids["factura_chica"] = _factura(cur, numf=9002, importe=120.0)
        ids["factura_aplicada"] = _factura(
            cur, numf=9003, importe=500.0, abono=500.0, stat="T")
        ids["factura_nc"] = _factura(cur, numf=9004, importe=-80.0, stat="A")

        # ── Un cheque por cada estado de la tabla de `estados.py` ────────
        ids["cartera"] = _cheque(cur, stat="Z", no_cheque="C0001")
        ids["postergado"] = _cheque(cur, stat="P", no_cheque="C0002",
                                    fechad=FUTURO)
        ids["daniela"] = _cheque(cur, stat="D", no_cheque="C0003")
        ids["devuelto"] = _cheque(cur, stat="1", no_cheque="C0004")
        ids["devuelto2"] = _cheque(cur, stat="2", no_cheque="C0005")
        ids["devuelto3"] = _cheque(cur, stat="3", no_cheque="C0006")
        ids["depositado"] = _cheque(cur, stat="B", no_cheque="C0007")
        ids["depositado_inter"] = _cheque(cur, stat="I", no_cheque="C0008",
                                          no_banco=20)
        ids["redepositado"] = _cheque(cur, stat="V", no_cheque="C0009")
        ids["caja"] = _cheque(cur, stat="C", no_cheque="C0010", no_banco=99)
        ids["anulado"] = _cheque(cur, stat="X", no_cheque="C0011")
        ids["endosado"] = _cheque(cur, stat="E", no_cheque="C0012", prov=PROV)
        ids["acreditado"] = _cheque(cur, stat="A", no_cheque="C0013")
        ids["reversado"] = _cheque(cur, stat="R", no_cheque="C0014")
        ids["cobrado"] = _cheque(cur, stat="T", no_cheque="C0015")
        ids["rebotado"] = _cheque(cur, stat="9", no_cheque="C0016")

        # ── Anticipo y su espejo NB=98 ──────────────────────────────────
        # El anticipo vive en el banco 97 y deja un espejo NEGATIVO en el 98
        # (el saldo a favor del cliente). `espejo_de_anticipo` lo busca por
        # `id_cheque_padre`, así que el par tiene que existir de verdad.
        ids["anticipo"] = _cheque(cur, stat="Z", no_cheque="C0017",
                                  importe=3000.0, no_banco=97)
        cur.execute(
            "INSERT INTO scintela.cheque "
            "  (no_cheque, fecha, fechad, codigo_cli, importe, no_banco, "
            "   stat, id_cheque_padre, usuario_crea) "
            "VALUES ('C0017E',%s,%s,%s,-3000.0,98,'Z',%s,'smoke') "
            "RETURNING id_cheque",
            (HOY, HOY, CLI, ids["anticipo"]),
        )
        ids["espejo"] = int(cur.fetchone()["id_cheque"])
        # Un segundo espejo, chico, para el CANCELA ANTICIPO (95): `crear()`
        # aparea el 95 contra un NB=97/98 del MISMO cliente por el importe
        # EXACTO en negativo. Sin un espejo de -80 el 95 de $80 se queda en
        # cartera con un warning y la rama contable no se recorre.
        cur.execute(
            "INSERT INTO scintela.cheque "
            "  (no_cheque, fecha, fechad, codigo_cli, importe, no_banco, "
            "   stat, usuario_crea) "
            "VALUES ('C0017F',%s,%s,%s,-80.0,98,'Z','smoke') "
            "RETURNING id_cheque",
            (HOY, HOY, CLI),
        )
        ids["espejo_chico"] = int(cur.fetchone()["id_cheque"])
        # Y un RESIDUO de retención: espejo NB=98 de centavos (el control del
        # 29/07 los tenía repartidos en 6 clientes por −100,45). Sin uno de
        # verdad, `/cheques/residuos-retencion/eliminar` contesta "no se anuló
        # ninguno" y las ~30 líneas de la anulación no se recorren.
        cur.execute(
            "INSERT INTO scintela.cheque "
            "  (no_cheque, fecha, fechad, codigo_cli, importe, no_banco, "
            "   stat, id_cheque_padre, usuario_crea) "
            "VALUES ('C0017R',%s,%s,%s,-0.45,98,'Z',%s,'smoke') "
            "RETURNING id_cheque",
            (HOY, HOY, CLI, ids["anticipo"]),
        )
        ids["residuo"] = int(cur.fetchone()["id_cheque"])

        # ── El cheque aplicado a una factura ────────────────────────────
        # `desaplicar` y `confirmar-desaplicar` chequean que exista la fila en
        # chequesxfact ANTES de hacer nada: sin ella la ruta se va por el
        # flash y no toca la query real.
        ids["aplicado"] = _cheque(cur, stat="Z", no_cheque="C0018",
                                  importe=500.0)
        cur.execute(
            "INSERT INTO scintela.chequesxfact "
            "  (id_cheque, id_fact, fechaing, codigo_cli, importe, no_banco, "
            "   tipo, stat_f, abono_f, saldo_f, usuario_crea) "
            "VALUES (%s,%s,%s,%s,500.0,10,'CH','T',500.0,0.0,'smoke')",
            (ids["aplicado"], ids["factura_aplicada"], HOY, CLI),
        )

        # ── Los tres mov_doble de las pantallas de "deshacer" ────────────
        # (1) protesto: el cheque quedó en '1' y venía de 'B'. `stat_prev` y
        #     `stat_destino` son obligatorios — sin ellos la ruta contesta
        #     "no lo restauro a ciegas" y no ejercita nada.
        ids["protestado"] = _cheque(cur, stat="1", no_cheque="C0019",
                                    fechad=FUTURO, fechad_original=AYER)
        ids["mov_protesto"] = _mov(
            cur, tipo="cheque_devuelto", id_cheque=ids["protestado"],
            importe=100.0,
            metadata={"stat_prev": "B", "stat_destino": "1"},
        )
        # Un segundo juego, intacto, para las PANTALLAS de confirmación (GET):
        # el POST de más arriba deja el mov en 'reversado' y entonces el GET
        # rebota con "ya se deshizo" sin llegar al template — que es
        # justamente donde vivía el 500 de la conciliación del 13/08.
        ids["protestado_get"] = _cheque(cur, stat="1", no_cheque="C0019G",
                                        fechad=FUTURO, fechad_original=AYER)
        ids["mov_protesto_get"] = _mov(
            cur, tipo="cheque_devuelto", id_cheque=ids["protestado_get"],
            importe=100.0,
            metadata={"stat_prev": "B", "stat_destino": "1"},
        )
        # (2) anulación por error de carga: el cheque TIENE que estar en 'X'.
        ids["anulado_err"] = _cheque(cur, stat="X", no_cheque="C0020")
        ids["mov_anulacion"] = _mov(
            cur, tipo="reverso_cheque_administrativo",
            id_cheque=ids["anulado_err"], importe=100.0,
            metadata={"stat_previo": "Z", "aplicaciones_borradas": [],
                      "n_aplicaciones_reversadas": 0, "motivo": "smoke"},
        )
        # (3) cancelación por anticipo: idem 'X', y el snapshot de posdat
        #     tiene que EXISTIR (aunque sea vacío) para que no rebote por
        #     "es anterior al 29/07".
        ids["cancelado_ant"] = _cheque(cur, stat="X", no_cheque="C0021")
        ids["mov_canc_anticipo"] = _mov(
            cur, tipo="cheque_cancelado_por_anticipo",
            id_cheque=ids["cancelado_ant"], importe=100.0,
            metadata={"stat_previo": "Z", "posdat_borradas": [],
                      "id_cheque_anticipo": ids["anticipo"]},
        )

        # ── Una entrada de caja suelta, para el alta `desde_caja` ────────
        # La dueña carga el efectivo a mano en /caja y después la cobranza lo
        # ADOPTA en vez de insertar otra fila (03/08). Sin la fila, esa rama
        # de `crear()` no se recorre.
        cur.execute(
            "INSERT INTO scintela.caja (fecha, tipo, importe, concepto, "
            "  usuario_crea) VALUES (%s,'E',250.0,%s,'smoke') "
            "RETURNING id_caja",
            (HOY, f"CH.{CLI}"),
        )
        ids["caja_suelta"] = int(cur.fetchone()["id_caja"])

        # Un segundo cheque aplicado, intacto, para el GET de "confirmar
        # desaplicación" (el del barrido POST se desaplica y deja de tener
        # fila en chequesxfact).
        ids["aplicado_get"] = _cheque(cur, stat="Z", no_cheque="C0018G",
                                      importe=500.0)
        ids["factura_aplicada_get"] = _factura(
            cur, numf=9005, importe=500.0, abono=500.0, stat="T")
        cur.execute(
            "INSERT INTO scintela.chequesxfact "
            "  (id_cheque, id_fact, fechaing, codigo_cli, importe, no_banco, "
            "   tipo, stat_f, abono_f, saldo_f, usuario_crea) "
            "VALUES (%s,%s,%s,%s,500.0,10,'CH','T',500.0,0.0,'smoke')",
            (ids["aplicado_get"], ids["factura_aplicada_get"], HOY, CLI),
        )
    conn.commit()
    return ids


# ══════════════════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def escenario():
    """La app real contra `pc_cheques`, ya sembrada. Uno solo por módulo.

    `create_app()` cuesta ~0,1 s y el 13/08 medimos que llamarlo 433 veces era
    el 40% de la suite: acá se llama UNA.
    """
    with app_smoke(DB) as app:
        conn = conexion(DB)
        try:
            ids = _sembrar(conn)
        finally:
            conn.close()
        yield app, ids


@pytest.fixture()
def cliente(escenario):
    app, _ = escenario
    return app.test_client()


@pytest.fixture()
def ids(escenario):
    return escenario[1]


def _nuevo_cheque(stat: str = "Z", **kw) -> int:
    """Un cheque fresco, ya commiteado, para una prueba que lo consume.

    Las transiciones se PISAN entre ellas (un cheque que pasó a 'X' ya no
    puede pasar a 'B'), así que cada una necesita el suyo. Un savepoint no
    serviría: la ruta commitea por su cuenta.
    """
    conn = conexion(DB)
    try:
        with conn.cursor() as cur:
            id_cheque = _cheque(cur, stat=stat, **kw)
        conn.commit()
    finally:
        conn.close()
    return id_cheque


# ══════════════════════════════════════════════════════════════════════════
# LA TRAMPA — el 302 que esconde un crash
# ══════════════════════════════════════════════════════════════════════════
# Casi todas las vistas de cheques terminan en
#
#     except ValueError as e:  flash(str(e), "warn")     ← rechazo del negocio
#     except Exception as e:   flash_exc("No pude …", e) ← CRASH
#     return redirect(...)
#
# Las dos salidas contestan 302. O sea: un smoke que sólo mira el status code
# da VERDE con la vista reventando adentro — mide que la ruta existe, no que el
# botón haga algo. Es la misma trampa que el "0 filas" de la consola SQL.
#
# Así que espiamos `humanize()` —vía `CazaTragadas` de `_smoke_base`—: es el
# único camino por el que una excepción se vuelve texto para el usuario, y lo
# usan tanto `flash_exc` como las vistas que tragan devolviendo un 400. Si una
# ruta lo llama, hubo una excepción NO prevista y el resultado se marca roto
# con su traceback, aunque haya contestado 302 o 400.
_EXCEPCIONES_TRAGADAS: list[str] = []

#: Los flashes de la última request. Un "warn" quiere decir que el negocio
#: RECHAZÓ la operación: no es un bug, pero tampoco se midió lo que se quería
#: medir — con `SMOKE_DUMP=1` sale en pantalla para poder corregir el payload.
_FLASHES: list[tuple] = []

#: La `CazaTragadas` viva de la request en curso (una sola, la del fixture).
_CAZA: list = []


@pytest.fixture(autouse=True)
def trampa_de_excepciones(monkeypatch, escenario):
    """Convierte en hallazgo toda excepción que la vista se trague."""
    from modules.cheques import views as vistas

    flash_original = vistas.flash

    def espia_flash(mensaje, category="message"):
        _FLASHES.append((category, str(mensaje)))
        return flash_original(mensaje, category)

    monkeypatch.setattr(vistas, "flash", espia_flash)
    _EXCEPCIONES_TRAGADAS.clear()
    _FLASHES.clear()

    with CazaTragadas(vistas) as caza:
        # La caza acumula en su propia lista; la volcamos en la de este archivo
        # después de cada request, que es donde `_marcar` la busca.
        _CAZA.append(caza)
        try:
            yield
        finally:
            _CAZA.clear()


def _marcar(r):
    """Le pega al Resultado la excepción tragada y el rechazo del negocio."""
    if _CAZA:
        _EXCEPCIONES_TRAGADAS.extend(x for x in [_CAZA[0].tomar()] if x)
    if _EXCEPCIONES_TRAGADAS:
        r.error = "".join(_EXCEPCIONES_TRAGADAS)
        _EXCEPCIONES_TRAGADAS.clear()
    rechazos = [m for cat, m in _FLASHES if cat in ("warn", "warning", "error")]
    if rechazos:
        r.nota = (r.nota + " · RECHAZO: " + rechazos[0][:120]).strip(" ·")
    _FLASHES.clear()
    return r


def postear(client, url, data=None, *, json=None, nota=""):
    """`_smoke_base.postear` + la trampa. Misma firma, a propósito."""
    _EXCEPCIONES_TRAGADAS.clear()
    _FLASHES.clear()
    return _marcar(_postear_crudo(client, url, data, json=json, nota=nota))


def getear(client, url, *, nota=""):
    """Idem para GET: las pantallas de confirmación también tragan."""
    _EXCEPCIONES_TRAGADAS.clear()
    _FLASHES.clear()
    return _marcar(_getear_crudo(client, url, nota=nota))


def _cerrar(b: Barrido) -> None:
    """Afirma el barrido entero de una, y deja ver TODO con `SMOKE_DUMP=1`.

    El reporte de `Barrido` sólo lista lo roto — que es lo que importa cuando
    falla. Pero para saber si un smoke MIDE algo (y no está contestando 302 de
    login a todo) hace falta ver también lo que pasó: `SMOKE_DUMP=1` lo imprime
    sin cambiar el criterio de falla.
    """
    if os.environ.get("SMOKE_DUMP") == "1":  # pragma: no cover - diagnóstico
        print(f"\n── {b.modulo}: {len(b.resultados)} rutas ──")
        for r in b.resultados:
            print(f"   {r}")
    assert not b.rotos, b.reporte()


# ══════════════════════════════════════════════════════════════════════════
# 1) EL BARRIDO — las 19 rutas POST y las 16 GET con parámetro
# ══════════════════════════════════════════════════════════════════════════

def test_barrido_post_cheques(cliente, ids):
    """Las 19 rutas POST del inventario, con un payload plausible cada una."""
    b = Barrido("cheques (POST)")
    c = cliente

    # ── Depósitos ───────────────────────────────────────────────────────
    b.agregar(postear(
        c, "/cheques/depositar-lote",
        data={"id_cheque": str(_nuevo_cheque("Z", no_cheque="D001")),
              "no_banco": "10", "fecha_deposito": HOY.isoformat(),
              "concepto": "smoke lote"},
        nota="lote de 1 cheque de cartera a Pichincha",
    ))
    b.agregar(postear(
        c, "/cheques/_api/depositar-lote",
        json={"ids": [_nuevo_cheque("Z", no_cheque="D002")],
              "no_banco": 10, "fecha": HOY.isoformat()},
        nota="misma operación por JSON (el botón inline de /cheques)",
    ))
    b.agregar(postear(
        c, "/cheques/_api/depositar-lote", json={"ids": []},
        nota="sin ids: tiene que rebotar 400, no romperse",
    ))

    # ── Endoso a proveedor ──────────────────────────────────────────────
    id_endosable = _nuevo_cheque("Z", no_cheque="E001", importe=400.0)
    b.agregar(postear(
        c, f"/cheques/{id_endosable}/endosar",
        data={"codigo_prov": PROV, "concepto": "endoso smoke",
              "tipo_compra": "C", "fecha": HOY.isoformat()},
        nota="endosar: crea la compra hermana (nunca lo nombró un test)",
    ))
    b.agregar(postear(
        c, f"/cheques/{id_endosable}/reversar-endoso",
        data={"motivo": "smoke: me confundí de proveedor"},
        nota="y el reverso del endoso, sobre el mismo cheque",
    ))
    b.agregar(postear(
        c, f"/cheques/{ids['depositado']}/endosar",
        data={"codigo_prov": PROV, "concepto": "x", "tipo_compra": "C"},
        nota="un depositado NO es endosable: 302 con flash",
    ))

    # ── Edición de la ficha ─────────────────────────────────────────────
    b.agregar(postear(
        c, f"/cheques/{ids['cartera']}/actualizar",
        data={"concepto": "abono a facturas", "doc_banco": "DEP-777",
              "fechad": FUTURO.isoformat(), "importe": "150,50",
              "no_banco": "10", "no_cheque": "C0001B",
              "nota_usuario": "revisar con Daniela",
              "observacion": "smoke", "next": "/cheques"},
        nota="actualizar TODOS los campos editables de una",
    ))
    b.agregar(postear(
        c, f"/cheques/{ids['cobrado']}/actualizar",
        data={"importe": "1,00"},
        nota="un cheque terminal (T) no se edita",
    ))

    # ── Postergar / deshacer depósito ───────────────────────────────────
    b.agregar(postear(
        c, f"/cheques/{_nuevo_cheque('Z', no_cheque='P001')}/postergar",
        data={"motivo": "el cliente pidió esperar",
              "nueva_fechad": FUTURO.isoformat(), "next": "/cheques"},
        nota="postergar desde cartera",
    ))
    b.agregar(postear(
        c, f"/cheques/{_nuevo_cheque('B', no_cheque='DD01')}/deshacer-deposito",
        data={"motivo": "al final no se depositó", "next": "/cheques"},
        nota="deshacer depósito → vuelve a cartera (Z)",
    ))
    b.agregar(postear(
        c,
        f"/cheques/{_nuevo_cheque('B', no_cheque='DD02')}"
        f"/deshacer-deposito?destino=P",
        data={"motivo": "se posterga", "nueva_fechad": FUTURO.isoformat()},
        nota="deshacer depósito con destino=P (pide la fecha nueva)",
    ))

    # ── Las tres pantallas de "deshacer" que cuelgan de un mov_doble ────
    b.agregar(postear(
        c, f"/cheques/protesto/{ids['mov_protesto']}/deshacer",
        data={"motivo": "protesté por confusión", "next": "/cheques"},
        nota="deshacer protesto (13/08: era un camino de ida)",
    ))
    b.agregar(postear(
        c, f"/cheques/anulacion-error-carga/{ids['mov_anulacion']}/deshacer",
        data={"motivo": "no era error", "next": "/cheques"},
        nota="revivir un cheque anulado por error de carga",
    ))
    b.agregar(postear(
        c, f"/cheques/cancelacion-anticipo/{ids['mov_canc_anticipo']}/reversar",
        data={"motivo": "el anticipo era de otro cheque", "next": "/cheques"},
        nota="devolver a cartera un cheque cancelado con anticipo",
    ))

    # ── Reemplazo, reverso, anulación, desaplicación ────────────────────
    b.agregar(postear(
        c, f"/cheques/{_nuevo_cheque('Z', no_cheque='R001')}/reemplazar",
        data={"motivo": "el cliente trajo otro", "nuevo_importe": "250,00",
              "nuevo_no_cheque": "R001BIS"},
        nota="cheque XX reemplazo (BANCOS.PRG:266-305)",
    ))
    b.agregar(postear(
        c, f"/cheques/{_nuevo_cheque('B', no_cheque='RV01')}/reversar",
        data={"motivo": "rebotó", "next": "/cheques"},
        nota="reversar un depositado = rebote real (emite la ND)",
    ))
    b.agregar(postear(
        c, f"/cheques/{_nuevo_cheque('Z', no_cheque='AN01')}/anular-error-carga",
        data={"motivo": "importe mal tipeado", "id_reemplazo": ""},
        nota="anular por error de carga, sin reemplazo",
    ))
    b.agregar(postear(
        c, f"/cheques/{ids['aplicado']}/desaplicar/{ids['factura_aplicada']}",
        data={"motivo": "se aplicó a la factura equivocada"},
        nota="desaplicar UNA factura (deja el cheque vivo)",
    ))

    # ── JSON / masivos ──────────────────────────────────────────────────
    b.agregar(postear(
        c, "/cheques/corregir-devoluciones-sin-nd", json={},
        nota="corrección masiva de devueltos sin ND (idempotente)",
    ))
    b.agregar(postear(
        c, "/cheques/residuos-retencion/eliminar",
        data={"id": [str(ids["residuo"]), str(ids["espejo"])], "tope": "5"},
        nota="anular residuos: el de centavos entra, el de $3.000 se ignora",
    ))
    b.agregar(postear(
        c, "/cheques/residuos-retencion/eliminar", data={"tope": "5"},
        nota="sin tildar ninguno: 302 con flash, no 500",
    ))
    # Los headers son los LABELS de `CHEQUES_CSV_COLS`, no los nombres de
    # campo: `procesar_csv` mapea por el texto de la plantilla que baja la
    # pantalla. Con los nombres internos la fila entra vacía y el upload
    # contesta "0 ok, 1 con error" — verde por fuera, sin cargar nada.
    csv = (
        "Fecha,Código cliente,N° cheque,Importe,N° banco,Banco,"
        "Fecha depósito,Estado,Proveedor,Clave\n"
        f"{HOY.isoformat()},{CLI},CSV001,75.00,10,PICHINCHA,"
        f"{HOY.isoformat()},Z,,\n"
    )
    b.agregar(postear(
        c, "/cheques/cargar-csv",
        data={"archivo": (io.BytesIO(csv.encode("utf-8")), "cheques.csv")},
        nota="carga por CSV con una fila válida",
    ))
    b.agregar(postear(
        c, "/cheques/cargar-csv", data={},
        nota="CSV sin archivo: 302 con flash",
    ))

    _cerrar(b)


def test_barrido_get_con_parametro(cliente, ids):
    """Las 16 GET con parámetro que el smoke viejo saltea por necesitar un id.

    Son las pantallas de confirmación: el 500 de la conciliación del 13/08 era
    justamente una clave faltante en el dict que iba a un template así.
    """
    b = Barrido("cheques (GET con parámetro)")
    c = cliente
    urls = [
        (f"/cheques/{ids['cartera']}", "ficha del cheque"),
        (f"/cheques/{ids['cartera']}/endosar", "wizard de endoso"),
        (f"/cheques/{ids['cartera']}/postergar", "wizard de postergación"),
        (f"/cheques/{ids['cartera']}/reemplazar", "wizard de reemplazo"),
        (f"/cheques/{ids['cartera']}/anular-error-carga", "wizard de anulación"),
        (f"/cheques/{ids['cartera']}/confirmar-reverso", "confirmar reverso"),
        (f"/cheques/{ids['cartera']}/reverso-preview", "JSON del modal"),
        (f"/cheques/{ids['depositado']}/deshacer-deposito", "destino Z"),
        (f"/cheques/{ids['depositado']}/deshacer-deposito?destino=P",
         "destino P: agrega el campo de fecha"),
        (f"/cheques/{ids['endosado']}/confirmar-reverso-endoso",
         "confirmar reverso de endoso"),
        (f"/cheques/{ids['aplicado_get']}/desaplicar/"
         f"{ids['factura_aplicada_get']}", "confirmar desaplicación"),
        (f"/cheques/protesto/{ids['mov_protesto_get']}/deshacer",
         "pantalla del protesto"),
        (f"/cheques/anulacion-error-carga/{ids['mov_anulacion']}/deshacer",
         "pantalla de la anulación"),
        (f"/cheques/cancelacion-anticipo/{ids['mov_canc_anticipo']}/reversar",
         "pantalla de la cancelación por anticipo"),
        (f"/cheques/_api/facturas-pendientes/{CLI}", "API de facturas"),
        (f"/cheques/_api/anticipos-vivos/{CLI}", "API de anticipos"),
        (f"/cheques/_api/cheques-vivos/{CLI}", "API de cheques vivos"),
        # Un cliente que no existe: la API tiene que contestar vacío, no 500.
        ("/cheques/_api/facturas-pendientes/ZZZ", "cliente inexistente"),
        # La boleta imprimible: es a donde REDIRIGE el depósito en lote, así
        # que en la práctica es parte de ese flujo aunque el barrido no siga
        # los redirects.
        (f"/cheques/boleta?fecha={HOY.isoformat()}&no_banco=10",
         "boleta de depósito (BOLEPICH del legacy)"),
        # Los buckets del listado: cada uno arma su propio WHERE y su propio
        # total (el total es del UNIVERSO filtrado, no de la página).
        ("/cheques?estado=cartera", "bucket cartera"),
        ("/cheques?estado=postergados", "bucket postergados"),
        ("/cheques?estado=devueltos", "bucket devueltos"),
        ("/cheques?estado=depositados", "bucket depositados"),
        ("/cheques?estado=anticipos", "bucket anticipos"),
        (f"/cheques?q={CLI}&estado=todos", "buscador por palabras"),
    ]
    for url, nota in urls:
        b.agregar(getear(c, url, nota=nota))
    _cerrar(b)


# ══════════════════════════════════════════════════════════════════════════
# 2) /cheques/nuevo — 1.411 líneas en una sola ruta
# ══════════════════════════════════════════════════════════════════════════

def _base_form(**extra) -> dict:
    """El esqueleto del form de cobranza: cabecera + un bloque de cheque."""
    data = {
        "fecha_recibido": HOY.isoformat(),
        "codigo_cli": CLI,
        "no_cheque[]": "N0001",
        "importe[]": "100,00",
        "fechad[]": HOY.isoformat(),
        "stat[]": "Z",
        "no_banco[]": "10",
        "doc_banco[]": "",
        "concepto[]": "abono",
        "paso": "ejecutar",
    }
    data.update(extra)
    return data


def test_nuevo_variantes(cliente, ids):
    """El wizard de alta, paso por paso y variante por variante.

    Acá está la mayor parte de la cobertura del módulo: un solo `def nuevo()`
    con el multi-cheque, el anticipo (97), el CANCELA ANTICIPO (95), el
    sobrante (anticipo vs. nota de crédito en la misma factura), la diferencia
    aprobada (flete/retención), el alta desde caja y la vuelta atrás sin
    perder lo tipeado.
    """
    b = Barrido("/cheques/nuevo")
    c = cliente

    # ── El formulario ───────────────────────────────────────────────────
    b.agregar(getear(c, "/cheques/nuevo", nota="GET del form vacío"))
    b.agregar(getear(
        c, f"/cheques/nuevo?codigo_cli={CLI}&importe=100&es_anticipo=1",
        nota="GET con restore por query string (vuelta de /clientes/nuevo)",
    ))
    b.agregar(getear(c, "/cobranza", nota="alias /cobranza"))

    # ── paso=editar: la vuelta atrás que NO borra lo cargado ────────────
    b.agregar(postear(
        c, "/cheques/nuevo",
        data=_base_form(paso="editar", **{
            "no_cheque[]": "N0001", "importe[]": "100,00"}),
        nota="paso=editar: re-renderiza sin crear (dueña 27/05)",
    ))

    # ── Paso 1: la pantalla de confirmación ─────────────────────────────
    b.agregar(postear(
        c, "/cheques/nuevo",
        data=_base_form(paso="", no_cheque=["N0002"], **{
            "no_cheque[]": "N0002", f"aplicar[{ids['factura']}]": "100,00"}),
        nota="paso 1: resumen del impacto en las facturas",
    ))

    # ── Alta simple ─────────────────────────────────────────────────────
    b.agregar(postear(
        c, "/cheques/nuevo", data=_base_form(**{"no_cheque[]": "N0010"}),
        nota="cheque simple a cartera",
    ))
    b.agregar(postear(
        c, "/cheques/nuevo",
        data=_base_form(**{"no_cheque[]": "N0011", "stat[]": "P",
                           "fechad[]": FUTURO.isoformat()}),
        nota="postdatado (stat P + fecha de depósito)",
    ))
    b.agregar(postear(
        c, "/cheques/nuevo",
        data=_base_form(**{"no_cheque[]": "N0012", "stat[]": "B",
                           "no_banco[]": "90"}),
        nota="depósito directo (banco 90): nace fuera de cartera",
    ))

    # ── Multi-cheque ────────────────────────────────────────────────────
    b.agregar(postear(
        c, "/cheques/nuevo",
        data={
            "fecha_recibido": HOY.isoformat(), "codigo_cli": CLI,
            "no_cheque[]": ["M0001", "M0002", "M0003"],
            "importe[]": ["100,00", "200,00", "300,00"],
            "fechad[]": [HOY.isoformat(), HOY.isoformat(), FUTURO.isoformat()],
            "stat[]": ["Z", "Z", "P"],
            "no_banco[]": ["10", "20", "10"],
            "doc_banco[]": ["", "DOC-2", ""],
            "concepto[]": ["", "", ""],
            "paso": "ejecutar",
        },
        nota="3 cheques en un solo guardado (batch atómico)",
    ))
    b.agregar(postear(
        c, "/cheques/nuevo",
        data={
            "fecha_recibido": HOY.isoformat(), "codigo_cli": CLI,
            "no_cheque[]": ["M0010", "M0010"],
            "importe[]": ["50,00", "50,00"],
            "no_banco[]": ["10", "10"], "paso": "ejecutar",
        },
        nota="dos bloques con el MISMO N°: 400 con el error del duplicado",
    ))

    # ── Efectivo y alta desde caja ──────────────────────────────────────
    b.agregar(postear(
        c, "/cheques/nuevo",
        data=_base_form(**{"no_cheque[]": "", "no_banco[]": "99",
                           "importe[]": "250,00"},
                        desde_caja=str(ids["caja_suelta"])),
        nota="efectivo desde /caja: ADOPTA la entrada suelta, no crea otra",
    ))

    # ── Anticipos (banco 97 + espejo NB=98) ─────────────────────────────
    b.agregar(postear(
        c, "/cheques/nuevo",
        data=_base_form(**{"no_cheque[]": "A0001", "no_banco[]": "97",
                           "importe[]": "500,00", "medio_anticipo[]": "99"},
                        es_anticipo="1"),
        nota="anticipo por efectivo: crea el espejo negativo",
    ))
    b.agregar(postear(
        c, "/cheques/nuevo",
        data=_base_form(**{"no_cheque[]": "A0002", "no_banco[]": "97",
                           "importe[]": "300,00", "medio_anticipo[]": "10",
                           f"aplicar[{ids['factura']}]": "120,00"},
                        es_anticipo="1"),
        nota="anticipo aplicado PARCIALMENTE: espejo por el resto (caso CLR)",
    ))
    # Anticipo que cancela cheques en cartera (dueña 06/07).
    id_a_cancelar = _nuevo_cheque("Z", no_cheque="CC01", importe=200.0)
    b.agregar(postear(
        c, "/cheques/nuevo",
        data=_base_form(**{"no_cheque[]": "A0003", "no_banco[]": "97",
                           "importe[]": "500,00", "medio_anticipo[]": "99",
                           "cheques_cancelar[]": str(id_a_cancelar)},
                        es_anticipo="1", sobrante_anticipo_modo="nc"),
        nota="anticipo que cancela un cheque en cartera, sobrante a NC",
    ))
    b.agregar(postear(
        c, "/cheques/nuevo",
        data=_base_form(**{"no_cheque[]": "A0004", "no_banco[]": "97",
                           "importe[]": "500,00", "medio_anticipo[]": "99",
                           "cheques_cancelar[]":
                               str(_nuevo_cheque("Z", no_cheque="CC02",
                                                 importe=200.0))},
                        es_anticipo="1", sobrante_anticipo_modo="totalizar"),
        nota="idem pero el sobrante se olvida (totalizar)",
    ))
    b.agregar(postear(
        c, "/cheques/nuevo",
        data=_base_form(**{"no_cheque[]": "A0005", "no_banco[]": "10",
                           "cheques_cancelar[]": str(ids["cartera"])}),
        nota="tildar cheques a cancelar SIN anticipo: 400 explicando",
    ))

    # ── CANCELA ANTICIPO (95) ───────────────────────────────────────────
    b.agregar(postear(
        c, "/cheques/nuevo",
        data=_base_form(**{"no_cheque[]": "", "no_banco[]": "95",
                           "importe[]": "80,00",
                           f"aplicar[{ids['factura_chica']}]": "80,00"}),
        nota="95: usa el saldo a favor para cerrar una factura",
    ))
    b.agregar(postear(
        c, "/cheques/nuevo",
        data=_base_form(**{"no_cheque[]": "", "no_banco[]": "95",
                           "importe[]": "80,00"}),
        nota="95 SIN factura: la guarda del 30/07 lo rechaza (400)",
    ))
    b.agregar(postear(
        c, "/cheques/nuevo",
        data=_base_form(**{"no_cheque[]": "", "no_banco[]": "95",
                           "importe[]": ""}),
        nota="95 sin importe: el error lista los anticipos vivos",
    ))

    # ── Sobrante y diferencia ───────────────────────────────────────────
    b.agregar(postear(
        c, "/cheques/nuevo",
        data=_base_form(**{"no_cheque[]": "S0001", "importe[]": "300,00",
                           f"aplicar[{ids['factura_chica']}]": "120,00"},
                        sobrante_modo="anticipo", confirmar_anticipo="1"),
        nota="sobrante → anticipo (futuras facturas)",
    ))
    b.agregar(postear(
        c, "/cheques/nuevo",
        data=_base_form(**{"no_cheque[]": "S0002", "importe[]": "300,00",
                           f"aplicar[{ids['factura']}]": "120,00",
                           f"aplicar_manual[{ids['factura']}]": "1"},
                        sobrante_modo="factura",
                        sobrante_a_factura=str(ids["factura"])),
        nota="sobrante → saldo a favor EN la misma factura (nota de crédito)",
    ))
    b.agregar(postear(
        c, "/cheques/nuevo",
        data=_base_form(**{"no_cheque[]": "S0003", "importe[]": "100,00",
                           f"aplicar[{ids['factura']}]": "400,00"},
                        aprobar_diferencia="1",
                        motivo_diferencia="flete + retención",
                        aplicar_t_used="1",
                        **{f"stat_final[{ids['factura']}]": "T"}),
        nota="cheque corto con la diferencia APROBADA (flete/retención)",
    ))
    b.agregar(postear(
        c, "/cheques/nuevo",
        data=_base_form(**{"no_cheque[]": "S0004", "importe[]": "100,00",
                           f"aplicar[{ids['factura']}]": "400,00"}),
        nota="cheque corto SIN aprobar: 400 y guía a la NC del cliente",
    ))
    b.agregar(postear(
        c, "/cheques/nuevo",
        data=_base_form(**{"no_cheque[]": "S0005", "importe[]": "-50,00",
                           f"aplicar[{ids['factura_nc']}]": "-50,00",
                           f"aplicar_manual[{ids['factura_nc']}]": "1"}),
        nota="cobranza NEGATIVA (devolución) sobre una nota de crédito",
    ))

    # ── Validaciones ────────────────────────────────────────────────────
    b.agregar(postear(
        c, "/cheques/nuevo", data=_base_form(codigo_cli=""),
        nota="sin cliente: 400",
    ))
    b.agregar(postear(
        c, "/cheques/nuevo", data=_base_form(**{"importe[]": "0"}),
        nota="importe cero: 400 (el negativo SÍ se permite)",
    ))
    b.agregar(postear(
        c, "/cheques/nuevo",
        data=_base_form(**{"stat[]": "P", "fechad[]": ""}),
        nota="postdatado sin fecha de depósito: 400",
    ))
    b.agregar(postear(
        c, "/cheques/nuevo",
        data=_base_form(**{"no_banco[]": ""}, banco_texto=""),
        nota="sin banco ni texto de banco: 400",
    ))
    b.agregar(postear(
        c, "/cheques/nuevo", data=_base_form(codigo_cli="ZZZ"),
        nota="cliente inexistente: redirige a crearlo (302)",
    ))
    b.agregar(postear(
        c, "/cheques/nuevo",
        data=_base_form(**{"no_cheque[]": "N0090", "no_banco[]": ""},
                        banco_texto="BANCO ESCRITO A MANO", prov=PROV),
        nota="banco tipeado a mano en vez de elegido",
    ))

    _cerrar(b)


# ══════════════════════════════════════════════════════════════════════════
# 3) /cheques/<id>/transicionar — la máquina de estados, según ella misma
# ══════════════════════════════════════════════════════════════════════════

#: Wizards cuya PANTALLA y cuyo BOTÓN viven en rutas distintas.
#: `confirmar_reverso` es GET-only: su `accion_url` apunta a `cheques.reversar`.
#: Postearle a la pantalla da 405 — un código que no es 500 y que por eso
#: pasaría el smoke sin haber ejecutado nada.
_ACCION_DEL_WIZARD = {"cheques.confirmar_reverso": "cheques.reversar"}


def test_transiciones_declaradas_por_la_app(escenario, cliente):
    """Cada transición que la app OFRECE tiene que poder ejecutarse.

    No inventamos destinos: recorremos `transiciones_para(stat)`, que es lo que
    dibuja el dropdown, y **cada opción se aprieta por el endpoint que ella
    misma declara**. Postearlas todas a `transicionar` daría un rechazo
    ordenado ("Transición B→P no permitida") y el smoke pasaría igual: pero la
    opción "Volver a postergado" NO es un cambio de etiqueta, es el wizard
    `deshacer_deposito`, que además revierte el movimiento del banco. Medir el
    rechazo en vez de la acción es exactamente el test dormido.

    `url_for` con el endpoint declarado es, de paso, el chequeo de que el link
    del menú existe: un link puede estar ROTO sin dar 404.

    Cada par (origen → destino) va sobre un cheque PROPIO: las transiciones se
    pisan entre ellas y reusar el cheque probaría el rechazo, no la transición.
    """
    from flask import url_for

    from modules.cheques.estados import ESTADOS
    from modules.cheques.queries import transiciones_para

    app, _ = escenario
    b = Barrido("cheques (transiciones que declara la app)")
    c = cliente
    n = 0
    #: Lo que cada pantalla necesita. Se manda el superset: cada vista lee lo
    #: suyo e ignora el resto, y así el bucle no tiene un `if` por endpoint.
    for stat in ESTADOS:
        for t in transiciones_para(stat):
            destino = t["stat_destino"]
            n += 1
            id_cheque = _nuevo_cheque(
                stat, no_cheque=f"T{n:03d}",
                no_banco=20 if stat == "I" else 10,
            )
            with app.test_request_context():
                url = url_for(t["endpoint"], id_cheque=id_cheque,
                              **(t.get("url_args") or {}))
            nota = (f"{stat} → {destino} · {t.get('label') or ''} "
                    f"[{t['endpoint']}]")
            payload = {
                "stat_destino": destino,
                "motivo": "smoke",
                "fecha": HOY.isoformat(),
                "fecha_postergada": FUTURO.isoformat(),
                "nueva_fechad": FUTURO.isoformat(),
                "next": "/cheques",
            }
            # Los WIZARD tienen pantalla de confirmación: se MIRA la pantalla
            # (es la clase de template que rompió el 13/08) y después se
            # aprieta el botón que esa pantalla dibuja.
            url_accion = url
            if t.get("kind") == "WIZARD":
                b.agregar(getear(c, url, nota=nota + " (pantalla)"))
                destino_accion = _ACCION_DEL_WIZARD.get(t["endpoint"])
                if destino_accion:
                    with app.test_request_context():
                        url_accion = url_for(destino_accion,
                                             id_cheque=id_cheque)
            b.agregar(postear(c, url_accion, data=payload, nota=nota))
    assert n > 0, "transiciones_para no devolvió NINGUNA transición"
    _cerrar(b)


def test_transicionar_destino_invalido(cliente):
    """Un destino que la máquina no permite se rechaza CON flash, sin 500.

    El backend valida por su cuenta porque `transicionar` no exige permiso
    (dueña 11/07: "all users can do it"): el freno tiene que estar acá.
    """
    b = Barrido("cheques (transiciones ilegales)")
    c = cliente
    for stat, destino, por_que in [
        ("B", "P", "salir de un depositado sin tocar el banco"),
        ("X", "B", "revivir un eliminado directo a depositado"),
        ("C", "Z", "sacar plata de caja por un cambio de etiqueta"),
        ("Z", "Q", "una letra que no existe en la tabla de estados"),
        ("Z", "", "sin destino"),
    ]:
        id_cheque = _nuevo_cheque(stat, no_cheque=f"IL{stat}{destino or 'x'}")
        b.agregar(postear(
            c, f"/cheques/{id_cheque}/transicionar",
            data={"stat_destino": destino, "motivo": "smoke"},
            nota=f"{stat} → {destino or '(vacío)'}: {por_que}",
        ))
    _cerrar(b)


def test_rutas_con_id_inexistente(cliente):
    """Un id que no existe da 404 o un rebote controlado, nunca un 500.

    Es el caso que más se ve en producción: alguien deja la pantalla abierta,
    el registro se borra y el botón se aprieta igual.
    """
    b = Barrido("cheques (ids inexistentes)")
    c = cliente
    inexistente = 987654321
    for url, metodo in [
        (f"/cheques/{inexistente}", "GET"),
        (f"/cheques/{inexistente}/endosar", "GET"),
        (f"/cheques/{inexistente}/reemplazar", "GET"),
        (f"/cheques/{inexistente}/transicionar", "POST"),
        (f"/cheques/{inexistente}/reversar", "POST"),
        (f"/cheques/{inexistente}/actualizar", "POST"),
        (f"/cheques/{inexistente}/postergar", "POST"),
        (f"/cheques/{inexistente}/reversar-endoso", "POST"),
        (f"/cheques/{inexistente}/desaplicar/{inexistente}", "POST"),
        (f"/cheques/protesto/{inexistente}/deshacer", "POST"),
        (f"/cheques/anulacion-error-carga/{inexistente}/deshacer", "POST"),
        (f"/cheques/cancelacion-anticipo/{inexistente}/reversar", "POST"),
    ]:
        if metodo == "GET":
            b.agregar(getear(c, url, nota="id inexistente"))
        else:
            b.agregar(postear(c, url, data={"motivo": "smoke",
                                            "stat_destino": "P"},
                              nota="id inexistente"))
    _cerrar(b)
