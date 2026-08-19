"""El lapicito de la columna Retención en /facturas: cargar UNA a mano.

Tamara 2026-08-19: *"quiero en la pantalla de facturas a retenciones poder
editar el 0 a un numero y que se guarde. las que ya vinieron de asinfo no se
editan. si que aplique de verdad"*.

⭐ Lo que hace falta probar acá no es "se guarda": es **"aplica de verdad"**.
La pantalla que ya existía (`/retenciones/emitir` → `retenciones.queries.
emitir`) *sí* guardaba — insertaba la fila en `scintela.retencion` y listo. No
llenaba `factura.retencion`, no bajaba el saldo, no recalculaba el estado y no
dejaba rastro reversible, así que el cliente quedaba debiendo de más y la base
con la divergencia que el health reporta como `falta_en_la_factura`. Un test
que sólo mirara la fila insertada habría pasado con la versión rota.
"""
from __future__ import annotations

import contextlib
from datetime import date

import pytest


class _Stub:
    """Fake de `db` acotado a lo que toca la carga manual.

    `retencion_registrada` es el SUM(rete) de `scintela.retencion` para esa
    factura: se controla aparte de `factura.retencion` a propósito, porque los
    dos faltantes NO son el mismo número (el caso AL1 177629 del 07/08).
    """

    def __init__(self, factura, retencion_registrada=0.0):
        self.factura = factura
        self.retencion_registrada = retencion_registrada
        self.updates = []
        self.inserts = []
        self.movs = []

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.factura" in s:
            return dict(self.factura)
        if "from scintela.retencion" in s:
            return {"s": self.retencion_registrada}
        return None

    def fetch_all(self, sql, params=None, conn=None):
        return []

    def execute(self, sql, params=None, conn=None):
        if "update scintela.factura" in " ".join(sql.split()).lower():
            self.updates.append(tuple(params))
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        if "insert into scintela.retencion" in " ".join(sql.split()).lower():
            self.inserts.append(tuple(params))
        return {"id_retencion": 909}

    @contextlib.contextmanager
    def tx(self):
        yield object()


def _fac(**kw):
    base = {"id_factura": 41, "codigo_cli": "ABC", "numf": 177801,
            "numf_completo": "001-099-000177801", "fecha": date(2026, 8, 14),
            "importe": 1250.0, "abono": 0.0, "retencion": 0.0,
            "saldo": 1250.0, "stat": "Z"}
    base.update(kw)
    return base


@pytest.fixture
def stub(monkeypatch):
    import db
    from modules.retenciones import queries as q
    s = _Stub(_fac())
    for a in ("fetch_one", "fetch_all", "execute", "execute_returning", "tx"):
        monkeypatch.setattr(db, a, getattr(s, a))
    monkeypatch.setattr(q, "asegurar_fecha_abierta", lambda f: None, raising=False)
    monkeypatch.setattr(q._md, "registrar",
                        lambda **k: s.movs.append(k) or {"id_mov_doble": 1})
    return s


# ── aplica DE VERDAD ───────────────────────────────────────────────────────

def test_baja_el_saldo_y_llena_la_columna_retencion(stub):
    from modules.retenciones import queries as q
    res = q.aplicar_manual(41, 12.50, usuario="tamara")
    retencion, saldo, stat = stub.updates[0][:3]
    assert retencion == 12.50, "la retención va a su columna propia (mig 0179)"
    assert saldo == 1237.50, "el saldo BAJA — esto es lo que la pantalla vieja no hacía"
    assert stat == "Z", "una retención sola no abona: Z sigue Z"
    assert res["saldo"] == 1237.50 and res["retencion"] == 12.50


def test_no_toca_el_abono(stub):
    """Desde la 0179 la retención NO vive adentro del abono."""
    from modules.retenciones import queries as q
    stub.factura = _fac(abono=200.0, saldo=1050.0, stat="A")
    q.aplicar_manual(41, 50.0, usuario="tamara")
    # El UPDATE de la retención escribe retencion/saldo/stat: el abono ni
    # aparece en la sentencia, así que no hay forma de que se lo lleve puesto.
    assert stub.updates[0][:3] == (50.0, 1000.0, "A")


