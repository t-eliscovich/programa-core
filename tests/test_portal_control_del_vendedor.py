"""El vendedor ve quién entró al portal y le puede cortar el acceso.

TMT 2026-08-24. El cliente entra al portal con su código de 3 letras y su RUC,
que son los dos **públicos**. Se eligió a propósito no ponerle un chequeo
previo: le cargaría fricción al 100% de los clientes para atajar un caso raro.

⭐ **Este control es la otra mitad de esa decisión**, así que va en la v1 y no
después. Sin él, "alguien con una factura vieja puede entrar" no tiene
respuesta.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.mi_cartera import portal_cliente  # noqa: E402

VISTAS = (ROOT / "modules" / "mi_cartera" / "views.py").read_text(encoding="utf-8")
FICHA = (ROOT / "modules" / "mi_cartera" / "templates" / "mi_cartera"
         / "cliente.html").read_text(encoding="utf-8")


def test_el_boton_vive_en_la_ficha_del_cliente():
    """Donde el vendedor ya está parado. Una pantalla nueva no la abre nadie."""
    assert "portal-caja" in FICHA
    assert "Cortar el acceso" in FICHA
    assert "mi_cartera.portal_acceso" in FICHA


def test_no_se_dibuja_nada_si_el_cliente_nunca_entro():
    """'Todavía no entró' repetido en 400 fichas es ruido, no información."""
    assert "{% if portal is defined and portal.tiene %}" in FICHA


def test_cortar_se_puede_deshacer():
    """Un botón que corta y no sabe reabrir es una trampa: el vendedor que se
    equivoca deja al cliente afuera y sin salida."""
    assert "reabrir" in FICHA
    assert hasattr(portal_cliente, "reabrir")


def test_reabrir_tambien_destraba():
    """El que llama para que le reabran no tiene que quedar trabado además por
    los intentos fallidos de antes."""
    import inspect
    src = inspect.getsource(portal_cliente.reabrir)
    assert "intentos_fallidos = 0" in src
    assert "bloqueado_hasta = NULL" in src


def test_cortar_no_borra_la_fila():
    """Reversar no es eliminar: queda el rastro de que existió y de quién lo
    cortó, y el cliente que vuelve no pierde su historia."""
    import inspect
    src = inspect.getsource(portal_cliente.cortar)
    assert "DELETE" not in src.upper()
    assert "activo = false" in src
    assert "cortado_por" in src


def test_el_guard_del_vendedor_corre_ANTES_de_tocar_nada():
    """🚨 `_cargar_cliente` es el que verifica que el cliente sea SUYO. Sin
    llamarlo primero, tipear el código de un cliente ajeno en la barra de
    direcciones le cortaría el acceso a un cliente de otro vendedor."""
    bloque = VISTAS[VISTAS.index("def portal_acceso("):]
    bloque = bloque[:bloque.index("return redirect")]
    i_guard = bloque.index("_cargar_cliente(vend, codigo_cli)")
    i_corte = bloque.index("portal_cliente.")
    assert i_guard < i_corte, "se toca el acceso antes de verificar que el cliente es suyo"


def test_la_ruta_es_solo_POST():
    """Cortarle el acceso a alguien con un GET es un link que se dispara solo
    desde cualquier lado."""
    bloque = VISTAS[VISTAS.index('@mi_cartera_bp.route("/mi-cartera/cliente/<codigo_cli>/portal"'):]
    assert 'methods=["POST"]' in bloque[:200]


def test_leer_el_estado_nunca_tumba_la_ficha():
    """⚠ Fail-soft: si las tablas del portal todavía no existen —el deploy
    corre las migraciones, pero hay una ventana— la ficha con la que el
    vendedor trabaja todo el día tiene que abrir igual."""
    with patch.object(portal_cliente.db, "fetch_one",
                      side_effect=RuntimeError("no existe la tabla")):
        r = portal_cliente.estado("ATE")
    assert r["tiene"] is False


def test_sin_codigo_no_hace_nada():
    assert portal_cliente.estado("")["tiene"] is False
    assert portal_cliente.cortar("", "edg")[0] is False


def test_los_textos_estan_bien_escritos():
    """🚨 Se me escaparon los acentos escribiendo este bloque, porque venía de
    escribir el script de PowerShell en ASCII puro (ahí los acentos rompen el
    parseo). Salió "Ultima vez" y "todavia no eligio clave" en la pantalla de
    los vendedores.

    El ASCII es una regla de LOS `.ps1`, no de las pantallas: acá el castellano
    se escribe como se escribe. Ver el skill `textos-de-pantalla-intela`."""
    import re

    bloque = FICHA[FICHA.index("portal-caja"):FICHA.index("portal-btn") + 400]
    # Fuera los `{{ ... }}` y `{% ... %}`: adentro van NOMBRES DE VARIABLE, que
    # sí van sin acento (`portal.eligio_clave`). Lo que se revisa es lo que el
    # vendedor lee, no los identificadores.
    texto = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", "", bloque, flags=re.S)
    for mal, bien in (("Ultima vez", "Última vez"),
                      ("todavia", "todavía"),
                      ("eligio clave", "eligió clave"),
                      ("Se lo cerro", "Se lo cerró"),
                      ("lo cambio el", "lo cambió él")):
        assert mal not in texto, f"falta el acento: '{mal}' tendría que ser '{bien}'"
