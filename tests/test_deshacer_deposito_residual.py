"""BUG A — "Volver a cartera" podía borrar el depósito ENTERO.

TMT 2026-08-03. `compensar_deposito_devuelto` (rebote) saca el link del cheque
protestado del 'DE' pero **deja el importe del depósito intacto** a propósito:
el extracto muestra el depósito completo + la nota de débito del protesto, y así
concilia. Consecuencia: después de un rebote, la CANTIDAD DE LINKS deja de
describir la composición del 'DE'.

`deshacer_deposito_cheque` decidía borrar el movimiento con
`if n <= 1 or nuevo_imp <= 0.005`, usando el conteo de links como proxy de
"este cheque era el único del depósito". Con un 'DE' de 150 (3 cheques de 50)
del que dos rebotaron, quedaba 1 link y "Volver a cartera" sobre el tercero
**borraba los 150** en vez de bajarlo a 100: el banco perdía 100 reales.

Ahora se decide por IMPORTE: el mov se borra sólo si queda en ~0. Para que el
resto sea SIEMPRE plata real hace falta que el importe del 'DE' no se
desincronice de sus cheques, y `editar()` lo permitía (cambiar el importe de un
cheque ya depositado sin tocar el 'DE' inventaba un resto de la nada). Por eso
el fix viene en dos partes y los dos lados se testean acá.
"""
from __future__ import annotations

import contextlib
from datetime import date

import pytest


HOY = date.today()