def test_registra_el_comprobante_en_scintela_retencion(stub):
    from modules.retenciones import queries as q
    q.aplicar_manual(41, 12.50, usuario="tamara")
    assert stub.inserts, "sin comprobante, el cron la vería como nunca aplicada"
    codigo_cli, numf, rete, usuario = stub.inserts[0]
    assert (codigo_cli, numf, rete, usuario) == ("ABC", 177801, 12.50, "tamara")


def test_deja_el_mov_doble_del_tipo_que_el_historial_sabe_deshacer(stub):
    """El ↺ del historial cuelga del STRING del tipo: si la carga manual
    inventara uno propio, quedaría una retención que no se puede deshacer."""
    from modules.retenciones import queries as q
    q.aplicar_manual(41, 12.50, usuario="tamara")
    mv = stub.movs[0]
    assert mv["tipo"] == "retencion_asinfo_aplicada"
    assert mv["metadata"]["origen"] == "manual", "de dónde vino se lee acá"
    # El snapshot que necesita el reverso para restaurar la factura.
    for k in ("abono_previo", "retencion_previa", "saldo_previo", "stat_previo"):
        assert k in mv["metadata"], k
    assert "(carga manual)" in mv["concepto"]


def test_el_tipo_que_deja_es_uno_que_desaplicar_por_mov_acepta(stub):
    """Prueba el enganche, no la forma: se le pasa al reverso el MISMO
    movimiento que acaba de escribir la carga manual."""
    from modules.retenciones import queries as q
    q.aplicar_manual(41, 12.50, usuario="tamara")
    mv = stub.movs[0]
    stub.factura = _fac(retencion=12.50, saldo=1237.50)
    stub.fetch_one_mov = None

    class _S2(_Stub):
        def fetch_one(self, sql, params=None, conn=None):
            s = " ".join(sql.split()).lower()
            if "from scintela.mov_doble" in s:
                return {"id_mov_doble": 5, "tipo": mv["tipo"], "estado": "activo",
                        "origen_id": 41, "importe": 12.50,
                        "metadata": mv["metadata"]}
            return super().fetch_one(sql, params, conn)

    import db
    s2 = _S2(_fac(retencion=12.50, saldo=1237.50))
    for a in ("fetch_one", "fetch_all", "execute", "execute_returning", "tx"):
        setattr(db, a, getattr(s2, a))
    res = q.desaplicar_por_mov(5, usuario="tamara")
    assert res["saldo_restaurado"] == 1250.0


def test_si_la_retencion_cubre_todo_la_factura_queda_en_T(stub):
    from modules.retenciones import queries as q
    stub.factura = _fac(importe=12.50, saldo=12.50)
    q.aplicar_manual(41, 12.50, usuario="tamara")
    assert stub.updates[0][2] == "T"


# ── lo que BLOQUEA (dueña 19/08: "las que ya vinieron de asinfo no se editan") ─

def test_bloquea_si_la_factura_ya_tiene_retencion(stub):
    from modules.retenciones import queries as q
    stub.factura = _fac(retencion=21.41, saldo=1228.59)
    with pytest.raises(ValueError, match="ya tiene una retención"):
        q.aplicar_manual(41, 12.50, usuario="tamara")
    assert not stub.updates and not stub.inserts


def test_bloquea_si_hay_comprobante_aunque_la_columna_este_en_cero(stub):
    """El segundo faltante: retención de la época del dBase o `parcial`, con
    fila en `scintela.retencion` y la columna todavía en 0. Cargar otra acá
    duplicaría el comprobante — es cómo AL1 177629 terminó sumando 197,37
    cuando el cliente había retenido 125,60."""
    from modules.retenciones import queries as q
    stub.retencion_registrada = 53.83
    with pytest.raises(ValueError, match="Ya hay un comprobante"):
        q.aplicar_manual(41, 12.50, usuario="tamara")
    assert not stub.updates and not stub.inserts


def test_freno_retencion_mayor_al_saldo(stub):
    """Mismo freno que el aplicador automático (dueña 06/08): no dejar el
    saldo negativo sin que nadie lo mire."""
    from modules.retenciones import queries as q
    stub.factura = _fac(abono=1200.0, saldo=50.0, stat="A")
    with pytest.raises(ValueError, match="mayor que el saldo"):
        q.aplicar_manual(41, 80.0, usuario="tamara")
    assert not stub.updates


