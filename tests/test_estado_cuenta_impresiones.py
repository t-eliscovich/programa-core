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
    i_total = IMPRESO.index(">Cheques a depositar<")
    assert i_tabla_facturas < i_total < i_cheques, (
        "el pie con el total de cheques tiene que quedar dentro del bloque de "
        "facturas, antes de la tabla de cheques"
    )
    # Y el TOTAL suma saldo + TODO lo que el cliente debe (CUENTA.PRG: SA+CH).
    assert "(_saldo_neto or 0) + _ch_total" in IMPRESO
    assert "+ Cheques protestados" in IMPRESO


def test_el_pie_es_el_de_CUENTA_PRG():
    """Paridad literal con `CUENTA.PRG` PROCEDURE EDITA (dueña: "lo que haga
    dbase"), verificado contra el fuente del FoxPro:

        &SU .AND. STAT $ "12"     TO CHPRO
        &SU .AND. STAT $ "ZP12D"  TO CH
        ? "CHEQUES A DEPOSITAR: " + STR(CH-CHPRO)    -> Z/P/D
        ? "+ CHEQUES PROTESTADOS: " + STR(CHPRO)     -> 1/2
        ? "TOTAL: " + STR(SA+CH)                     -> saldo + Z/P/1/2/D

    `3 R 9` no existen en el dBase (CHEQUES.DBF: B/Z/X/C/P/W/1/V/D) — son
    estados que agregó PC para el mismo hecho (el cheque rebotó), así que van
    con los protestados. Si quedaran afuera, un cheque rebotado por PC
    desaparecería de lo que el cliente debe.
    """
    from modules.informes import queries as iq

    assert iq.STATS_CHEQUE_A_DEPOSITAR == ("Z", "P", "D"), "CH − CHPRO del PRG"
    assert set(iq.STATS_CHEQUE_PROTESTADO) >= {"1", "2"}, "CHPRO del PRG"
    for st in ("3", "R", "9"):
        assert st in iq.STATS_CHEQUE_PROTESTADO, (
            f"{st} es un rebote que sólo existe en PC: tiene que contar"
        )
    # ⭐ Invariante estructural: por cobrar ES la suma de los dos, no una copia.
    assert set(iq.STATS_CHEQUE_POR_COBRAR) == (
        set(iq.STATS_CHEQUE_A_DEPOSITAR) | set(iq.STATS_CHEQUE_PROTESTADO)
    )
    assert not (set(iq.STATS_CHEQUE_A_DEPOSITAR) & set(iq.STATS_CHEQUE_PROTESTADO))
    # El `D` es el que se perdía: el pie sumaba `cheques_cartera` (Z/P).
    assert "D" in iq.STATS_CHEQUE_A_DEPOSITAR
    assert "t.cheques_a_depositar" in IMPRESO and "t.cheques_protestados" in IMPRESO
    assert "t.cheques_cartera or 0" not in IMPRESO, "volvió la definición vieja (Z/P)"
    assert "t.cheques_rebotados or 0" not in IMPRESO


def test_el_pie_replica_los_IF_del_PRG():
    """El PRG imprime cada renglón sólo si tiene monto (`IF CH-CHPRO > 0`,
    `IF CHPRO > 0`, `IF CH > 0`)."""
    i = IMPRESO.index(">Cheques a depositar<")
    bloque = IMPRESO[i - 400:i + 1400]
    assert "{% if _ch_depositar > 0 %}" in bloque
    assert "{% if _ch_protestados > 0 %}" in bloque
    assert "{% if _ch_total > 0 %}" in bloque
    # `> 0`, no truthiness: un importe negativo tampoco imprime renglón.
    assert "{% if _ch_depositar %}" not in bloque


def test_hay_tres_botones_de_impresion():
    """TMT 2026-08-04: el rótulo tiene dos formas. La pantalla individual los
    pone detrás de un ícono de impresora y pide los cortos ("Todo / Facturas /
    Cheques", `ec_btn_corto`); el lote no tiene ícono y sigue con los largos.
    Los dos salen de acá — el que se rompe si alguien toca uno solo."""
    for corto, largo in (("Todo", "Imprimir todo"),
                         ("Facturas", "Imprimir facturas"),
                         ("Cheques", "Imprimir cheques")):
        assert f">{{{{ '{corto}' if ec_btn_corto else '{largo}' }}}}<" in BOTONES, largo
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

    import re

    sql = next((q for q in vistos if "AS por_cobrar" in q), None)
    assert sql, "la query de totales dejó de calcular `por_cobrar`"
    # Los TRES totales del pie salen de las MISMAS listas. Que uno solo se
    # desalinee es justo el bug que veníamos a cerrar.
    for alias, esperado in (
        ("por_cobrar", iq.STATS_CHEQUE_POR_COBRAR),
        ("a_depositar", iq.STATS_CHEQUE_A_DEPOSITAR),
        ("protestados", iq.STATS_CHEQUE_PROTESTADO),
    ):
        assert f"AS {alias}" in sql, alias
        caso = sql.split(f"AS {alias}")[0].rsplit("WHEN", 1)[-1]
        en_sql = {x for x in re.findall(r"'([^']*)'", caso) if x}
        assert en_sql == set(esperado), (
            f"el IN de `{alias}` no es la lista compartida: sobra "
            f"{en_sql - set(esperado)}, falta {set(esperado) - en_sql}"
        )
        # Y normaliza igual que la función Python: un `stat = 'Z '` tiene que
        # caer del MISMO lado en los dos, si no el total contradice a las filas.
        assert "UPPER(TRIM(" in caso, f"`{alias}` no normaliza y Python sí"


