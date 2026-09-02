"""Tests del sync de clientes Asinfo → PC (modules/clientes/sync_asinfo).

Sin Postgres ni Metabase: `traer_asinfo` y las funciones de `db` se mockean.
Las reglas que se prueban son las decisiones de la dueña del 05/08/2026:
pisar nombre/RUC, teléfono sólo rellena vacíos con valores reales, cupo
jamás, alta nueva → campanita, RUC repetido → conflicto sin alta; más la del
25/08/2026: en el descuento manda ASINFO y pisa el de la ficha, salvo cuando
Asinfo no sabe (sin lista, o con un nombre de lista que no se entiende);
más la del 27/08/2026 (Tamara): VENDEDOR y DIRECCIÓN también los manda
Asinfo — mapa de agentes, la casa (INT) aparte, ventanas cada hora 07–19.
"""
from __future__ import annotations

from datetime import UTC

import db
from modules.clientes import sync_asinfo as sa

# ─── telefono_util: la basura de Asinfo no entra ────────────────────────────

def test_telefono_util_rechaza_placeholders():
    assert sa.telefono_util("2222222") == ""
    assert sa.telefono_util("02-2222222") == ""      # contiene el relleno
    assert sa.telefono_util("9999999") == ""          # un solo dígito repetido
    assert sa.telefono_util("12345") == ""            # corto
    assert sa.telefono_util("") == ""
    assert sa.telefono_util(None) == ""


def test_telefono_util_acepta_reales():
    assert sa.telefono_util("0984042960") == "0984042960"
    assert sa.telefono_util("  02-334-0224 ") == "02-334-0224"


def test_ruc10():
    assert sa.ruc10("1712345678001") == "1712345678"
    assert sa.ruc10("171-234.5678") == "1712345678"
    assert sa.ruc10("12345") == ""      # menos de 10 dígitos no es clave
    assert sa.ruc10(None) == ""


# ─── el pase completo ───────────────────────────────────────────────────────

def _armar(monkeypatch, asinfo_rows, pc_rows, contesto=True):
    """Mockea las dos fuentes y captura todo lo que el sync escribe."""
    capturado = {"updates": [], "altas": [], "avisos": [], "logs": []}

    monkeypatch.setattr(sa, "traer_asinfo", lambda: (asinfo_rows, contesto))
    monkeypatch.setattr(db, "fetch_all", lambda *a, **k: pc_rows)

    def fake_execute(sql, params=None, conn=None):
        if "UPDATE scintela.cliente" in sql:
            capturado["updates"].append((sql, params))
            return len(params or []) // 9  # aprox: filas del VALUES
        return 1

    monkeypatch.setattr(db, "execute", fake_execute)
    monkeypatch.setattr(sa, "_guardar_log",
                        lambda usuario, rep: capturado["logs"].append(rep))

    from modules.avisos import queries as av_q
    monkeypatch.setattr(av_q, "avisar",
                        lambda **kw: capturado["avisos"].append(kw) or True)

    from modules.clientes import queries as cli_q
    monkeypatch.setattr(cli_q, "crear",
                        lambda **kw: capturado["altas"].append(kw) or {"id_cliente": 1})
    return capturado


def test_metabase_caido_no_toca_nada(monkeypatch):
    cap = _armar(monkeypatch, [], [], contesto=False)
    r = sa.sincronizar()
    assert r["ok"] is False
    assert cap["updates"] == [] and cap["altas"] == [] and cap["avisos"] == []


def test_pisa_nombre_y_ruc_y_rellena_telefono_vacio(monkeypatch):
    asinfo = [
        # nombre distinto (formato fiscal) + RUC corregido + tel real
        {"cod": "AAA", "ruc": "1712345678001", "nombre": "PEREZ JUAN",
         "tel1": "0984042960", "tel2": ""},
        # idéntico → no genera cambio
        {"cod": "BBB", "ruc": "0912345678001", "nombre": "IGUAL IGUAL",
         "tel1": "2222222", "tel2": ""},
    ]
    pc = [
        {"id_cliente": 1, "cod": "AAA", "nombre": "JUAN PEREZ",
         "ruc": "1712345678999", "telefono": ""},
        {"id_cliente": 2, "cod": "BBB", "nombre": "IGUAL IGUAL",
         "ruc": "0912345678001", "telefono": "022345678"},
    ]
    cap = _armar(monkeypatch, asinfo, pc)
    r = sa.sincronizar()
    assert r["ok"] is True
    assert len(cap["updates"]) == 1
    sql, params = cap["updates"][0]
    # cupo y correo NUNCA aparecen en el UPDATE. (Hasta el 27/08/2026 acá
    # también se afirmaba que vend y dirección no aparecían — decisión del
    # 05/08; Tamara la dio vuelta el 27/08: "los clientes en Asinfo a veces
    # cambian de dirección y de vendedor — lo tiene que agarrar".)
    assert "cupo" not in sql and "correo" not in sql
    assert "vend" in sql and "direccion1" in sql
    # una sola fila cambia: AAA con nombre, ruc y teléfono (sin lista de
    # descuentos en Asinfo, el descuento viaja en None y COALESCE lo ignora)
    assert params[0] == "sync-asinfo"
    assert list(params[1:]) == ["AAA", "PEREZ JUAN", "1712345678001",
                                "0984042960", None, None, None, None, None]
    assert r["altas"] == [] and r["conflictos"] == []


