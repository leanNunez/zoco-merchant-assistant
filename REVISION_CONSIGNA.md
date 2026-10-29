# Revisión de la consigna: trampas, contradicciones y decisiones

Se leyeron el PDF, el contrato de evaluación y los fixtures del kit buscando lo que se puede
resolver mal sin darse cuenta. Cada fila dice qué pasa, qué se decidió y qué prueba lo respalda.
La fuente de verdad es el PDF (`kit/README_KIT.md` lo dice); donde el kit lo contradice, manda el PDF.

Los 13 `examples/` son idénticos a `kit/scenarios.json`, y los dos PDF recibidos tienen el mismo contenido.

## A. Lo que puede descalificar una entrega

| Qué pasa | Decisión | Prueba |
|---|---|---|
| El `operation_id` lo manda el modelo: con unicidad global, el de una empresa choca con el de otra | Clave (empresa, `operation_id`) y huella de los valores; el mismo id con otros valores se rechaza | `tests/cortex/integration/test_writes.py::test_an_operation_id_reused_with_other_values_is_refused` |
| T05 pasa `tenant_id` como argumento de la herramienta | Nombrar empresa o rol en los argumentos termina la ejecución como `scope_violation`, sin llamar al adaptador | `tests/cortex/unit/test_policy.py::test_naming_a_company_or_a_role_is_a_scope_violation_that_ends_the_run` |
| Un timeout de lectura es transitorio; el de una escritura es incierto (pudo grabarse) | Toda escritura con timeout se busca por su `operation_id` antes de reintentar. T01 a T13 no lo cubren, pero `kit/fixtures/faults.json` lo describe | `tests/cortex/integration/test_failures.py::test_a_plain_write_timeout_is_looked_up_and_attempted_once_more` |
| La idempotencia debe valer "aunque se reinicie el proceso" y con ejecuciones concurrentes | Registro de operaciones en SQLite y clave única en el sistema de reclamos | `tests/cortex/integration/test_writes.py::test_concurrent_executions_of_one_command_have_one_effect` |
| `harness_config.json` expone las políticas de seguridad como flags | No son interruptores: una configuración que afloja una (o trae una clave desconocida) se rechaza | `tests/cortex/unit/test_harness_config.py::test_loosening_any_policy_is_refused` |
| "Aunque el usuario afirme ser administrador": los fixtures no tienen rol de administrador | Los permisos salen solo del principal autenticado, que además debe pertenecer a la empresa | `tests/cortex/integration/test_kit_scenarios.py::test_a_principal_of_another_company_is_refused_before_anything_runs` |

## B. Señuelos dentro de los escenarios

| Qué pasa | Decisión | Prueba |
|---|---|---|
| T10 a T13 traen `{"final": "OK"}` en el guion; enviarlo es el error esperado | En HUMAN no se llama al modelo; el seguimiento lo decide el código; si hay toma de control, la respuesta se suprime | `tests/cortex/integration/test_chat.py::test_follow_ups_are_decided_by_code_and_send_nothing` |
| T08 trae `max_steps: 3`, que pisa el valor por defecto (6) | La salida informa el límite aplicado | `tests/cortex/integration/test_kit_scenarios.py::test_t08_the_scenario_step_limit_overrides_the_default` |
| T07 espera una sola ejecución y `retry_count: 1` | Los reintentos no suman pasos ni se cuentan como ejecuciones | `tests/cortex/integration/test_failures.py::test_t07_a_transient_read_failure_is_retried_once_and_counted_once` |
| T13 mezcla "completó el alta" y "pidió la baja": no distingue qué regla cancela | Cada regla se prueba por separado y se registran todos los motivos que aplican | `tests/cortex/unit/test_followup.py::test_each_case_of_the_kit` |
| Ningún fixture produce `CONTACTAR` | Se prueba el caso por defecto de la tabla de decisión | `tests/cortex/unit/test_followup.py::test_each_case_of_the_kit` |
| E03 (reprogramar al 2026-11-06) depende de la fecha de hoy y el evento no la trae | Reloj fijo en la evaluación (`--now` lo cambia) | `tests/cortex/integration/test_chat.py::test_the_rescheduling_depends_on_the_fixed_clock_not_on_the_day_it_runs` |

## C. Contradicciones entre documentos