def test_el_total_impreso_es_LA_SUMA_de_las_filas_que_quedan(app, fake_db, monkeypatch):
    """No alcanza con que el total exista: tiene que ser la suma de lo que se
    ve. Cheques de $50: Z, P (se imprimen) + B, X, C, E (no) → 100,00."""
    html = _get_lote(app, fake_db, monkeypatch, [
        _cliente_fake("SUMA", facturas=1, cheques=["Z", "P", "B", "X", "C", "E", "9"]),
    ])
    bloque = html[html.index("CLIENTE SUMA"):]
    bloque = bloque[:bloque.index("Total cheques por cobrar") + 400]
    # 4 filas por cobrar (Z, P, 9 … y ninguna más) → las otras llevan la marca.
    assert bloque.count("ec-ch-cobrado") == 4, "B/X/C/E tienen que quedar marcados"
    assert "150,00" in bloque.split("Total cheques por cobrar")[1], (
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
    assert "Total cheques por cobrar" in IMPRESO
    # Ya no hay dos líneas peleando por el rótulo: el pie de facturas usa los
    # nombres del PRG ("Cheques a depositar" / "+ Cheques protestados") y su
    # TOTAL suma exactamente el mismo `cheques_por_cobrar` que imprime la hoja
    # de cheques.
    assert 'Total cheques <span class="text-slate-400">(por cobrar)</span>' not in IMPRESO


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

    from modules.informes.queries import (
        STATS_CHEQUE_A_DEPOSITAR,
        STATS_CHEQUE_PROTESTADO,
        cheque_por_cobrar,
    )

    def _suma(stats):
        return 50.0 * sum(1 for st in cheques if st.upper() in stats)

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
            # El MISMO número que tiene que imprimir la hoja de cheques…
            "cheques_por_cobrar": 50.0 * sum(1 for st in cheques if cheque_por_cobrar(st)),
            # …y los dos renglones del pie (CUENTA.PRG), que tienen que sumarlo.
            "cheques_a_depositar": _suma(STATS_CHEQUE_A_DEPOSITAR),
            "cheques_protestados": _suma(STATS_CHEQUE_PROTESTADO),
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


def test_el_pie_no_se_come_los_cheques_D(app, fake_db, monkeypatch):
    """El bug que cerró la dueña con "lo que haga dbase": el renglón "a
    depositar" sumaba `cheques_cartera` (Z/P), así que un cheque en gestión
    Daniela (`D`) no entraba ni ahí ni en el TOTAL — y el dBase lo cuenta
    (`STAT $ "ZP12D"`).

    Cliente con 1 factura de $100 y tres cheques de $50 (`D`, `Z`, `1`):
        Cheques a depositar   = D + Z = 100,00
        + Cheques protestados = 1     =  50,00
        TOTAL                 = 100 + 150 = 250,00   (antes daba 200,00)
    """
    html = _get_lote(app, fake_db, monkeypatch, [
        _cliente_fake("DANI", facturas=1, cheques=["D", "Z", "1"]),
    ])
    pie = html[html.index("CLIENTE DANI"):]
    pie = pie[:pie.index("ec-bloque-cheques")]
    i_dep = pie.index("Cheques a depositar")
    i_pro = pie.index("+ Cheques protestados")
    assert "100,00" in pie[i_dep:i_pro], "el D quedó fuera de 'a depositar'"
    i_tot = pie.index(">Total<", i_pro)
    assert "50,00" in pie[i_pro:i_tot], "los protestados no son el 1"
    assert "250,00" in pie[i_tot:], f"el TOTAL se comió el D: {pie[i_tot:][:400]!r}"


def test_lo_impreso_en_el_pie_suma_el_total(app, fake_db, monkeypatch):
    """Los DOS renglones del pie tienen que sumar el TOTAL que sale abajo, y ese
    mismo número es el que imprime la hoja de cheques. Se lee del HTML
    renderizado — que las constantes cierren entre sí es tautológico."""
    from modules.informes.queries import (
        STATS_CHEQUE_A_DEPOSITAR,
        STATS_CHEQUE_PROTESTADO,
        cheque_por_cobrar,
    )

    todos = list(STATS_CHEQUE_A_DEPOSITAR) + list(STATS_CHEQUE_PROTESTADO) + [
        "B", "A", "C", "V", "W", "E", "X",
    ]
    fake = _cliente_fake("MIX", facturas=1, cheques=todos)
    t = fake["totales"]
    assert t["cheques_por_cobrar"] == 50.0 * sum(1 for st in todos if cheque_por_cobrar(st))

    def _es(x):
        return f"{x:,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")

    html = _get_lote(app, fake_db, monkeypatch, [fake])
    pie = html[html.index("CLIENTE MIX"):]
    pie = pie[:pie.index("ec-bloque-cheques")]
    i_dep, i_pro = pie.index("Cheques a depositar"), pie.index("+ Cheques protestados")
    i_tot = pie.index(">Total<", i_pro)
    assert _es(t["cheques_a_depositar"]) in pie[i_dep:i_pro]
    assert _es(t["cheques_protestados"]) in pie[i_pro:i_tot]
    # saldo de la factura (100) + los dos renglones de arriba.
    assert _es(100.0 + t["cheques_por_cobrar"]) in pie[i_tot:]
    # Y la hoja de cheques imprime ese mismo por-cobrar.
    cheques = html[html.index("Total cheques por cobrar"):]
    assert _es(t["cheques_por_cobrar"]) in cheques[:400]


