"""Las tres impresiones del estado de cuenta y el pie con el total de cheques.

TMT 2026-08-03. Dueña: "cuando saco la impresión del estado de cuenta del
cliente, podemos generar igual dos botones — 1, que diga imprimir facturas y
salga esto" y "únicamente agregando abajo una línea q diga TOTAL CHEQUES XXX
(que son los cheques por cobrar)". Alex, por WhatsApp: "que sea para imprimir
SOLO listado de cheques… de momento se imprime incluso los depositados y para
enviar eso al cliente viene a ser innecesario".

Referencia: `CUENTA.PRG` L365-392 cierra el estado de cuenta con SALDO /
CHEQUES A DEPOSITAR / (+ CHEQUES PROTESTADOS) / TOTAL, pegados a las facturas.

Son tests de PLANTILLA (no tocan Postgres): leen los archivos y verifican los
ganchos que hacen andar las tres impresiones. Si alguien saca una clase o un
botón, esto falla.
"""

from __future__ import annotations

from pathlib import Path

TPL = Path(__file__).resolve().parent.parent / "modules/informes/templates/informes"
IMPRESO = (TPL / "_estado_cuenta_impreso.html").read_text(encoding="utf-8")
PANTALLA = (TPL / "estado_cuenta.html").read_text(encoding="utf-8")
# TMT 2026-08-03 (dueña: "cuando quiero imprimir la cartera por vendedor,
# provincias o grupos igual debería poder elegir sólo factura sólo cheques o
# ambas"): los tres botones se mudaron a un parcial COMPARTIDO entre la
# pantalla individual y la impresión en lote.
BOTONES = (TPL / "_ec_print_botones.html").read_text(encoding="utf-8")
LOTE = (TPL / "estado_cuenta_lote_print.html").read_text(encoding="utf-8")
GRUPOS = (TPL / "estado_cuenta_grupos.html").read_text(encoding="utf-8")
INCLUDE = '{% include "informes/_ec_print_botones.html" %}'


def test_el_pie_de_facturas_trae_el_total_de_cheques_por_cobrar():
    """La línea que pidió la dueña, y el TOTAL, van PEGADOS a las facturas.

    Antes el total vivía al final de todo, después de la tabla de cheques: al
    imprimir sólo la hoja de facturas no salía.
    """
    i_tabla_facturas = IMPRESO.index("ec-bloque-facturas")
    i_cheques = IMPRESO.index("ec-bloque-cheques")
    i_total = IMPRESO.index("Total cheques")
    assert i_tabla_facturas < i_total < i_cheques, (
        "el pie con el total de cheques tiene que quedar dentro del bloque de "
        "facturas, antes de la tabla de cheques"
    )
    # Y el TOTAL suma saldo + cheques por cobrar + protestados (CUENTA.PRG).
    assert "(_saldo_neto or 0) + _ch_depositar + _ch_protestados" in IMPRESO
    assert "+ Cheques protestados" in IMPRESO


def test_hay_tres_botones_de_impresion():
    for etiqueta in (">Imprimir todo<", ">Imprimir facturas<", ">Imprimir cheques<"):
        assert etiqueta in BOTONES, etiqueta
    assert "ecImprimir('todo')" in BOTONES
    assert "ecImprimir('facturas')" in BOTONES
    assert "ecImprimir('cheques')" in BOTONES


def test_las_dos_pantallas_usan_LOS_MISMOS_botones():
    """Individual y lote (vendedor / provincia / grupos) incluyen el parcial.

    Si se copiara en vez de incluirse, un lado se arregla y el otro no, y el
    papel sale distinto según de qué pantalla salió — sin que se note hasta
    después de imprimir. Mismo principio que el clasificador compartido.
    """
    assert INCLUDE in PANTALLA, "el estado de cuenta individual dejó de incluirlo"
    assert INCLUDE in LOTE, "la impresión en lote dejó de incluirlo"
    # Y NADIE tiene una copia del CSS ni del helper.
    for nombre, html in (("estado_cuenta.html", PANTALLA), ("lote_print", LOTE)):
        assert "ec-print-facturas" not in html, f"{nombre} copió el CSS de impresión"
        assert "function ecImprimir" not in html, f"{nombre} copió el helper"


def test_cada_boton_oculta_el_bloque_que_no_toca():
    assert "body.ec-print-facturas .ec-bloque-cheques { display: none" in BOTONES
    assert "body.ec-print-cheques  .ec-bloque-facturas { display: none" in BOTONES