| Qué pasa | Decisión | Prueba |
|---|---|---|
| El PDF pide bloquear "respuestas y acciones"; la política del kit solo habla de la salida | Manda el PDF: tras una toma de control no se ejecuta ni una escritura ya solicitada | `tests/cortex/integration/test_chat.py::test_a_takeover_after_a_tool_request_stops_the_write` |
| El PDF da motivos de fin "por ejemplo"; el contrato los da como lista cerrada | Se usan exactamente los cinco; la causa fina va en un campo aparte (`finish_detail`) | `tests/cortex/unit/test_finish.py::test_every_detail_implies_exactly_one_of_the_five_reasons` |
| El ejemplo del contrato muestra una respuesta final con `customer_messages_sent: 0`, pero T12 y T13 usan ese campo para detectar que se envió el "OK" | Una respuesta entregada cuenta como mensaje al cliente; el ejemplo es ilustrativo | `tests/cortex/integration/test_kit_scenarios.py::test_t01_a_plain_answer_completes_in_one_step` |
| En la primera entrega un cambio de control invalida lo pendiente; T10 pide que el pendiente se conserve | Se conserva visible para el operador (`SUSPENDED`) pero no se puede ejecutar | `tests/cortex/integration/test_chat.py::test_t10_a_restart_keeps_the_human_in_control_and_the_pending_action` |
| Una herramienta pide confirmación y la política global también | Gana la más estricta: toda escritura se confirma | `tests/cortex/unit/test_catalog.py::test_a_write_tool_without_confirmation_cannot_even_be_declared` |
| Un mismo dato con nombres distintos: `initial_state`, `current_state`, `conversation_mode`; `operator_took_over` y `operator_takeover`; `pending_action.type: "reclamo"` y `crear_reclamo` | El adaptador traduce y falla en voz alta ante una clave que no conoce | `tests/cortex/integration/test_kit_scenarios.py::test_an_unknown_or_malformed_scenario_fails_loudly` |
| El mock responde "política de devoluciones" con DOC-03, pero en el corpus real DOC-03 es otro tema | No se compara con el mock; las pruebas usan el corpus real | `tests/cortex/integration/test_docs.py::test_a_company_finds_its_own_document_with_its_validity` |

## D. Ambigüedades que hubo que decidir

| Qué no dice la consigna | Decisión | Prueba |
|---|---|---|
| Qué es un "paso" | Una llamada al modelo (el ejemplo del contrato da `step_count: 1` con una sola respuesta) | `tests/cortex/integration/test_kit_scenarios.py::test_t01_a_plain_answer_completes_in_one_step` |
| Dónde busca IT las claves de `expected` (`executed_tools`, `retry_count`...), que no están en la salida mínima | Todas se emiten siempre, calculadas desde la base y la traza, nunca copiadas de `expected` | `tests/cortex/integration/test_kit_scenarios.py::test_the_contract_fields_are_always_present` |
| Qué significa "respetando permisos" en un seguimiento (los fixtures no traen permisos) | El permiso es el consentimiento de contacto; si no se sabe, no se contacta | `tests/cortex/unit/test_followup.py::test_unknown_consent_never_allows_contact` |
| A qué empresa pertenece el corpus de documentos | Comercio A usa el de la primera entrega; Comercio B no tiene y nunca ve el de A | `tests/cortex/integration/test_docs.py::test_a_company_without_documents_gets_nothing_never_somebody_elses` |
| Cómo reiniciar el proceso en T10 | El adaptador arma objetos nuevos sobre los mismos archivos; otro test lo repite con procesos reales | `tests/cortex/integration/test_chat.py::test_three_separate_processes_file_one_claim` |

## E. Por qué no se reutilizó el motor de la primera entrega tal cual

La consigna pide reutilizar los componentes; varios del motor anterior hacen hoy lo que está prohibido.

| Qué hacía | Decisión | Prueba |
|---|---|---|
| Tomar el control exigía horario de atención y, al cerrar, devolvía la conversación al agente: T10 y T11 habrían dependido de la hora | La toma de control es un hecho informado por Zoco Chat; no hay guarda de horario | `tests/cortex/integration/test_chat.py::test_the_takeover_scenarios_do_not_depend_on_the_time_of_day` |
| El ejecutor reintentaba las escrituras a ciegas | Las escrituras no pasan por los reintentos: se buscan por su id | `tests/cortex/unit/test_retry.py::test_only_transient_failures_are_ever_retried` |
| La clave de idempotencia salía del texto y la conversación, sin empresa | Sale de la empresa y el `operation_id` | `tests/cortex/integration/test_tenancy.py::test_every_query_on_company_data_filters_by_company` |
| El estado vivía en memoria y la confirmación no se podía serializar | Todo en SQLite, con una acción pendiente propia | `tests/cortex/integration/test_writes.py::test_the_same_command_after_a_restart_still_files_one_claim` |
| Había estado global (registro de herramientas, contador de ids, generador aleatorio) | Nada global: lo prohíbe un test sobre el código | `tests/cortex/unit/test_harness_invariants.py::test_no_module_holds_mutable_global_state` |

Se reutilizan por importación los componentes puros: el contrato de fechas y el modelo de ventas,
los argumentos estrictos y la guarda de argumentos, la huella de los valores, el repositorio de
reclamos, el buscador BM25 y las capacidades por modo. Un contrato de arquitectura impide importar
el motor de turnos, la consola o el armado de la primera entrega.

## F. Preguntas que se le podrían hacer a IT

Ninguna bloquea el trabajo (todo quedó decidido arriba), pero confirmarlas quitaría supuestos:

1. ¿`customer_messages_sent` cuenta las respuestas de la IA entregadas, como en T12 y T13?
2. ¿Qué motivo de fin corresponde a una escritura que espera la confirmación del usuario?
   Aquí es `NEEDS_HUMAN` con `finish_detail: awaiting_confirmation`.
