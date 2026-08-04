"""El saldo del banco se CALCULA. TMT 2026-08-04.

Dueña, después de leer el diagnóstico de por qué la cadena se seguía
partiendo: *"arregla el bug para que no pase más"*.

El bug de fondo: **el Balance publicaba un número que alguien escribió a mano
en una fila**. `saldo_bancos()` no sumaba nada — tomaba el `saldo` running
guardado de la última fila, y eso era BANCOS → patrimonio → utilidad. Como las
filas nacen en orden de carga y se leen en orden de fecha, cualquier fila
backdated obligaba a re-estampar toda la cadena; si ese re-estampado fallaba,
el error iba derecho a la plata. Falló el 12/05, el 11/06 y el 03/08.

Ahora sale de un solo dato guardado —la apertura del banco— más la suma
firmada de los movimientos. La columna `saldo` quedó como decoración.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fila(no_banco, nombre, *, stored, signed, raw, suma, derivado=0.0):
    return {"no_banco": no_banco, "nombre": nombre, "saldo_stored": stored,
            "saldo_signed": signed, "saldo_raw": raw, "suma_firmada": suma,
            "saldo_derivado": derivado, "n_transacciones": 10}


def _correr(monkeypatch, filas, aperturas):
    import db as db_mod
    from modules.bancos import apertura as ap_mod
    from modules.informes import queries as iq

    monkeypatch.setattr(db_mod, "fetch_all", lambda *a, **kw: filas)
    monkeypatch.setattr(ap_mod, "aperturas", lambda: aperturas)
    return {b["no_banco"]: b for b in iq.saldo_bancos()}


def test_el_saldo_sale_de_apertura_mas_movimientos(monkeypatch):
    """Los números reales de Pichincha del 04/08."""
    out = _correr(
        monkeypatch,
        [_fila(10, "PICHINCHA", stored=1960392.09, signed=-1001943.68,
               raw=0, suma=-1001943.68)],
        {10: 2962335.77},
    )
    assert out[10]["saldo"] == 1960392.09
    assert out[10]["saldo_origen"] == "derivado"
    assert out[10]["usa_fallback"] is False


def test_una_fila_mal_estampada_ya_no_mueve_el_patrimonio(monkeypatch):
    """⭐ EL PUNTO. El caso del 03/08, exacto.

    Aquel día el running guardado de la última fila quedó 155.187,31 arriba
    y el Balance publicó eso: la utilidad saltó de 37.658 a 193.749 sin que
    se moviera un peso. Con el saldo calculado, la misma columna rota deja
    el patrimonio EXACTAMENTE donde estaba.
    """
    sano = _correr(
        monkeypatch,
        [_fila(10, "PICHINCHA", stored=2489398.56, signed=0, raw=0,
               suma=-472937.21)],
        {10: 2962335.77},
    )
    roto = _correr(
        monkeypatch,
        [_fila(10, "PICHINCHA", stored=2489398.56 + 155187.31, signed=0,
               raw=0, suma=-472937.21)],
        {10: 2962335.77},
    )
    assert roto[10]["saldo"] == sano[10]["saldo"], (
        "la columna `saldo` volvió a mandar sobre el patrimonio"
    )


def test_sin_apertura_cae_al_comportamiento_viejo(monkeypatch):
    """Fail-soft: si el bootstrap falló, el Balance no se queda sin número."""
    out = _correr(
        monkeypatch,
        [_fila(10, "PICHINCHA", stored=1960392.09, signed=0, raw=0, suma=0)],
        {},
    )
    assert out[10]["saldo"] == 1960392.09
    assert out[10]["saldo_origen"] == "stored"


def test_la_siembra_deja_el_numero_publicado_intacto():
    """🌱 El día que esto entra, ningún saldo se puede mover.

    La siembra calcula `apertura = lo que el Balance publica HOY − la suma de
    los movimientos`, así que `apertura + suma` devuelve, al centavo, el
    mismo número. Incluye a DEP.PICH. con sus −455,89 que sumados dan 0: esa
    decisión es de la dueña, no de un deploy.
    """
    import re

    from modules.bancos import apertura as ap_mod

    sql = " ".join(ap_mod._SIEMBRA_SQL.split())
    assert "ON CONFLICT (no_banco) DO NOTHING" in sql, (
        "sin esto la siembra pisaría una apertura que una persona afirmó"
    )
    # apertura = saldo publicado − suma firmada
    assert re.search(r"ORDER BY t\.fecha DESC, t\.id_transaccion DESC.*?"
                     r"LIMIT 1.*?\), 0\) - ", sql), sql[:400]
    assert "ABS(t.saldo) > 0.5" in sql, (
        "el filtro que sostiene los −455,89 de DEP.PICH. desapareció: la "
        "siembra le cambiaría el saldo a ese banco"
    )


def test_la_apertura_no_vive_en_una_tabla_que_el_sync_pisa():
    """🔒 `scintela.banco` la reescribe el sync del dBase.

    Una columna nuestra ahí se borraría sin aviso en el próximo sync y el
    patrimonio se iría a cero en silencio.
    [[feedback_dato_pc_only_no_colgar_de_lo_que_el_sync_pisa]]
    """
    from modules.bancos import apertura as ap_mod

    boot = " ".join(ap_mod._BOOTSTRAP_SQL.split())
    assert "CREATE TABLE IF NOT EXISTS scintela.banco_apertura" in boot
    assert "ALTER TABLE" not in boot.upper()


def test_la_suma_firmada_usa_la_regla_unica():
    """La misma `DOCS_ENTRADA` de bank_helpers, no una copia a mano."""
    import inspect

    import bank_helpers
    from modules.informes import queries as iq

    src = inspect.getsource(iq.saldo_bancos)
    i = src.index("AS suma_firmada")
    bloque = src[max(0, i - 500):i]
    for doc in bank_helpers.DOCS_ENTRADA:
        assert f"'{doc}'" in bloque, f"falta {doc} en la suma firmada"