def test_imprimir_cheques_deja_afuera_los_ya_cobrados():
    """Alex: mandarle al cliente los cheques que ya cobramos "viene a ser
    innecesario"."""
    assert "body.ec-print-cheques  tr.ec-ch-cobrado { display: none" in BOTONES
    # La marca NO se re-define en la plantilla: sale de `por_cobrar`, que
    # calcula `estado_cuenta_cliente`.
    assert "{{ '' if c.por_cobrar else ' ec-ch-cobrado' }}" in IMPRESO
    assert "stat_u in ('B','A')" not in IMPRESO, (
        "volvió el criterio B/A pelado: se cuelan los depositados legacy "
        "(V/W/I/J/K), el cobrado en caja (C) y el endosado (E)"
    )


def test_por_cobrar_es_UNA_definicion():
    """Tres lugares decidían por su cuenta qué cheque "todavía nos debe": la
    fila que se esconde al imprimir, el contador del lote y el total del pie.
    Con criterios distintos el papel salía contradictorio."""
    from modules.informes import queries as iq

    # Z/P cartera · D Daniela · 1/2/3/R rebotados · 9 sin fondos → los debe.
    for st in ("Z", "P", "D", "1", "2", "3", "R", "9", " z ", "p"):
        assert iq.cheque_por_cobrar(st), st
    assert iq.cheque_por_cobrar("9"), (
        "'9' es el rebote del dropdown de /cheques (el banco lo rechazó y se le "
        "pone stop al cliente): es el caso MÁS por cobrar que hay"
    )
    # Depositados (incluidos los legacy), cobrado en caja, endosado, anulado.
    for st in ("B", "A", "V", "W", "I", "J", "K", "C", "E", "X", "T", None, ""):
        assert not iq.cheque_por_cobrar(st), st
    # Y cada cheque sale de la query ya marcado, para que las plantillas no
    # tengan que re-decidir.
    fuente = Path(iq.__file__).read_text(encoding="utf-8")
    assert '_c["por_cobrar"] = cheque_por_cobrar(' in fuente


def test_el_total_por_cobrar_del_SQL_usa_la_misma_lista(monkeypatch):
    """El total del pie se calcula en SQL. Si ese `IN (...)` se tipea aparte,
    el número y las filas impresas se separan sin que nada avise."""
    import db
    from modules.informes import queries as iq

    vistos: list[str] = []

    def _fetch_one(sql, params=None, conn=None):
        vistos.append(sql)
        # Un cliente que existe; los totales caen a 0 por los .get(...) or 0.
        return {"codigo_cli": "ZZZ", "nombre": "CLIENTE ZZZ"}

    monkeypatch.setattr(db, "fetch_one", _fetch_one)
    monkeypatch.setattr(db, "fetch_all", lambda *a, **k: [])
    iq.estado_cuenta_cliente("ZZZ")

    sql = next((q for q in vistos if "AS por_cobrar" in q), None)
    assert sql, "la query de totales dejó de calcular `por_cobrar`"
    caso = sql.split("AS por_cobrar")[0].rsplit("WHEN", 1)[-1]
    import re

    en_sql = {x for x in re.findall(r"'([^']*)'", caso) if x}
    assert en_sql == set(iq.STATS_CHEQUE_POR_COBRAR), (
        f"el IN del SQL no es la lista compartida: sobra {en_sql - set(iq.STATS_CHEQUE_POR_COBRAR)}, "
        f"falta {set(iq.STATS_CHEQUE_POR_COBRAR) - en_sql}"
    )
    # Y normaliza igual que la función Python: un `stat = 'Z '` tiene que caer
    # del MISMO lado en los dos, si no el total contradice a las filas.
    assert "UPPER(TRIM(" in caso, "el SQL no normaliza y `cheque_por_cobrar()` sí"


def test_el_total_impreso_es_LA_SUMA_de_las_filas_que_quedan(app, fake_db, monkeypatch):
    """No alcanza con que el total exista: tiene que ser la suma de lo que se
    ve. Cheques de $50: Z, P (se imprimen) + B, X, C, E (no) → 100,00."""
    html = _get_lote(app, fake_db, monkeypatch, [
        _cliente_fake("SUMA", facturas=1, cheques=["Z", "P", "B", "X", "C", "E", "9"]),
    ])
    bloque = html[html.index("CLIENTE SUMA"):]
    bloque = bloque[:bloque.index("Total de los cheques listados") + 400]
    # 4 filas por cobrar (Z, P, 9 … y ninguna más) → las otras llevan la marca.
    assert bloque.count("ec-ch-cobrado") == 4, "B/X/C/E tienen que quedar marcados"
    assert "150,00" in bloque.split("Total de los cheques listados")[1], (
        "el total impreso no es la suma de Z + P + 9"
    )


