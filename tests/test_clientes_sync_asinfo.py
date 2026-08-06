"""Tests del sync de clientes Asinfo → PC (modules/clientes/sync_asinfo).

Sin Postgres ni Metabase: `traer_asinfo` y las funciones de `db` se mockean.
Las reglas que se prueban son las decisiones de la dueña del 05/08/2026:
pisar nombre/RUC, teléfono sólo rellena vacíos con valores reales, cupo y
descuento jamás, alta nueva → campanita, RUC repetido → conflicto sin alta.
"""
from __future__ import annotations

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
            return len(params or []) // 4  # aprox: filas del VALUES
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
    # cupo y descuento NUNCA aparecen en el UPDATE — es la regla madre
    assert "cupo" not in sql and "descuento" not in sql and "vend" not in sql
    assert "correo" not in sql and "direccion" not in sql
    # una sola fila cambia: AAA con nombre, ruc y teléfono
    assert params[0] == "sync-asinfo"
    assert list(params[1:]) == ["AAA", "PEREZ JUAN", "1712345678001", "0984042960"]
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
    assert aviso["url"] == "/clientes?q=VA2"


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
