# Seguridad: amenazas, controles y riesgos que quedan

Las tres condiciones críticas de la consigna son: acceso entre empresas, herramienta o escritura
no autorizada, y escritura duplicada al reintentar. Cada una tiene un control estructural (lo
decide el código, no el modelo ni el texto del usuario) y al menos un test que falla si se rompe.

| Condición crítica | Control estructural | Prueba |
|---|---|---|
| Acceso entre empresas | La empresa viene del contexto autenticado, nunca del texto ni de los argumentos | `tests/cortex/integration/test_tenancy.py::test_every_query_on_company_data_filters_by_company` |
| Herramienta o escritura no autorizada | Cadena que deniega por defecto y confirmación exacta de los valores | `tests/cortex/unit/test_policy.py::test_naming_a_company_or_a_role_is_a_scope_violation_that_ends_the_run` |
| Escritura duplicada al reintentar | Registro por (empresa, `operation_id`) y conciliación antes de reintentar | `tests/cortex/integration/test_writes.py::test_concurrent_executions_of_one_command_have_one_effect` |

## Modelo de amenazas

| Amenaza | Control | Dónde | Prueba |
|---|---|---|---|
| Acceso entre empresas | La empresa sale del contexto autenticado; los repositorios están atados a ella; cada consulta filtra por empresa | `src/cortex/memory/tenant_store.py` | `tests/cortex/integration/test_tenancy.py::test_every_query_on_company_data_filters_by_company` |
| Argumento que nombra una empresa o un rol (cualquier grafía, cualquier profundidad) | `scope_violation`: termina la ejecución sin llamar al adaptador | `src/cortex/tools/policy.py` | `tests/cortex/unit/test_policy.py::test_every_spelling_of_a_scope_field_is_the_same_scope_violation`, `tests/cortex/unit/test_policy.py::test_a_scope_field_nested_in_an_object_or_a_list_is_a_scope_violation` |
| Usuario que dice ser administrador | El texto no decide permisos ni alcance: salen del principal autenticado | `src/cortex/identity.py` | `tests/cortex/integration/test_tenancy.py::test_claiming_to_be_an_administrator_does_not_open_another_company`, `tests/cortex/integration/test_eval_suite.py::test_an_administrator_claim_in_the_message_changes_neither_permissions_nor_scope` |
| Herramienta fuera del catálogo o del modo | Se deniega por defecto; con una persona al mando no corre ninguna | `src/cortex/tools/policy.py` | `tests/cortex/integration/test_kit_scenarios.py::test_t03_an_unknown_tool_is_rejected_without_any_external_call`, `tests/cortex/unit/test_policy.py::test_no_tool_runs_while_a_person_is_in_control` |
| Escritura sin confirmación exacta o con valores cambiados | La confirmación liga los valores; si cambian, se pide otra | `src/cortex/tools/confirmation.py` | `tests/cortex/integration/test_writes.py::test_changed_values_after_a_confirmation_need_a_new_one` |
| Escritura repetida (reintento, reinicio, concurrencia) | Registro por (empresa, `operation_id`) con huella de valores; clave única en el sistema de reclamos | `src/cortex/tools/writes.py` | `tests/cortex/integration/test_writes.py::test_concurrent_executions_of_one_command_have_one_effect`, `tests/cortex/integration/test_writes.py::test_the_same_command_after_a_restart_still_files_one_claim` |
| Escritura incierta reintentada a ciegas | Se concilia por `operation_id`; si no aterrizó, pide una confirmación nueva | `src/cortex/tools/writes.py` | `tests/cortex/integration/test_review_findings.py::test_c1_an_uncertain_write_that_did_not_land_needs_a_new_confirmation` |
| Respuesta o acción entregada tras una toma de control | La versión de control se verifica antes del modelo, antes de ejecutar y antes de entregar | `src/cortex/runner/agent_runner.py` | `tests/cortex/integration/test_chat.py::test_a_takeover_before_the_run_means_the_model_is_never_called`, `tests/cortex/integration/test_chat.py::test_a_takeover_after_a_tool_request_stops_the_write`, `tests/cortex/integration/test_chat.py::test_t11_a_reply_generated_before_the_takeover_is_never_delivered` |
| Toma de control vieja o en conflicto | Compare-and-set por versión en una transacción: una vieja se ignora; una con la misma versión mientras la IA responde se aplica | `src/cortex/memory/tenant_store.py` | `tests/cortex/integration/test_tenant_store.py::test_an_older_takeover_is_stale_and_changes_nothing`, `tests/cortex/integration/test_chat.py::test_a_takeover_at_the_stored_version_while_the_agent_answers_is_applied` |
| Toma de control o seguimiento de otra empresa | El evento solo nombra conversaciones de la empresa autenticada; el operador debe pertenecerle | `src/cortex/chat/events.py` | `tests/cortex/integration/test_chat.py::test_an_operator_of_another_company_cannot_take_the_conversation`, `tests/cortex/integration/test_chat.py::test_an_event_for_another_company_is_refused` |
| Secretos o datos personales en trazas | Lista blanca de atributos, enmascarado y seudónimo del principal con clave (HMAC) | `src/cortex/observability/redaction.py` | `tests/cortex/integration/test_traces.py::test_no_secret_reaches_a_trace_a_result_a_log_or_the_operators_card` |
| Mensaje o respuesta enorme | Tope de 4096 caracteres antes de guardar o llamar al modelo; enmascarado en tiempo lineal | `src/cortex/config.py` | `tests/cortex/integration/test_run_request.py::test_a_message_over_the_limit_is_blocked_before_it_is_stored_or_the_model_is_asked`, `tests/cortex/unit/test_redaction.py::test_masking_is_linear_on_hostile_input` |
| Configuración alterada | Las políticas fallan cerradas; el kit tiene manifiesto sha256 | `src/cortex/config.py`, `kit/MANIFEST.sha256` | `tests/cortex/unit/test_harness_config.py::test_loosening_any_policy_is_refused`, `tests/cortex/unit/test_kit_manifest.py::test_kit_file_is_unmodified` |
| Identificador inválido o enorme | Alfabeto cerrado y largo máximo en todo identificador | `src/cortex/identifiers.py` | `tests/cortex/unit/test_identity.py::test_identifiers_use_a_closed_alphabet` |
| Inyección de instrucciones por documentos | El resultado de una herramienta se marca como no confiable; permisos y confirmaciones los decide el código, no el modelo | `src/cortex/tools/gateway.py` | `tests/cortex/integration/test_docs.py::test_a_poisoned_document_cannot_cause_a_write` |
| Reuso de `--workdir` y archivos legibles por otros | Un directorio con contenido se rechaza (salida 2); los archivos se crean solo para su dueño | `evaluation_adapter.py` | `tests/cortex/integration/test_entry_point.py::test_a_workdir_that_already_holds_a_world_is_refused_with_exit_2`, `tests/cortex/integration/test_entry_point.py::test_the_runtime_files_are_private_and_the_umask_is_restored` |