def test_en_solo_cheques_el_total_cuadra_con_lo_impreso():
    """El pie "Total (N cheques)" cuenta TODOS y la franja dice "Depositados
    (B/A): $X" — las dos contradicen la hoja del cliente, donde esas filas no
    salen. Se esconden y sale un total que sí cuadra."""
    assert "ec-ch-total-todos" in IMPRESO and "ec-ch-resumen" in IMPRESO
    assert "body.ec-print-cheques .ec-ch-total-todos," in BOTONES
    assert "body.ec-print-cheques .ec-ch-resumen { display: none" in BOTONES
    assert "body.ec-print-cheques .ec-ch-total-cobrar { display: block" in BOTONES
    # Y en pantalla / "imprimir todo" ese total no aparece.
    assert ".ec-ch-total-cobrar { display: none; }" in BOTONES
    assert "t.cheques_por_cobrar" in IMPRESO
    # El rótulo no puede competir con el "Total cheques (por cobrar)" del pie de
    # facturas, que sale de OTRA definición (Z/P, paridad CUENTA.PRG): dos
    # líneas con la misma etiqueta y números distintos en el mismo estado.
    assert "Total de los cheques listados" in IMPRESO
    assert IMPRESO.count(
        'Total cheques <span class="text-slate-400">(por cobrar)</span>'
    ) == 1, "hay dos líneas rotuladas 'por cobrar' con números distintos"


def test_el_separador_va_sobre_el_primer_bloque_que_de_verdad_se_imprime():
    """`.cli-block:first-of-type` es POSICIONAL: si el primer cliente queda
    escondido, el primero que sí se imprime arranca con una raya colgada."""
    assert ".cli-block.ec-primero { margin-top: 0" in BOTONES
    assert "ec-primero" in BOTONES and "classList.toggle('ec-primero'" in BOTONES
    assert "e.classList.remove('ec-primero')" in BOTONES, "la marca no se limpia"


def test_el_lote_saltea_a_los_clientes_que_no_tienen_nada_que_mostrar():
    """Un vendedor puede tener 69 clientes y la mitad sin cheques por cobrar.
    Sin esto, "sólo cheques" imprime decenas de hojas con el nombre del cliente
    y un "Sin cheques" adentro."""
    assert 'data-n-ch-cobrar="{{ _ch_cobrar | length }}"' in LOTE
    # El contador NO re-define "por cobrar": filtra por la marca compartida.
    assert "c.cheques | selectattr('por_cobrar') | list" in LOTE
    assert 'body.ec-print-cheques .cli-block[data-n-ch-cobrar="0"] { display: none' in BOTONES


def test_el_lote_no_imprime_solo_al_abrirse():
    """Antes el botón azul entraba con ?auto=1 y disparaba window.print() al
    cargar: no había forma de elegir qué imprimir."""
    assert "auto=1" not in GRUPOS, "el botón sigue auto-imprimiendo"
    assert "addEventListener('load'" not in LOTE, "el lote imprime solo al cargar"


def test_la_clase_de_impresion_se_limpia_siempre():
    """Si quedara pegada, la pantalla se vería mutilada después de imprimir."""
    assert "afterprint" in BOTONES
    assert "setTimeout(limpiar, 3000)" in BOTONES


def test_las_tres_pantallas_fechan_el_cheque_igual():
    """Cargado y Depositado tienen que salir de la MISMA expresión en las tres.

    TMT 2026-08-03. `/cheques` y la ficha se arreglaron a la mañana; este
    parcial tiene query propia y se quedó atrás: "Cargado" caía a `fecha_crea`
    (12/07/2026 en los ~3.200 del dBase) y "Depositado" leía `fechaing` en vez
    de `fechaout`. Es el papel que se le manda al cliente.
    """
    from modules.informes import queries as iq

    assert "dia_ingreso" in IMPRESO
    assert "(c.fechaout or c.fechaing)" in IMPRESO
    # La query del estado de cuenta usa la constante compartida, no una copia.
    fuente = Path(iq.__file__).read_text(encoding="utf-8")
    assert "_SQL_DIA_INGRESO_CHEQUE" in fuente
    assert "c.fechaout" in fuente

    for tpl_dir, nombre in (
        ("modules/cheques/templates/cheques", "lista.html"),
        ("modules/cheques/templates/cheques", "detalle.html"),
    ):
        html = (Path(__file__).resolve().parent.parent / tpl_dir / nombre).read_text(
            encoding="utf-8"
        )
        assert "dia_ingreso" in html, nombre
        assert "fechaout or " in html, nombre