def test_freno_retencion_mayor_al_importe(stub):
    from modules.retenciones import queries as q
    with pytest.raises(ValueError, match="no puede superar el importe"):
        q.aplicar_manual(41, 5000.0, usuario="tamara")


@pytest.mark.parametrize("valor", [0, 0.0, -12.5, None])
def test_no_acepta_cero_ni_negativo(stub, valor):
    from modules.retenciones import queries as q
    with pytest.raises(ValueError, match="mayor que cero"):
        q.aplicar_manual(41, valor, usuario="tamara")
    assert not stub.updates


def test_no_se_le_carga_retencion_a_una_factura_anulada(stub):
    from modules.retenciones import queries as q
    stub.factura = _fac(stat="X")
    with pytest.raises(ValueError, match="anulada"):
        q.aplicar_manual(41, 12.50, usuario="tamara")


def test_respeta_el_cierre_de_periodo(monkeypatch, stub):
    from modules.retenciones import queries as q
    monkeypatch.setattr(
        q, "asegurar_fecha_abierta",
        lambda f: (_ for _ in ()).throw(ValueError("Período contable cerrado.")))
    with pytest.raises(ValueError, match="Período contable cerrado"):
        q.aplicar_manual(41, 12.50, usuario="tamara")


# ── el escritor es UNO solo ────────────────────────────────────────────────

def test_la_carga_manual_y_la_de_asinfo_usan_el_mismo_escritor():
    """Si dos lugares hacen lo mismo, comparten la función que lo hace. Con
    copias, el día que cambie la regla del saldo o la del estado una de las
    dos se queda vieja EN SILENCIO — le pasó al preview el 03/08."""
    import inspect

    from modules.retenciones import queries as q
    for fn in (q.aplicar_manual, q._aplicar_una_por_numero):
        src = inspect.getsource(fn)
        assert "_grabar_retencion(" in src, fn.__name__
        assert "UPDATE scintela.factura" not in src, (
            f"{fn.__name__} volvió a escribir el UPDATE a mano")


# ── la ruta y la pantalla ──────────────────────────────────────────────────

def test_la_ruta_existe_y_pide_permiso_de_retenciones(app):
    """Gatea por OPERACIÓN, no por pantalla: quien carga retenciones es
    Cobranzas, que NO tiene `facturas.editar`."""
    regla = next((r for r in app.url_map.iter_rules()
                  if str(r) == "/facturas/<int:id_factura>/retencion"), None)
    assert regla is not None, "la ruta del lapicito no está registrada"
    assert "POST" in regla.methods
    vista = app.view_functions[regla.endpoint]
    assert getattr(vista, "_permiso", None) == "retenciones.emitir"


def test_cobranzas_puede_cargar_retenciones_sin_poder_editar_facturas():
    from config.roles import ROLES
    permisos = dict(ROLES)["Cobranzas"] if isinstance(ROLES, list) else ROLES["Cobranzas"]
    assert "retenciones.emitir" in permisos
    assert "facturas.editar" not in permisos, (
        "si Cobranzas ganara facturas.editar, este test sobra — pero entonces "
        "hay que decidir de nuevo por qué permiso gatea el lapicito")


def test_el_lapicito_no_aparece_si_la_retencion_ya_esta_cargada():
    """La celda con retención > 0 vuelve a ser texto: para cambiarla hay que
    deshacerla desde el Historial."""
    from pathlib import Path
    tpl = Path("modules/facturas/templates/facturas/lista.html").read_text(encoding="utf-8")
    i = tpl.index('data-campo="retencion"')
    bloque = tpl[max(0, i - 1500):i]
    assert "{% if (f.retencion or 0) > 0 %}" in bloque
    assert "{% elif tiene_permiso('retenciones.emitir')" in bloque


def test_la_celda_de_retencion_postea_a_su_propia_ruta():
    """No a `/facturas/<id>/campo`: guardar acá no escribe un campo, aplica la
    retención (baja el saldo, recalcula el estado, deja el ↺)."""
    from pathlib import Path
    tpl = Path("modules/facturas/templates/facturas/lista.html").read_text(encoding="utf-8")
    assert 'data-url="/facturas/{{ f.id_factura }}/retencion"' in tpl
    assert "cell.dataset.url || `/facturas/${id}/campo`" in tpl