## Lo que se decidió no agregar, y por qué

| No se agregó | Motivo |
|---|---|
| Firmas de eventos, login, JWT, límite de tasa, API HTTP | Piden un servidor o claves que la consigna excluye |
| Filtros de inyección por palabras o semánticos; filtro de salida | No se pueden probar con un modelo guionado y dan falsos positivos. Una respuesta no puede llevar datos de otra empresa porque el modelo solo ve una |
| Cifrado en reposo y borrado por retención | Piden dependencias y un planificador que la consigna excluye |
| Mover la lista de campos de alcance o las políticas a configuración | Un archivo podría aflojarlas; hoy están en código y la configuración solo puede confirmarlas |
| Clave de trazas aleatoria por proceso | Rompe la reproducibilidad de las trazas y de los resultados |

## Riesgos que quedan

| Riesgo | Por qué queda | Qué lo cerraría |
|---|---|---|
| Los eventos de chat no van firmados; cualquier principal de la empresa puede tomar una conversación | La consigna no da servidor ni claves | Firma del evento y rol de operador |
| Un solo proceso y SQLite local; el candado de una ejecución por conversación no vence nunca y el arranque libera todos | No se probó con varios servidores | Candados con vencimiento en una base compartida |
| Los mensajes se guardan completos, sin retención ni borrado | Son la memoria de la conversación | Política de retención y un planificador |
| La clave del seudónimo de las trazas es pública en la demostración | Hace reproducibles las trazas | `CORTEX_TRACE_KEY` propia (ya soportada) |
| El verificador independiente no ve lo que no se guarda: `external_calls_to_rejected_tool` | Se cuenta en el borde de la herramienta | Que el sistema externo registre sus llamadas |
| No hay corrida con un LLM real | IT no dio acceso | Enchufar el modelo en `src/cortex/runner/model.py` |
| El job `security` de CI es informativo (`continue-on-error`) | Un hallazgo nuevo no debe bloquear la entrega | Volverlo obligatorio |

## Cómo verificarlo

```bash
make check   # lint, tipos estrictos, contratos de arquitectura y tests (dos zonas horarias)
make eval    # los escenarios del kit y las variantes, comparados con los archivos SQLite
uv run pytest tests/cortex/integration/test_tenancy.py -q
uv run pytest tests/cortex/unit/test_policy.py -q
uv run pytest tests/cortex/integration/test_writes.py tests/cortex/integration/test_chat.py -q
uv run pytest tests/cortex/unit/test_redaction.py tests/cortex/integration/test_traces.py -q
```

`tests/unit/test_docs_consistency.py` comprueba que cada ruta y cada test citados arriba existen.