class _Rec:
    """Stub de db.* para `deshacer_deposito_cheque`.

    Modela UN depósito 'DE' (#777) con `de_importe` y `n_links` links vivos, y
    un cheque de `imp_cheque` en estado depositado. Captura los execute() para
    poder afirmar si el 'DE' se borró o se actualizó.
    """

    DE_ID = 777
    NO_BANCO = 10

    def __init__(self, *, de_importe: float, n_links: int, imp_cheque: float,
                 concepto: str = "dep.3 ch.", conciliado: bool = False):
        self.de_importe = de_importe
        self.n_links = n_links
        self.imp_cheque = imp_cheque
        self.concepto = concepto
        self.conciliado = conciliado
        self.executes: list[tuple[str, tuple]] = []

    # ── lecturas ───────────────────────────────────────────────────────
    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.cheque where id_cheque" in s:
            return {
                "id_cheque": 1, "no_cheque": "99224", "codigo_cli": "CJE",
                "importe": self.imp_cheque, "stat": "B",
            }
        if "banco_conciliacion_match" in s:
            return {"1": 1} if self.conciliado else None
        if "count(*) as n from scintela.chequextransaccion" in s:
            return {"n": self.n_links}
        if "from scintela.mov_doble" in s:
            return None
        if "order by fecha asc, id_transaccion asc offset 1" in s:
            return None  # sin ancla → no recomputa saldos
        return None

    def fetch_all(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "chequextransaccion cxt" in s and "'de'" in s:
            return [{
                "id_transaccion": self.DE_ID, "no_banco": self.NO_BANCO,
                "importe": self.de_importe, "concepto": self.concepto,
            }]
        return []

    def execute(self, sql, params=None, conn=None):
        self.executes.append((" ".join(sql.split()), tuple(params or ())))
        return 1

    @contextlib.contextmanager
    def tx(self):
        yield object()

    # ── asserts de conveniencia ────────────────────────────────────────
    def borro_el_deposito(self) -> bool:
        return any(
            "delete from scintela.transacciones_bancarias" in sql.lower()
            for sql, _ in self.executes
        )

    def update_del_deposito(self) -> tuple | None:
        for sql, params in self.executes:
            if "update scintela.transacciones_bancarias" in sql.lower():
                return params
        return None


def _apply(monkeypatch, rec):
    import db
    import mov_doble

    monkeypatch.setattr(db, "fetch_one", rec.fetch_one)
    monkeypatch.setattr(db, "fetch_all", rec.fetch_all)
    monkeypatch.setattr(db, "execute", rec.execute)
    monkeypatch.setattr(db, "tx", rec.tx)
    monkeypatch.setattr(mov_doble, "registrar", lambda **kw: 1)


def _deshacer(**kw):
    from modules.cheques import queries as q
    return q.deshacer_deposito_cheque(id_cheque=1, usuario="test", **kw)


# ── EL BUG ──────────────────────────────────────────────────────────────
def test_resto_de_rebote_no_borra_el_deposito_entero(monkeypatch):
    """'DE' de 150 al que le quedó UN link (dos cheques rebotaron y se
    desagruparon). Volver a cartera el tercero (50) tiene que dejar el 'DE' en
    100 — NO borrar los 150."""
    rec = _Rec(de_importe=150.0, n_links=1, imp_cheque=50.0, concepto="dep.3 ch.")
    _apply(monkeypatch, rec)
    _deshacer()
    assert not rec.borro_el_deposito(), (
        "borró el depósito entero (150) por un cheque de 50: el banco pierde 100"
    )
    params = rec.update_del_deposito()
    assert params is not None, "tenía que bajar el importe del 'DE', no borrarlo"
    assert params[0] == 100.0, f"el 'DE' debía quedar en 100.00, quedó en {params[0]}"


def test_el_resto_sobrevive_aunque_el_concepto_sea_texto_libre(monkeypatch):
    """El concepto del lote es un campo LIBRE de la pantalla de depósito
    (`depositar_lote.html`: "Boleta de depósito (opcional)"), así que puede no
    tener el contador 'dep.N ch.'. La decisión no puede depender de él."""
    rec = _Rec(de_importe=150.0, n_links=1, imp_cheque=50.0,
               concepto="Boleta 12345 Pichincha")
    _apply(monkeypatch, rec)
    _deshacer()
    assert not rec.borro_el_deposito(), "con concepto libre volvió a borrar los 150"
    assert rec.update_del_deposito()[0] == 100.0


def test_rebotes_y_vueltas_a_cartera_intercalados(monkeypatch):
    """Lote A+B+C de 50 (DE=150). A rebota (ND −50, se desagrupa) → volver a
    cartera B → volver a cartera C. Cada paso baja 50; el 'DE' nunca se borra
    de golpe y termina en 50, que es la parte de A (ya debitada por su ND).
    Neto del libro = 0."""
    de = 150.0
    for n_links, esperado in ((2, 100.0), (1, 50.0)):
        rec = _Rec(de_importe=de, n_links=n_links, imp_cheque=50.0, concepto="dep.3 ch.")
        _apply(monkeypatch, rec)
        _deshacer()
        assert not rec.borro_el_deposito(), f"borró el 'DE' con n={n_links}"
        de = rec.update_del_deposito()[0]
        assert de == esperado, f"n={n_links}: esperaba {esperado}, quedó {de}"


# ── lo que NO se puede romper ───────────────────────────────────────────
def test_deposito_individual_se_sigue_borrando(monkeypatch):
    """Caso clásico (TMT 2026-07-07): depósito de 1 cheque que queda en 0."""
    rec = _Rec(de_importe=50.0, n_links=1, imp_cheque=50.0, concepto="dep.1 ch.")
    _apply(monkeypatch, rec)
    _deshacer()
    assert rec.borro_el_deposito(), "un depósito que queda en 0 tiene que borrarse"


def test_consolidado_baja_importe_y_contador(monkeypatch):
    """'DE' de 150 con sus 3 cheques vivos: sacar uno lo deja en 100 y
    'dep.2 ch.'."""
    rec = _Rec(de_importe=150.0, n_links=3, imp_cheque=50.0)
    _apply(monkeypatch, rec)
    _deshacer()
    assert not rec.borro_el_deposito()
    imp, conc, _id = rec.update_del_deposito()
    assert imp == 100.0
    assert "dep.2 ch." in conc


def test_deposito_conciliado_sigue_bloqueado(monkeypatch):
    """El guard de conciliación no se toca: un 'DE' conciliado no se modifica."""
    rec = _Rec(de_importe=150.0, n_links=1, imp_cheque=50.0, conciliado=True)
    _apply(monkeypatch, rec)
    with pytest.raises(ValueError, match="conciliad"):
        _deshacer()
    assert not rec.executes, "no puede escribir nada si el depósito está conciliado"


def test_deposito_que_queda_en_cero_con_cheques_colgando_aborta(monkeypatch):
    """Si sacar el cheque deja el 'DE' en 0 pero todavía quedan links, borrarlo
    dejaría cheques marcados como depositados sin depósito. Abortar, no adivinar.
    Y como el raise corre dentro de `db.tx()`, no puede quedar escrito nada."""
    rec = _Rec(de_importe=50.0, n_links=3, imp_cheque=50.0)
    _apply(monkeypatch, rec)
    with pytest.raises(ValueError, match="quedaría en cero"):
        _deshacer()
    assert not rec.borro_el_deposito()


# ── el otro lado del fix: que el resto sea SIEMPRE plata real ──────────
class _RecEditar:
    """Stub mínimo para `editar()`: un cheque vivo, con o sin depósito 'DE'."""

    def __init__(self, *, en_deposito: bool, stat: str = "B"):
        self.en_deposito = en_deposito
        self.stat = stat
        self.executes: list[str] = []

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "chequextransaccion cxt" in s and "'de'" in s:
            return {"id_transaccion": 777} if self.en_deposito else None
        if "from scintela.cheque" in s:
            return {
                "id_cheque": 1, "no_cheque": "99224", "codigo_cli": "CJE",
                "importe": 1200.0, "stat": self.stat, "fechad": HOY, "fecha": HOY,
                "concepto": "", "observacion": "", "doc_banco": "", "no_banco": 10,
            }
        return None

    def execute(self, sql, params=None, conn=None):
        self.executes.append(" ".join(sql.split()))
        return 1


def _editar(monkeypatch, rec, **kw):
    import db
    from modules.cheques import queries as q

    monkeypatch.setattr(db, "fetch_one", rec.fetch_one)
    monkeypatch.setattr(db, "execute", rec.execute)
    return q.editar(1, usuario="test", **kw)


def test_no_se_puede_editar_el_importe_de_un_cheque_depositado(monkeypatch):
    """Fuente del "resto falso": `editar()` dejaba cambiar el importe de un
    cheque que está DENTRO de un 'DE' sin tocar el depósito. Un cheque de 1.200
    corregido a 1.000 dejaba el 'DE' en 1.200 y, al volverlo a cartera, un resto
    de 200 que nadie acreditó. Ahora se bloquea y se explica el camino."""
    rec = _RecEditar(en_deposito=True, stat="B")
    with pytest.raises(ValueError, match="depósito bancario"):
        _editar(monkeypatch, rec, importe=1000.0)
    assert not any("update scintela.cheque" in s.lower() for s in rec.executes)


def test_editar_importe_sigue_andando_para_un_cheque_en_cartera(monkeypatch):
    """El guard es angosto: un cheque en cartera (Z, sin depósito) se sigue
    editando (TMT 2026-05-27 dueña: 'dejame editar valor de cheque!!')."""
    rec = _RecEditar(en_deposito=False, stat="Z")
    _editar(monkeypatch, rec, importe=1000.0)
    assert any("importe=%s" in s for s in rec.executes), "no escribió el importe"


def test_un_link_viejo_sin_estar_depositado_no_bloquea_la_edicion(monkeypatch):
    """Hay cheques que conservan el link a un 'DE' SIN estar depositados: el
    rebote de un depósito de un solo cheque no desagrupa (a propósito) y
    `anular_por_error_de_carga` tampoco borra el link. Esos no pueden ir por
    «Volver a cartera» (que exige STATS_DEPOSITADO), así que bloquearlos los
    dejaría sin ninguna salida por pantalla."""
    for stat in ("9", "1", "Z"):
        rec = _RecEditar(en_deposito=True, stat=stat)
        _editar(monkeypatch, rec, importe=1000.0)
        assert any("importe=%s" in s for s in rec.executes), (
            f"stat {stat}: quedó sin salida — no puede editar ni volver a cartera"
        )


# ── el formato del concepto es CONTRATO, no cosmética ──────────────────
def test_el_relabel_conserva_el_formato_dep_n_ch():
    """`matcher_banco._detectar_agrupado`, `hoja_queries._detectar_agrupado_simple`,
    `_extraer_cliente_concepto` y la firma de match (`_firma_expr`,
    LEFT(concepto,40)) parsean literalmente 'dep.N ch.'. Cambiar ese texto
    (p.ej. a 'dep. resto (…)') rompe la extracción del código de cliente, mueve
    el mov de bucket en la conciliación y trunca el sufijo por los chars extra.
    El contador puede llegar a 0 y el formato tiene que aguantar."""
    from modules.cheques.queries import _relabel_dep_concepto
    from modules.conciliacion.matcher_banco import (
        _detectar_agrupado,
        _extraer_cliente_concepto,
    )

    for n, agrupado in ((0, 0), (1, 0), (2, 2), (12, 12)):
        out = _relabel_dep_concepto("dep.3 ch. CJE", n)
        assert len(out) <= 50
        assert _detectar_agrupado(out) == agrupado, (
            f"n={n}: el matcher lo clasifica {_detectar_agrupado(out)} en {out!r}"
        )
        cli, _ = _extraer_cliente_concepto(out)
        assert cli == "CJE", f"n={n}: se perdió el código de cliente en {out!r}"


def test_el_relabel_no_produce_contadores_negativos():
    from modules.cheques.queries import _relabel_dep_concepto

    assert "dep.0 ch." in _relabel_dep_concepto("dep.1 ch.", -1)