def test_telefono_de_pc_no_se_pisa(monkeypatch):
    asinfo = [{"cod": "AAA", "ruc": "", "nombre": "PEREZ JUAN",
               "tel1": "0984042960", "tel2": ""}]
    pc = [{"id_cliente": 1, "cod": "AAA", "nombre": "PEREZ JUAN",
           "ruc": "", "telefono": "099111222"}]
    cap = _armar(monkeypatch, asinfo, pc)
    sa.sincronizar()
    # nombre igual, ruc vacío, PC ya tiene teléfono → cero updates
    assert cap["updates"] == []


def test_alta_nueva_crea_y_deja_campanita(monkeypatch):
    asinfo = [{"cod": "VA2", "ruc": "1721669206001",
               "nombre": "VELIZ ALCIVAR LUIS", "tel1": "0984042960", "tel2": ""}]
    cap = _armar(monkeypatch, asinfo, [])
    r = sa.sincronizar()
    assert r["altas"] == ["VA2"]
    assert cap["altas"][0]["codigo_cli"] == "VA2"
    assert cap["altas"][0]["nombre"] == "VELIZ ALCIVAR LUIS"
    # cupo NO viaja en el alta: lo carga Andrés cuando suena la campanita
    assert "cupo" not in cap["altas"][0]
    aviso = cap["avisos"][0]
    assert aviso["clave"] == "cliente-nuevo-VA2"
    assert "cupo y descuento" in aviso["titulo"]
    # TMT 2026-08-06 (dueña): el click te lleva DERECHO a editar el
    # cliente (ahí viven cupo y descuento), no a la lista filtrada.
    assert aviso["url"] == "/clientes/VA2/editar"


def test_ruc_repetido_es_conflicto_no_alta(monkeypatch):
    # VOL es nuevo por código pero su RUC ya vive en PC como OTR →
    # sucursal/recodificación: NO se importa (duplicaría plata).
    asinfo = [{"cod": "VOL", "ruc": "1891773613001",
               "nombre": "ASOC TEXTIL", "tel1": "", "tel2": ""}]
    pc = [{"id_cliente": 9, "cod": "OTR", "nombre": "LA MISMA",
           "ruc": "1891773613001", "telefono": ""}]
    cap = _armar(monkeypatch, asinfo, pc)
    r = sa.sincronizar()
    assert r["altas"] == []
    assert cap["altas"] == []
    assert r["conflictos"][0]["cod"] == "VOL"
    assert r["conflictos"][0]["en_pc"] == ["OTR"]
    claves = {a["clave"] for a in cap["avisos"]}
    assert "cliente-conflicto-VOL" in claves


def test_codigo_duplicado_en_asinfo_se_excluye(monkeypatch):
    asinfo = [
        {"cod": "AR1", "ruc": "1111111111001", "nombre": "UNO", "tel1": "", "tel2": ""},
        {"cod": "AR1", "ruc": "2222222222001", "nombre": "DOS", "tel1": "", "tel2": ""},
    ]
    cap = _armar(monkeypatch, asinfo, [])
    r = sa.sincronizar()
    assert r["dup_asinfo"] == ["AR1"]
    assert r["altas"] == [] and cap["altas"] == []
    claves = {a["clave"] for a in cap["avisos"]}
    assert "cliente-dup-asinfo-AR1" in claves


