"""Si la carga de tejeduría no puede leer el mes en curso, avisa (TMT 2026-10-01).

El día 1 la carga se salteó el mes en cada vuelta sin decir nada."""
from datetime import date
from unittest.mock import patch

from modules.avisos import queries as av
from modules.tejeduria_asinfo import service as svc


def test_dos_vueltas_sin_leer_avisan_y_al_volver_se_da_vuelta():
    svc._sin_lectura_seguidas = 0
    hoy = date(2026, 10, 1)
    with patch.object(av, "avisar") as avisar, \
         patch.object(av, "abiertos_por_clave", return_value=[{"id_aviso": 7}]), \
         patch.object(av, "resolver") as resolver:
        svc._vigilar_lectura(True, hoy)
        assert not avisar.called          # una vuelta sola puede ser un bache
        svc._vigilar_lectura(True, hoy)
        assert avisar.call_count == 1
        assert avisar.call_args.kwargs["clave"] == "tejeduria:sin-asinfo:2026-10-01"
        assert avisar.call_args.kwargs["nivel"] == "alerta"
        svc._vigilar_lectura(False, hoy)
        resolver.assert_called_once()
        assert resolver.call_args.args[0] == 7
    assert svc._sin_lectura_seguidas == 0