def test_sin_cheques_por_cobrar_no_queda_un_recuadro_vacio(app, fake_db, monkeypatch):
    """371 de los 493 clientes con saldo no tienen ningún cheque por cobrar. Sin
    el `if` externo les quedaba al pie un recuadro con borde de 2px y 24pt de
    aire, sin una sola letra adentro."""
    html = _get_lote(app, fake_db, monkeypatch, [
        _cliente_fake("SECO", facturas=2, cheques=["B", "X"]),
    ])
    pie = html[html.index("CLIENTE SECO"):]
    pie = pie[:pie.index("ec-bloque-cheques")]
    assert "Cheques a depositar" not in pie
    assert ">Total<" not in pie
    assert "border-t-2 border-slate-400" not in pie, "quedó el recuadro vacío"


def test_la_franja_de_abajo_corta_igual_que_el_pie(app, fake_db, monkeypatch):
    """En "Imprimir todo" el cliente ve el pie Y la franja en el mismo papel. Si
    cortan distinto (Z/P vs Z/P/D) los dos números se contradicen a la vista."""
    html = _get_lote(app, fake_db, monkeypatch, [
        _cliente_fake("FRANJA", facturas=1, cheques=["D", "Z", "1", "B"]),
    ])
    assert "A depositar <span class=\"text-slate-400\">(Z/P/D)</span>" in html
    assert "Protestados <span class=\"text-slate-400\">(1/2/3/R/9)</span>" in html
    assert "Cartera <span" not in html, "volvió el corte Z/P de la franja"
    assert "Rebotados <span" not in html
    franja = html[html.index("ec-ch-resumen"):]
    franja = franja[:franja.index("</div>", franja.index("Endosados"))]
    assert "100,00" in franja, "la franja no muestra D+Z como 'a depositar'"


# ---------------------------------------------------------------------------
# La columna Banco, centrada — TMT 2026-08-04
# ---------------------------------------------------------------------------
def _parcial_impreso() -> str:
    import os

    ruta = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "modules", "informes", "templates", "informes",
        "_estado_cuenta_impreso.html",
    )
    with open(ruta, encoding="utf-8") as fh:
        return fh.read()


def test_banco_va_centrado_entre_importe_y_stat():
    """Dueña: "centrá banco entre importe y stat".

    La columna tiene el 24% del ancho impreso —hace falta para
    "BANECUADOR"— y con el texto a la izquierda el nombre quedaba besando al
    importe, con un hueco largo hasta Stat. Centrado ocupa el medio del
    espacio que ya tenía.
    """
    html = _parcial_impreso()
    i = html.index(">Banco</th>")
    th = html[html.rindex("<th", 0, i):i]
    assert "text-center" in th
    assert "text-left" not in th


def test_la_celda_de_banco_va_centrada_igual_que_su_encabezado():
    """Si sólo se centra el <th>, el título queda en el medio y el nombre del
    banco pegado a la izquierda — peor que antes, porque ahora se nota."""
    html = _parcial_impreso()
    i = html.index("{{ c.nombre_banco or c.banco }}")
    td = html[html.rindex("<td", 0, i):i]
    assert "text-center" in td


def test_importe_sigue_a_la_derecha():
    """Es plata y se lee en columna con el Saldo de facturas: centrarlo
    también rompería la alineación que se acaba de conseguir."""
    html = _parcial_impreso()
    i = html.index(">Importe</th>")
    th = html[html.rindex("<th", 0, i):i]
    assert "text-right" in th


def test_no_se_tocaron_los_anchos_de_impresion():
    """El cambio es de alineación. Si además se movieran los anchos, Importe
    dejaría de terminar en el 70% y se despegaría del Saldo de facturas."""
    html = _parcial_impreso()
    assert "table th:nth-child(7),\n        main .ec-bloque-cheques table tbody td:nth-child(7) { width: 24% !important; }" in html