def test_codigo_largo_se_ignora(monkeypatch):
    # nombre_comercial con el RUC entero (ficha vieja de Asinfo) no es un
    # código de cliente — ni alta ni conflicto.
    asinfo = [{"cod": "1712345678001", "ruc": "1712345678001",
               "nombre": "SIN CODIGO", "tel1": "", "tel2": ""}]
    cap = _armar(monkeypatch, asinfo, [])
    r = sa.sincronizar()
    assert r["altas"] == [] and r["conflictos"] == [] and cap["altas"] == []


# ─── el descuento: TMT 2026-08-25 (dueña) ──────────────────────────────────

def test_descuento_de_lista_lee_el_segundo_tramo():
    # el primer tramo (5%) es el de contado, igual para todos; el segundo es
    # el del cliente, y es el que PC guarda en cliente.descuento
    assert sa.descuento_de_lista("5%y7%") == 7.0
    assert sa.descuento_de_lista("5%y14%") == 14.0
    assert sa.descuento_de_lista("5%y0%") == 0.0
    assert sa.descuento_de_lista(" 5 % y 4,5 % ") == 4.5


def test_descuento_de_lista_no_inventa():
    # cualquier nombre con otra forma: no se carga nada
    assert sa.descuento_de_lista("6%y7%") is None      # otro tramo de contado
    assert sa.descuento_de_lista("MAYORISTA") is None
    assert sa.descuento_de_lista("") is None
    assert sa.descuento_de_lista(None) is None
    assert sa.descuento_de_lista("5%y999%") is None


def test_descuento_a_escribir_manda_asinfo():
    assert sa.descuento_a_escribir(None, 7.0) == 7.0     # vacío → se carga
    assert sa.descuento_a_escribir(0, 7.0) == 7.0        # cero → se carga
    assert sa.descuento_a_escribir(7.0, 4.0) == 4.0      # cargado → SE PISA
    assert sa.descuento_a_escribir(9.0, 0.0) == 0.0      # el 0 de Asinfo vale
    assert sa.descuento_a_escribir(7.0, 7.0) is None     # igual → nada que hacer
    assert sa.descuento_a_escribir(0, 0.0) is None       # los dos en cero
    # lo único intocable: lo que Asinfo no sabe
    assert sa.descuento_a_escribir(7.0, None) is None
    assert sa.descuento_a_escribir(None, None) is None


def test_el_sync_rellena_el_descuento_vacio(monkeypatch):
    asinfo = [{"cod": "AAA", "ruc": "", "nombre": "PEREZ JUAN",
               "tel1": "", "tel2": "", "lista_desc": "5%y9%"}]
    pc = [{"id_cliente": 1, "cod": "AAA", "nombre": "PEREZ JUAN",
           "ruc": "", "telefono": "022345678", "descuento": None}]
    cap = _armar(monkeypatch, asinfo, pc)
    r = sa.sincronizar()
    sql, params = cap["updates"][0]
    assert "descuento = COALESCE(v.descuento::numeric, c.descuento)" in sql
    # el descuento viaja como TEXTO: si TODAS las filas fueran NULL, Postgres
    # no podría inferir el tipo de la columna del VALUES
    assert list(params[1:]) == ["AAA", None, None, None, "9.0", None, None, None, None]
    assert r["descuentos_puestos"] == 1
    assert r["descuentos_pisados"] == 0          # estaba vacío, no se pisó nada
    assert r["desc_cambiado"] == [
        {"cod": "AAA", "nombre": "PEREZ JUAN", "antes": None, "ahora": 9.0}
    ]


def test_el_sync_rellena_el_descuento_en_cero(monkeypatch):
    asinfo = [{"cod": "AAA", "ruc": "", "nombre": "PEREZ JUAN",
               "tel1": "", "tel2": "", "lista_desc": "5%y12%"}]
    pc = [{"id_cliente": 1, "cod": "AAA", "nombre": "PEREZ JUAN",
           "ruc": "", "telefono": "022345678", "descuento": 0}]
    cap = _armar(monkeypatch, asinfo, pc)
    r = sa.sincronizar()
    assert list(cap["updates"][0][1][1:]) == ["AAA", None, None, None, "12.0",
                                              None, None, None, None]
    assert r["descuentos_puestos"] == 1
    assert r["descuentos_pisados"] == 0     # cero cuenta como vacío, no como pisado


