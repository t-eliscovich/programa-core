from unittest.mock import patch
from modules._lib import formulas_memos
from modules.pedidos import service
from tests.test_pedidos_memos_2026_08_27 import (  # noqa: F401
    _FILAS, _VENDEDORES, _fake_asinfo, _login_vendedor, _sin_cache,
)

def test_preview_portal(app, fake_db):
    c = _login_vendedor(app, fake_db, vend="RMY")
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo), \
         patch.object(service, "mapa_vendedores", return_value=_VENDEDORES), \
         patch.object(formulas_memos, "estados", return_value={"PDCL-26401": {"estado": "terminado", "en_proceso_por": "jonathan"}}), \
         patch.object(service, "produccion_por_pedido", return_value={}):
        r = c.get("/mi-cartera/pedidos")
    assert r.status_code == 200
    open("/sessions/affectionate-admiring-franklin/mnt/outputs/PREVIEW-micartera-pedidos.html", "wb").write(r.data)
