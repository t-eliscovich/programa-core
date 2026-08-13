# Tests que dependen del ORDEN en que corren

Medido el 2026-08-13, sobre `main`, corriendo la suite en orden aleatorio con
cuatro semillas distintas:

```
pytest -m "not db" -n 4 --dist worksteal --randomly-seed=<SEMILLA>
```

(`pytest-randomly` no está en `requirements.txt` a propósito: instalarlo alcanza
para que shufflee por defecto y eso volvería no determinista el CI de todos los
días. Se instala a mano cuando se quiere hacer esta prueba.)

**Estos 50 tests pasan o fallan según qué corrió antes.** No es que estén rotos:
en el orden de siempre pasan. Pero eso significa que hoy no están probando lo
que dicen probar — están apoyados en algo que les dejó puesto otro test.

Casi todos son de PERMISOS y LOGIN ("sin permiso → 404", "requiere login →
302"), que es donde un verde equivocado hace más daño: el test sigue en verde
aunque la pantalla se haya quedado abierta para todo el mundo.

## Por qué importa ahora

El CI corre con `--dist loadfile`, que mantiene junto todo lo de un mismo
archivo. Eso tapa la mayoría de estas dependencias (37 de las 50 están dentro de
`test_mi_cartera.py`). Si alguien cambia a `worksteal` o a `load`, empiezan a
aparecer rojos que no corresponden a ningún bug.

## Cómo atacarlo

Por archivo, no de a un test. 37 de 50 son un solo archivo: casi seguro es UNA
causa. El síntoma testigo, para reproducir:

```
tests/test_precios_imprimir.py:139: test_la_hoja_requiere_login
    assert resp.status_code in (302, 401)
E   assert 200 in (302, 401)      # un request ANÓNIMO devolvió 200
```

## Uno que sigue apareciendo con `loadfile`

`tests/test_flujo_produccion_costo.py::test_hilado_cierra_por_movimiento_de_bodega_y_baja_ukg`

El 13/08 se le arregló la causa principal (el mock mandaba la forma vieja del
dict, sin `kg_con_costo`, y eso lo hacía tomar la rama del guard de asimetría en
vez de la del promedio ponderado). Con eso pasó de caerse casi siempre a
caerse **1 de cada 10 corridas**, incluso con `loadfile`. O sea que queda otro
canal de contagio sin identificar. Está anotado acá para que no se dé por
cerrado: es el mismo pecado de la lista de abajo.

## La lista

- `tests/test_anticipos_a_dolares.py::test_alta_anticipo_sin_permiso_escritura_404`
- `tests/test_anticipos_a_dolares.py::test_cancelar_anticipo_sin_permiso_escritura_404`
- `tests/test_anticipos_a_dolares.py::test_dolares_lista_sin_permiso_404`
- `tests/test_auth.py::test_requiere_login_redirects`
- `tests/test_dia_explicacion.py::test_sin_permiso_la_pantalla_no_existe`
- `tests/test_dolares_rework.py::test_convertir_seleccion_sin_permiso_404`
- `tests/test_mi_cartera.py::test_cambiar_de_pestana_no_te_saca_del_mes_que_estabas_mirando`
- `tests/test_mi_cartera.py::test_cheque_sin_numero_no_queda_pelado`
- `tests/test_mi_cartera.py::test_el_buscador_filtra_al_tipear_sin_volver_al_servidor`
- `tests/test_mi_cartera.py::test_el_buscador_vive_en_el_appbar_para_no_irse_con_el_scroll`
- `tests/test_mi_cartera.py::test_el_cliente_bloqueado_se_avisa_arriba_de_todo`
- `tests/test_mi_cartera.py::test_el_cuadradito_de_la_lista_dice_el_CODIGO_de_3_letras`
- `tests/test_mi_cartera.py::test_el_desglose_de_la_comision_dice_el_CODIGO_de_3_letras`
- `tests/test_mi_cartera.py::test_el_encabezado_cuenta_lo_que_se_ve_no_lo_que_se_cargo`
- `tests/test_mi_cartera.py::test_el_filtro_al_tipear_mira_lo_mismo_que_el_del_servidor`
- `tests/test_mi_cartera.py::test_el_portal_solo_lista_los_cheques_en_cartera`
- `tests/test_mi_cartera.py::test_el_redondel_de_la_cuenta_dice_el_codigo_del_vendedor`
- `tests/test_mi_cartera.py::test_el_rotulo_dice_lo_que_se_esta_mostrando`
- `tests/test_mi_cartera.py::test_el_vendedor_no_llega_a_las_pantallas_del_programa`
- `tests/test_mi_cartera.py::test_el_vendedor_puede_cerrar_sesion`
- `tests/test_mi_cartera.py::test_enter_no_recarga_la_pantalla_ya_filtrada`
- `tests/test_mi_cartera.py::test_imprimir_todos_usa_el_template_de_la_oficina`
- `tests/test_mi_cartera.py::test_la_comision_abre_en_el_mes_con_las_dos_pestanas_arriba`
- `tests/test_mi_cartera.py::test_la_comision_no_deja_pedir_un_mes_futuro`
- `tests/test_mi_cartera.py::test_la_ficha_del_cliente_renderiza`
- `tests/test_mi_cartera.py::test_la_lista_de_clientes_sale_alfabetica_por_codigo`
- `tests/test_mi_cartera.py::test_la_pestana_mes_a_mes_muestra_el_ano_y_no_el_desglose`
- `tests/test_mi_cartera.py::test_la_raiz_lo_manda_a_su_portal`
- `tests/test_mi_cartera.py::test_la_salida_no_depende_de_javascript`
- `tests/test_mi_cartera.py::test_las_alertas_del_inicio_siguen_yendo_por_vencido`
- `tests/test_mi_cartera.py::test_las_pantallas_renderizan[/mi-cartera/clientes?f=vencidos]`
- `tests/test_mi_cartera.py::test_las_pantallas_renderizan[/mi-cartera/clientes?q=zzz]`
- `tests/test_mi_cartera.py::test_las_pantallas_renderizan[/mi-cartera/clientes]`
- `tests/test_mi_cartera.py::test_las_pantallas_renderizan[/mi-cartera/comision?periodo=anio]`
- `tests/test_mi_cartera.py::test_las_pantallas_renderizan[/mi-cartera/comision]`
- `tests/test_mi_cartera.py::test_las_pantallas_renderizan[/mi-cartera?periodo=anio]`
- `tests/test_mi_cartera.py::test_las_pantallas_renderizan[/mi-cartera?periodo=semana]`
- `tests/test_mi_cartera.py::test_las_pantallas_renderizan[/mi-cartera]`
- `tests/test_mi_cartera.py::test_los_cheques_en_cartera_OCUPAN_cupo`
- `tests/test_mi_cartera.py::test_los_meses_sin_comision_no_se_listan_pero_se_dicen`
- `tests/test_mi_cartera.py::test_pasado_el_cupo_el_porcentaje_se_pinta_de_rojo`
- `tests/test_mi_cartera.py::test_sin_cheques_en_cartera_no_dice_que_no_tiene_ninguno`
- `tests/test_mi_cartera.py::test_sin_cupo_cargado_dice_sin_cupo_asignado_y_no_cero`
- `tests/test_precios_colores_sin_clase.py::test_el_vendedor_no_puede_asignar_la_clase`
- `tests/test_precios_colores_sin_clase.py::test_el_vendedor_ve_la_lista_pero_no_los_botones`
- `tests/test_precios_imprimir.py::test_la_hoja_impresa_incluye_las_telas_de_precio_unico`
- `tests/test_precios_imprimir.py::test_la_hoja_requiere_login`
- `tests/test_routes_smoke.py::test_static_get_routes_render_without_500`
- `tests/test_sql_console_solo_lectura.py::test_sin_permiso_de_admin_la_consola_no_existe`
- `tests/test_tejeduria_asinfo.py::test_auto_carga_requiere_permiso_de_crear`