def test_asinfo_pisa_el_descuento_cargado_y_guarda_el_anterior(monkeypatch):
    """TMT 2026-08-25 (dueña): "el descuento que vale es el que está en Asinfo"."""
    asinfo = [{"cod": "CAL", "ruc": "", "nombre": "CALDERON SA",
               "tel1": "", "tel2": "", "lista_desc": "5%y14%"}]
    pc = [{"id_cliente": 1, "cod": "CAL", "nombre": "CALDERON SA",
           "ruc": "", "telefono": "022345678", "descuento": 12}]
    cap = _armar(monkeypatch, asinfo, pc)
    r = sa.sincronizar()
    assert list(cap["updates"][0][1][1:]) == ["CAL", None, None, None, "14.0",
                                              None, None, None, None]
    assert r["descuentos_pisados"] == 1
    # el valor ANTERIOR queda registrado: es la forma de volver atrás
    assert r["desc_cambiado"] == [
        {"cod": "CAL", "nombre": "CALDERON SA", "antes": 12.0, "ahora": 14.0}
    ]
    # UNA campanita con el número, no una por cliente
    avisos = [a for a in cap["avisos"] if a["clave"].startswith("clientes-desc-pisados")]
    assert len(avisos) == 1
    assert avisos[0]["titulo"] == "Asinfo cambió el descuento de 1 cliente"
    # de cuánto a cuánto, sin tener que abrir la pantalla del sync
    assert "CAL 12% → 14%" in avisos[0]["detalle"]


def test_descuento_igual_no_toca_ni_avisa(monkeypatch):
    asinfo = [{"cod": "CAL", "ruc": "", "nombre": "CALDERON SA",
               "tel1": "", "tel2": "", "lista_desc": "5%y12%"}]
    pc = [{"id_cliente": 1, "cod": "CAL", "nombre": "CALDERON SA",
           "ruc": "", "telefono": "022345678", "descuento": 12}]
    cap = _armar(monkeypatch, asinfo, pc)
    r = sa.sincronizar()
    assert cap["updates"] == [] and cap["avisos"] == []
    assert r["desc_cambiado"] == []


def test_sin_lista_en_asinfo_la_ficha_no_se_toca(monkeypatch):
    """Asinfo sin lista NO significa "sin descuento": significa que no sabe."""
    asinfo = [{"cod": "GUI", "ruc": "", "nombre": "GUILLEN SA",
               "tel1": "", "tel2": "", "lista_desc": ""}]
    pc = [{"id_cliente": 1, "cod": "GUI", "nombre": "GUILLEN SA",
           "ruc": "", "telefono": "022345678", "descuento": 9}]
    cap = _armar(monkeypatch, asinfo, pc)
    r = sa.sincronizar()
    assert cap["updates"] == []
    assert r["listas_raras"] == [] and r["desc_cambiado"] == []


def test_lista_que_no_se_entiende_no_carga_nada(monkeypatch):
    asinfo = [{"cod": "GUI", "ruc": "", "nombre": "GUILLEN SA",
               "tel1": "", "tel2": "", "lista_desc": "MAYORISTA"}]
    pc = [{"id_cliente": 1, "cod": "GUI", "nombre": "GUILLEN SA",
           "ruc": "", "telefono": "022345678", "descuento": None}]
    cap = _armar(monkeypatch, asinfo, pc)
    r = sa.sincronizar()
    assert cap["updates"] == []
    assert r["listas_raras"] == [{"cod": "GUI", "lista": "MAYORISTA"}]
    assert r["desc_cambiado"] == []
    raras = [a for a in cap["avisos"] if a["clave"].startswith("clientes-listas-raras")]
    assert len(raras) == 1


def test_alta_nueva_ya_viene_con_el_descuento(monkeypatch):
    asinfo = [{"cod": "VA2", "ruc": "1721669206001", "nombre": "VELIZ LUIS",
               "tel1": "", "tel2": "", "lista_desc": "5%y7%"}]
    cap = _armar(monkeypatch, asinfo, [])
    sa.sincronizar()
    assert cap["altas"][0]["descuento"] == 7.0
    # la campanita ya no pide el descuento: sólo falta el cupo
    aviso = cap["avisos"][0]
    assert aviso["titulo"] == "Cliente nuevo VA2 — cargarle cupo"


def test_el_log_recorta_listas_largas_sin_romper_el_json():
    import json
    rep = {"ok": True, "desc_cambiado": [{"cod": f"C{i}"} for i in range(400)]}
    chico = sa._para_log(rep)
    assert len(chico["desc_cambiado"]) == 300
    assert chico["desc_cambiado_total"] == 400
    json.loads(json.dumps(chico))     # sigue siendo JSON válido