# ── el lote se RENDERIZA de verdad (no sólo matchea strings) ──────────────
def _login(app, fake_db, perms=("informes.ver",)):
    rid = fake_db.add_role("Tester", list(perms))
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def _cliente_fake(codigo, *, facturas, cheques):
    """Lo que devuelve `estado_cuenta_cliente`. `por_cobrar` se calcula con la
    función REAL — si el fixture lo hardcodeara, el test pasaría aunque la
    definición compartida cambiara."""
    from datetime import date

    from modules.informes.queries import cheque_por_cobrar

    hoy = date(2026, 8, 3)
    return {
        "cliente": {"codigo_cli": codigo, "nombre": f"CLIENTE {codigo}"},
        "facturas": [
            {
                "id_factura": i, "numf": 1000 + i, "numf_completo": None,
                "fecha": hoy, "importe": 100.0, "abono": 0.0, "saldo": 100.0,
                "stat": "A", "tipo": "1",
            }
            for i in range(facturas)
        ],
        "cheques": [
            {
                "id_cheque": 500 + i, "no_cheque": str(700 + i), "stat": st,
                "importe": 50.0, "fecha": hoy, "fechad": hoy, "fechad_original": None,
                "fecha_postergacion": None, "fechaing": None, "fechaout": None,
                "dia_ingreso": hoy, "fecha_recibido": hoy, "fecha_crea": hoy,
                "banco": "PICHINCHA", "nombre_banco": "PICHINCHA",
                "por_cobrar": cheque_por_cobrar(st),
            }
            for i, st in enumerate(cheques)
        ],
        "totales": {
            "importe": 100.0 * facturas, "abono": 0.0, "saldo": 100.0 * facturas,
            "saldo_neto": 100.0 * facturas, "cheques_total": 50.0 * len(cheques),
            "cheques_cartera": 0.0, "cheques_depositados": 0.0,
            "cheques_rebotados": 0.0, "cheques_endosados": 0.0,
            # El MISMO número que tiene que imprimir la hoja de cheques.
            "cheques_por_cobrar": 50.0 * sum(1 for st in cheques if cheque_por_cobrar(st)),
        },
    }


def _get_lote(app, fake_db, monkeypatch, fakes, url="/informes/estado-cuenta/imprimir?por=vendedor&sel=EDG"):
    from modules.informes import queries as iq

    monkeypatch.setattr(
        iq, "estado_cuenta_clientes_saldos",
        lambda *a, **k: [
            {"codigo_cli": f["cliente"]["codigo_cli"], "saldo": 100.0,
             "vend": "EDG", "vendedor_activo": True, "vendedor_nombre": "EDG",
             "provincia": "PICHINCHA", "grupo_codigo": None, "grupo_nombre": None}
            for f in fakes
        ],
    )
    porcod = {f["cliente"]["codigo_cli"]: f for f in fakes}
    monkeypatch.setattr(iq, "estado_cuenta_cliente", lambda c, *a, **k: porcod.get(c, {}))
    r = _login(app, fake_db).get(url)
    assert r.status_code == 200, r.status_code
    return r.get_data(as_text=True)


def test_el_lote_cuenta_bien_los_cheques_por_cobrar_de_cada_cliente(app, fake_db, monkeypatch):
    """El contador que decide si el bloque se imprime en modo "sólo cheques".

    ZZZ:  2 en cartera + 1 depositado + 1 anulado → 2 por cobrar.
    NADA: sólo depositados y anulados        → 0 (su hoja no se imprime).
    ENDO: endosado (E) — ya no es nuestro    → 0.
    CAJA: cobrado en caja (C) / legacy V/W   → 0 (el criterio viejo, "todo lo
          que no sea B/A/X", los contaba como por cobrar y le mandaba al
          cliente una hoja con cheques que ya habíamos cobrado).
    """
    html = _get_lote(app, fake_db, monkeypatch, [
        _cliente_fake("ZZZ", facturas=2, cheques=["Z", "P", "B", "X"]),
        _cliente_fake("NADA", facturas=1, cheques=["B", "A", "X"]),
        _cliente_fake("ENDO", facturas=1, cheques=["E"]),
        _cliente_fake("CAJA", facturas=1, cheques=["C", "V", "W"]),
    ])
    for cod, n_ch in (("ZZZ", 2), ("NADA", 0), ("ENDO", 0), ("CAJA", 0)):
        i = html.index(f"CLIENTE {cod}")
        bloque = html[max(0, i - 200):i]
        assert '<section class="cli-block"' in bloque, f"{cod}: bloque cortado"
        assert f'data-n-ch-cobrar="{n_ch}"' in bloque, f"{cod}: cheques por cobrar"
    # Y los tres botones llegaron por el include.
    assert ">Imprimir cheques<" in html and "function ecImprimir" in html


def test_el_lote_no_dispara_la_impresion_al_cargar(app, fake_db, monkeypatch):
    """Antes entraba con ?auto=1 y llamaba window.print() al cargar: no había
    forma de elegir qué imprimir."""
    html = _get_lote(
        app, fake_db, monkeypatch,
        [_cliente_fake("ZZZ", facturas=1, cheques=["Z"])],
        url="/informes/estado-cuenta/imprimir?por=vendedor&sel=EDG&auto=1",
    )
    assert "window.print(); }, 400)" not in html