# ─── vendedor y dirección: TMT 2026-08-27 (Tamara) ─────────────────────────
# "los clientes en Asinfo a veces cambian de dirección y de vendedor — lo
# tiene que agarrar". Revierte la regla del 05/08 que los dejaba quietos.

def test_vend_de_asinfo_mapea_y_no_adivina():
    # códigos medidos en vivo el 27/08: Asinfo no usa los mismos que PC
    assert sa.vend_de_asinfo("DEB") == ("BED", True)       # Héctor Bedón
    assert sa.vend_de_asinfo("DENNYS") == ("DJA", True)    # Dennys Jaramillo
    assert sa.vend_de_asinfo("ESTEFY") == ("EVB", True)    # Estefanía Bedón
    assert sa.vend_de_asinfo("V-JQU") == ("JQU", True)
    assert sa.vend_de_asinfo(" v-edg ") == ("EDG", True)
    assert sa.vend_de_asinfo("PPR") == ("PPR", True)
    assert sa.vend_de_asinfo("INT") == ("", True)          # la casa
    assert sa.vend_de_asinfo("") == (None, False)
    assert sa.vend_de_asinfo(None) == (None, False)
    assert sa.vend_de_asinfo("NUEVO") == (None, False)     # no se adivina


def test_el_sync_pisa_el_vendedor_y_guarda_el_anterior(monkeypatch):
    asinfo = [{"cod": "AAA", "ruc": "", "nombre": "PEREZ JUAN",
               "tel1": "", "tel2": "", "agente": "DEB"}]
    pc = [{"id_cliente": 1, "cod": "AAA", "nombre": "PEREZ JUAN", "ruc": "",
           "telefono": "022345678", "vend": "PPR", "direccion1": ""}]
    cap = _armar(monkeypatch, asinfo, pc)
    r = sa.sincronizar()
    assert list(cap["updates"][0][1][1:]) == ["AAA", None, None, None, None,
                                              "BED", None, None, None]
    # el valor ANTERIOR queda registrado: es la forma de volver atrás
    assert r["vend_cambiado"] == [
        {"cod": "AAA", "nombre": "PEREZ JUAN", "antes": "PPR", "ahora": "BED"}
    ]
    avisos = [a for a in cap["avisos"] if a["clave"].startswith("clientes-vend-pisados")]
    assert len(avisos) == 1
    assert avisos[0]["titulo"] == "Asinfo cambió el vendedor de 1 cliente"
    assert "AAA PPR → BED" in avisos[0]["detalle"]


def test_vend_igual_o_sin_agente_no_toca(monkeypatch):
    asinfo = [
        {"cod": "AAA", "ruc": "", "nombre": "PEREZ JUAN",
         "tel1": "", "tel2": "", "agente": "V-FL1"},   # ya es FL1
        {"cod": "BBB", "ruc": "", "nombre": "GOMEZ ANA",
         "tel1": "", "tel2": "", "agente": ""},        # Asinfo no sabe
    ]
    pc = [
        {"id_cliente": 1, "cod": "AAA", "nombre": "PEREZ JUAN", "ruc": "",
         "telefono": "1", "vend": "FL1", "direccion1": ""},
        {"id_cliente": 2, "cod": "BBB", "nombre": "GOMEZ ANA", "ruc": "",
         "telefono": "1", "vend": "EDG", "direccion1": ""},
    ]
    cap = _armar(monkeypatch, asinfo, pc)
    r = sa.sincronizar()
    assert cap["updates"] == []
    assert r["vend_cambiado"] == [] and r["agentes_raros"] == []


def test_vend_rellena_el_vacio_sin_campanita(monkeypatch):
    asinfo = [{"cod": "AAA", "ruc": "", "nombre": "PEREZ JUAN",
               "tel1": "", "tel2": "", "agente": "V-RMY"}]
    pc = [{"id_cliente": 1, "cod": "AAA", "nombre": "PEREZ JUAN", "ruc": "",
           "telefono": "1", "vend": None, "direccion1": ""}]
    cap = _armar(monkeypatch, asinfo, pc)
    r = sa.sincronizar()
    assert list(cap["updates"][0][1][1:]) == ["AAA", None, None, None, None,
                                              "RMY", None, None, None]
    assert r["vend_cambiado"] == [
        {"cod": "AAA", "nombre": "PEREZ JUAN", "antes": None, "ahora": "RMY"}
    ]
    # llenar un vacío no es pisarle el vendedor a nadie → sin campanita
    assert [a for a in cap["avisos"]
            if a["clave"].startswith("clientes-vend-pisados")] == []


def test_agente_que_no_se_conoce_no_toca_y_avisa(monkeypatch):
    asinfo = [{"cod": "AAA", "ruc": "", "nombre": "PEREZ JUAN",
               "tel1": "", "tel2": "", "agente": "NUEVO9"}]
    pc = [{"id_cliente": 1, "cod": "AAA", "nombre": "PEREZ JUAN", "ruc": "",
           "telefono": "1", "vend": "PPR", "direccion1": ""}]
    cap = _armar(monkeypatch, asinfo, pc)
    r = sa.sincronizar()
    assert cap["updates"] == []
    assert r["agentes_raros"] == [{"cod": "AAA", "agente": "NUEVO9"}]
    raros = [a for a in cap["avisos"] if a["clave"].startswith("clientes-agentes-raros")]
    assert len(raros) == 1


def test_la_casa_nunca_quita_el_vendedor(monkeypatch):
    """Decisión de Tamara (27/08/2026, con los 210 casos medidos a la
    vista): los clientes con X son de Intela, los de DJA también, los de
    los vendedores son de los vendedores, y BED agrupa sus clientes aparte
    sin perfil de vendedor. INT nunca borra: si un cliente pasa de verdad
    a la casa, el vendedor se saca a mano en la ficha. (A la mañana esto
    era un pendiente detrás de un flag `_INT_QUITA_VENDEDOR`, con la rama
    de pisar testeada; al decidirse se eliminaron flag y rama.)"""
    asinfo = [{"cod": "AAA", "ruc": "", "nombre": "PEREZ JUAN",
               "tel1": "", "tel2": "", "agente": "INT"}]
    pc = [{"id_cliente": 1, "cod": "AAA", "nombre": "PEREZ JUAN", "ruc": "",
           "telefono": "1", "vend": "BED", "direccion1": ""}]
    cap = _armar(monkeypatch, asinfo, pc)
    r = sa.sincronizar()
    assert cap["updates"] == []
    assert r["vend_cambiado"] == [] and r["agentes_raros"] == []


def test_el_sync_pisa_la_direccion_y_el_vacio_de_asinfo_no_borra(monkeypatch):
    asinfo = [
        {"cod": "AAA", "ruc": "", "nombre": "PEREZ JUAN",
         "tel1": "", "tel2": "", "dir1": "BARRIO LA JOSEFINA LOTE 217"},
        {"cod": "BBB", "ruc": "", "nombre": "GOMEZ ANA",
         "tel1": "", "tel2": "", "dir1": ""},          # Asinfo no tiene
    ]
    pc = [
        {"id_cliente": 1, "cod": "AAA", "nombre": "PEREZ JUAN", "ruc": "",
         "telefono": "1", "vend": None, "direccion1": "CALLE VIEJA 1"},
        {"id_cliente": 2, "cod": "BBB", "nombre": "GOMEZ ANA", "ruc": "",
         "telefono": "1", "vend": None, "direccion1": "SE QUEDA COMO ESTA"},
    ]
    cap = _armar(monkeypatch, asinfo, pc)
    r = sa.sincronizar()
    assert len(cap["updates"]) == 1
    assert list(cap["updates"][0][1][1:]) == ["AAA", None, None, None, None,
                                              None, "BARRIO LA JOSEFINA LOTE 217", None, None]
    assert r["direcciones_cambiadas"] == 1
    assert r["dir_cambiado"] == [
        {"cod": "AAA", "antes": "CALLE VIEJA 1",
         "ahora": "BARRIO LA JOSEFINA LOTE 217"}
    ]


def test_direccion_igual_salvo_espacios_y_mayusculas_no_toca(monkeypatch):
    asinfo = [{"cod": "AAA", "ruc": "", "nombre": "PEREZ JUAN",
               "tel1": "", "tel2": "", "dir1": "Calle  Bolívar 12-15"}]
    pc = [{"id_cliente": 1, "cod": "AAA", "nombre": "PEREZ JUAN", "ruc": "",
           "telefono": "1", "vend": None, "direccion1": "CALLE BOLÍVAR 12-15"}]
    cap = _armar(monkeypatch, asinfo, pc)
    sa.sincronizar()
    assert cap["updates"] == []


def test_alta_nueva_viene_con_vendedor_y_direccion(monkeypatch):
    asinfo = [{"cod": "VA2", "ruc": "1721669206001", "nombre": "VELIZ LUIS",
               "tel1": "", "tel2": "", "lista_desc": "5%y7%",
               "agente": "V-FL1", "dir1": "AV SIEMPRE VIVA 742"}]
    cap = _armar(monkeypatch, asinfo, [])
    sa.sincronizar()
    assert cap["altas"][0]["vend"] == "FL1"
    assert cap["altas"][0]["direccion1"] == "AV SIEMPRE VIVA 742"


def test_alta_de_la_casa_nace_sin_vendedor(monkeypatch):
    asinfo = [{"cod": "VA2", "ruc": "1721669206001", "nombre": "VELIZ LUIS",
               "tel1": "", "tel2": "", "agente": "INT", "dir1": ""}]
    cap = _armar(monkeypatch, asinfo, [])
    sa.sincronizar()
    assert cap["altas"][0]["vend"] is None
    assert cap["altas"][0]["direccion1"] is None


def test_el_sync_pisa_provincia_y_canton_y_el_vacio_no_borra(monkeypatch):
    """27/08 (segunda pasada): provincia y cantón también vienen de Asinfo
    (ciudad → provincia). Lo de PC venía del dBase truncado a ~10 letras."""
    asinfo = [
        {"cod": "AAA", "ruc": "", "nombre": "PEREZ JUAN", "tel1": "", "tel2": "",
         "provincia": "PICHINCHA", "canton": "QUITO"},
        {"cod": "BBB", "ruc": "", "nombre": "GOMEZ ANA", "tel1": "", "tel2": "",
         "provincia": "", "canton": ""},               # Asinfo no sabe
    ]
    pc = [
        {"id_cliente": 1, "cod": "AAA", "nombre": "PEREZ JUAN", "ruc": "",
         "telefono": "1", "vend": None, "direccion1": "",
         "provincia": "PICHINCHA®", "canton": "QUITO    D"},
        {"id_cliente": 2, "cod": "BBB", "nombre": "GOMEZ ANA", "ruc": "",
         "telefono": "1", "vend": None, "direccion1": "",
         "provincia": "SE QUEDA", "canton": "SE QUEDA"},
    ]
    cap = _armar(monkeypatch, asinfo, pc)
    r = sa.sincronizar()
    assert len(cap["updates"]) == 1
    assert list(cap["updates"][0][1][1:]) == ["AAA", None, None, None, None,
                                              None, None, "PICHINCHA", "QUITO"]
    assert r["geo_cambiadas"] == 2
    assert r["geo_cambiado"] == [
        {"cod": "AAA", "campo": "canton", "antes": "QUITO    D", "ahora": "QUITO"},
        {"cod": "AAA", "campo": "provincia", "antes": "PICHINCHA®",
         "ahora": "PICHINCHA"},
    ]


def test_provincia_igual_salvo_espacios_no_toca(monkeypatch):
    asinfo = [{"cod": "AAA", "ruc": "", "nombre": "PEREZ JUAN", "tel1": "",
               "tel2": "", "provincia": "Santo Domingo", "canton": "quito"}]
    pc = [{"id_cliente": 1, "cod": "AAA", "nombre": "PEREZ JUAN", "ruc": "",
           "telefono": "1", "vend": None, "direccion1": "",
           "provincia": "SANTO  DOMINGO", "canton": "QUITO"}]
    cap = _armar(monkeypatch, asinfo, pc)
    sa.sincronizar()
    assert cap["updates"] == []


def test_alta_nueva_viene_con_provincia_y_canton(monkeypatch):
    asinfo = [{"cod": "VA2", "ruc": "1721669206001", "nombre": "VELIZ LUIS",
               "tel1": "", "tel2": "", "provincia": "TUNGURAHUA",
               "canton": "AMBATO"}]
    cap = _armar(monkeypatch, asinfo, [])
    sa.sincronizar()
    assert cap["altas"][0]["provincia"] == "TUNGURAHUA"
    assert cap["altas"][0]["canton"] == "AMBATO"


# ─── el hook del auto-create en la carga de facturas ────────────────────────

def test_auto_create_de_facturas_deja_campanita(monkeypatch):
    """El otro camino de alta (factura de código desconocido) también avisa,
    con la MISMA clave idempotente que el sync — no se duplican avisos."""
    from modules.facturas import views as fv

    avisos = []
    monkeypatch.setattr(db, "fetch_one", lambda *a, **k: None)
    monkeypatch.setattr(db, "fetch_all", lambda *a, **k: [])
    monkeypatch.setattr(db, "execute", lambda *a, **k: 1)

    from modules.avisos import queries as av_q
    monkeypatch.setattr(av_q, "avisar", lambda **kw: avisos.append(kw) or True)

    from modules.asinfo import service as asvc
    monkeypatch.setattr(asvc, "cliente_ficha",
                        lambda cods: {"ZZZ": {"nombre": "NUEVO SA", "ruc": "1799999999001"}})

    cod, creado = fv._resolver_cliente_asinfo("ZZZ", "tamara")
    assert (cod, creado) == ("ZZZ", True)
    assert avisos and avisos[0]["clave"] == "cliente-nuevo-ZZZ"
    assert "cupo y descuento" in avisos[0]["titulo"]


# ─── correr_si_toca: cada hora de 07 a 19 EC, sin cron del EC2 ─────────────
# (Hasta el 27/08/2026 las ventanas eran dos, 11:00 y 16:00 EC; Tamara pidió
# corridas más seguidas porque en Asinfo cambian dirección y vendedor.)

def _reset_freno(monkeypatch):
    monkeypatch.setattr(sa, "_auto_ultimo_check", 0.0)


def test_ventana_utc():
    from datetime import datetime
    def mk(d, h, m=0):
        return datetime(2026, 8, d, h, m)

    assert sa._inicio_ventana_utc(mk(5, 11, 59)) is None       # 06:59 EC: nada
    assert sa._inicio_ventana_utc(mk(5, 12, 0)) == mk(5, 12)   # 07:00 EC → ventana 07
    assert sa._inicio_ventana_utc(mk(5, 16, 5)) == mk(5, 16)   # 11:05 EC → ventana 11
    assert sa._inicio_ventana_utc(mk(5, 20, 59)) == mk(5, 20)  # 15:59 EC → ventana 15
    assert sa._inicio_ventana_utc(mk(5, 23, 59)) == mk(5, 23)  # 18:59 EC → ventana 18
    # 19:xx EC ya es el día siguiente en UTC — el caso que rompía el
    # replace(hour=...) sobre la hora UTC
    assert sa._inicio_ventana_utc(mk(6, 0, 30)) == mk(6, 0)    # 19:30 EC → ventana 19
    assert sa._inicio_ventana_utc(mk(6, 1, 0)) is None         # 20:00 EC: nada


def test_correr_si_toca_respeta_ventana_y_log(monkeypatch):
    _reset_freno(monkeypatch)
    corridas = []
    monkeypatch.setattr(sa, "sincronizar", lambda usuario: corridas.append(usuario) or {"ok": True})
    monkeypatch.setattr(sa, "asegurar_tabla", lambda: None)
    monkeypatch.setattr(db, "fetch_one", lambda *a, **k: None)  # no corrió aún

    from datetime import datetime, timezone

    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 5, 16, 10, tzinfo=UTC)  # 11:10 EC

    monkeypatch.setattr(sa, "datetime", _FakeDT)
    r = sa.correr_si_toca()
    assert r["corrio"] is True and corridas == ["auto-sync-clientes"]

    # segunda pasada: el log ya tiene una corrida en la ventana → no repite
    _reset_freno(monkeypatch)
    monkeypatch.setattr(db, "fetch_one", lambda *a, **k: {"x": 1})
    r2 = sa.correr_si_toca()
    assert r2["corrio"] is False and len(corridas) == 1


def test_correr_si_toca_fuera_de_ventana_no_corre(monkeypatch):
    _reset_freno(monkeypatch)
    corridas = []
    monkeypatch.setattr(sa, "sincronizar", lambda usuario: corridas.append(usuario))

    from datetime import datetime, timezone

    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 5, 11, 0, tzinfo=UTC)  # 06:00 EC

    monkeypatch.setattr(sa, "datetime", _FakeDT)
    assert sa.correr_si_toca()["corrio"] is False and corridas == []


def test_correr_si_toca_apagado(monkeypatch):
    _reset_freno(monkeypatch)
    monkeypatch.setenv("SYNC_CLIENTES_AUTO", "0")
    assert sa.correr_si_toca()["corrio"] is False


def test_pct_formato_ecuador():
    assert sa._pct(7.0) == "7%"
    assert sa._pct(7.5) == "7,5%"
    assert sa._pct(None) == "0%"
    assert sa._n_clientes(3) == "3 clientes"
