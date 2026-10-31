# Revisión de la consigna: trampas, contradicciones y decisiones

Se leyeron el PDF, el contrato de evaluación y los fixtures del kit buscando lo que se puede
resolver mal sin darse cuenta. Cada fila dice qué pasa, qué se decidió y qué prueba lo respalda.
La fuente de verdad es el PDF (`kit/README_KIT.md` lo dice); donde el kit lo contradice, manda el PDF.

Los 13 `examples/` son idénticos a `kit/scenarios.json`, y los dos PDF recibidos tienen el mismo contenido.

## A. Lo que puede descalificar una entrega

| Qué pasa | Decisión | Prueba |
|---|---|---|
| El `operation_id` lo manda el modelo: con unicidad global, el de una empresa choca con el de otra | Clave (empresa, `operation_id`) y huella de los valores; el mismo id con otros valores se rechaza | `tests/cortex/integration/test_writes.py::test_an_operation_id_reused_with_other_values_is_refused`, `tests/cortex/integration/test_tenancy.py::test_the_same_operation_id_in_both_companies_files_one_claim_in_each` |
| T05 pasa `tenant_id` como argumento de la herramienta | Nombrar empresa o rol en los argumentos termina la ejecución como `scope_violation`, sin llamar al adaptador | `tests/cortex/unit/test_policy.py::test_naming_a_company_or_a_role_is_a_scope_violation_that_ends_the_run` |
| Un timeout de lectura es transitorio; el de una escritura es incierto (pudo grabarse) | Toda escritura con timeout se busca por su `operation_id` antes de reintentar. T01 a T13 no lo cubren, pero `kit/fixtures/faults.json` lo describe | `tests/cortex/integration/test_failures.py::test_a_plain_write_timeout_is_looked_up_and_attempted_once_more` |
| La idempotencia debe valer "aunque se reinicie el proceso" y con ejecuciones concurrentes | Registro de operaciones en SQLite y clave única en el sistema de reclamos | `tests/cortex/integration/test_writes.py::test_concurrent_executions_of_one_command_have_one_effect` |
| `harness_config.json` expone las políticas de seguridad como flags | No son interruptores: una configuración que afloja una (o trae una clave desconocida) se rechaza | `tests/cortex/unit/test_harness_config.py::test_loosening_any_policy_is_refused` |
| "Aunque el usuario afirme ser administrador": los fixtures no tienen rol de administrador | Los permisos salen solo del principal autenticado, que además debe pertenecer a la empresa. El texto del mensaje nunca decide el alcance: con la afirmación solo en el texto, la herramienta devuelve los datos de la empresa autenticada | `tests/cortex/integration/test_kit_scenarios.py::test_a_principal_of_another_company_is_refused_before_anything_runs`, `tests/cortex/integration/test_eval_suite.py::test_an_administrator_claim_in_the_message_changes_neither_permissions_nor_scope` |

## B. Señuelos dentro de los escenarios

| Qué pasa | Decisión | Prueba |
|---|---|---|
| T10 a T13 traen `{"final": "OK"}` en el guion; enviarlo es el error esperado | En HUMAN no se llama al modelo; el seguimiento lo decide el código. La toma de control se aplica justo antes de la primera entrega de cualquier respuesta (del modelo o del código, como la pregunta de confirmación o un rechazo) o, si la ejecución no entrega nada, justo después de ella; la respuesta se suprime | `tests/cortex/integration/test_chat.py::test_follow_ups_are_decided_by_code_and_send_nothing`, `tests/cortex/integration/test_chat.py::test_t10_a_restart_keeps_the_human_in_control_and_the_pending_action`, `tests/cortex/integration/test_chat.py::test_t11_a_reply_generated_before_the_takeover_is_never_delivered`, `tests/cortex/integration/test_chat.py::test_a_takeover_before_delivery_is_applied_whatever_the_run_delivers` |
| T08 trae `max_steps: 3`, que pisa el valor por defecto (6) | La salida informa el límite aplicado | `tests/cortex/integration/test_kit_scenarios.py::test_t08_the_scenario_step_limit_overrides_the_default` |
| T07 espera una sola ejecución y `retry_count: 1` | Los reintentos no suman pasos ni se cuentan como ejecuciones | `tests/cortex/integration/test_failures.py::test_t07_a_transient_read_failure_is_retried_once_and_counted_once` |
| T13 mezcla "completó el alta" y "pidió la baja": no distingue qué regla cancela | Cada regla se prueba por separado y se registran todos los motivos que aplican | `tests/cortex/unit/test_followup.py::test_each_case_of_the_kit` |
| Ningún fixture produce `CONTACTAR` | Se prueba el caso por defecto de la tabla de decisión | `tests/cortex/unit/test_followup.py::test_each_case_of_the_kit` |
| E03 (reprogramar al 2026-11-06) depende de la fecha de hoy y el evento no la trae | Reloj fijo en la evaluación (`--now` lo cambia) | `tests/cortex/integration/test_chat.py::test_the_rescheduling_depends_on_the_fixed_clock_not_on_the_day_it_runs` |
| DOC-12 es un archivo histórico (vigente hasta el 2026-08-31) que por palabras responde bien "comisión del plan pro" | La búsqueda aplica la política de vigencia de la primera entrega con la fecha del reloj inyectado; una consulta sobre un período pasado sigue encontrando lo vigente entonces | `tests/cortex/integration/test_docs.py::test_a_rule_that_is_no_longer_valid_is_not_returned` |

## C. Contradicciones entre documentos

| Qué pasa | Decisión | Prueba |
|---|---|---|
| El PDF pide bloquear "respuestas y acciones"; la política del kit solo habla de la salida | Manda el PDF: tras una toma de control no se ejecuta ni una escritura ya solicitada | `tests/cortex/integration/test_chat.py::test_a_takeover_after_a_tool_request_stops_the_write` |
| El PDF da motivos de fin "por ejemplo"; el contrato los da como lista cerrada | Se usan exactamente los cinco; la causa fina va en un campo aparte (`finish_detail`) | `tests/cortex/unit/test_finish.py::test_every_detail_implies_exactly_one_of_the_five_reasons` |
| El ejemplo del contrato muestra una respuesta entregada con `customer_messages_sent: 0`, y T12 y T13 esperan 0 en un seguimiento | El ejemplo es exactamente la salida de T01 (comercio_a, conv_t01, `COMPLETED`, un paso, `final_answer`): responder al mensaje del usuario no es contactar al cliente. `customer_messages_sent` cuenta todo lo enviado que no es la respuesta a un mensaje entrante de esa misma ejecución (un seguimiento, un mensaje proactivo), y vale 1 si un seguimiento llega a enviar algo; las respuestas entregadas van en `ai_replies_delivered`. El verificador recalcula ambas desde la tabla de salida | `tests/cortex/integration/test_kit_scenarios.py::test_t01_a_plain_answer_completes_in_one_step` |
| En la primera entrega un cambio de control invalida lo pendiente; T10 pide que el pendiente se conserve | Se conserva visible para el operador (`SUSPENDED`) pero no se puede ejecutar | `tests/cortex/integration/test_chat.py::test_t10_a_restart_keeps_the_human_in_control_and_the_pending_action` |
| El contrato pide `tool_rejected` o `tool_error` en un fallo o una autorización denegada | Una ejecución `LIMIT_REACHED` o `NEEDS_HUMAN` no recibe ninguno, porque no es un fallo ni una denegación en el sentido del contrato; una `BLOCKED` o `FAILED` siempre termina con uno | `tests/cortex/unit/test_finish.py::test_a_blocked_or_failed_run_without_a_tool_event_gets_exactly_one` |
| Una herramienta pide confirmación y la política global también | Gana la más estricta: toda escritura se confirma | `tests/cortex/unit/test_catalog.py::test_a_write_tool_without_confirmation_cannot_even_be_declared` |
| Un mismo dato con nombres distintos: `initial_state`, `current_state`, `conversation_mode`; `current_version` y `control_version`; `operator_took_over` y `operator_takeover`; `pending_action.type: "reclamo"` y `crear_reclamo` | El adaptador acepta todos los nombres, además de `tools` y `expected_control_version` (las entradas del motor en la sección 2 del PDF), y lee los valores de enumeraciones sin importar mayúsculas. Si dos nombres del mismo dato traen valores distintos, o aparece una clave desconocida, falla en voz alta | `tests/cortex/unit/test_preconditions.py::test_two_names_of_a_fact_that_disagree_are_refused` |
| El mock responde "política de devoluciones" con DOC-03, pero en el corpus real DOC-03 es otro tema | No se compara con el mock; las pruebas usan el corpus real | `tests/cortex/integration/test_docs.py::test_a_company_finds_its_own_document_with_its_validity` |

## D. Ambigüedades que hubo que decidir

| Qué no dice la consigna | Decisión | Prueba |
|---|---|---|
| Qué es un "paso" | Una llamada al modelo (el ejemplo del contrato da `step_count: 1` con una sola respuesta) | `tests/cortex/integration/test_kit_scenarios.py::test_t01_a_plain_answer_completes_in_one_step` |
| Dónde busca IT las claves de `expected` (`executed_tools`, `retry_count`...), que no están en la salida mínima | Todas se emiten siempre, en la salida de cada escenario del kit, calculadas desde la base y la traza, nunca copiadas de `expected`. El verificador las recalcula por su cuenta desde los archivos SQLite, salvo las llamadas contadas en el borde de la herramienta | `tests/cortex/integration/test_kit_scenarios.py::test_the_contract_fields_are_always_present` |
| Qué significa "respetando permisos" en un seguimiento (los fixtures no traen permisos) | El permiso es el consentimiento de contacto. Si no se sabe, el seguimiento pasa a tarea interna (`CONSENT_UNKNOWN`) y no se contacta; el consentimiento se mira antes de reprogramar, así que tampoco se reprograma | `tests/cortex/unit/test_followup.py::test_unknown_consent_never_allows_contact` |
| Qué hacer con un estado de oportunidad que no es abierto ni cerrado (`EXPIRED`, `SIGNED_UP`) | Tarea interna con motivo `UNKNOWN_OPPORTUNITY_STATUS`: lo desconocido nunca lleva a contactar | `tests/cortex/unit/test_followup.py::test_an_unknown_opportunity_status_never_allows_contact` |
| Una fecha acordada con el cliente que no es `YYYY-MM-DD` ni una fecha y hora ISO con desfase horario | Tarea interna con motivo `AGREED_DATE_UNREADABLE`, si ninguna regla anterior decide antes: una persona la lee y no se contacta al cliente | `tests/cortex/unit/test_followup.py::test_an_unreadable_agreed_date_is_an_internal_task`, `tests/cortex/unit/test_chat_events.py::test_an_agreed_date_that_cannot_be_read_is_kept_as_unreadable` |
| Si una decisión de seguimiento reemplaza lo pendiente de otra oportunidad de la misma conversación | No. Lo que se debe (tarea interna o fecha acordada) se guarda por oportunidad: una decisión cancela solo lo pendiente de su oportunidad, y una sin `opportunity_id` solo lo pendiente sin oportunidad. La ficha muestra la oportunidad de cada compromiso | `tests/cortex/integration/test_review_findings.py::test_f3_cancelling_one_opportunity_keeps_the_task_owed_on_another` |
| Un evento del kit (E02, E03) nombra una conversación distinta de la del escenario, de la misma empresa | El evento y el escenario actúan sobre la conversación que nombra el evento, y la salida la informa; un evento de otra empresa sigue siendo `scope_violation` | `tests/cortex/integration/test_chat.py::test_a_kit_event_pasted_verbatim_acts_on_the_conversation_it_names` |
| Una toma de control con la misma versión que la guardada | Si la conversación está en AUTO se aplica (gana la persona) y la versión sube en uno; si ya está en HUMAN es un duplicado. Ignorarla por error dejaría a la IA hablando encima de una persona | `tests/cortex/integration/test_tenant_store.py::test_the_same_version_while_the_agent_has_it_is_a_conflict_that_hands_it_over` |
| A qué empresa pertenece el corpus de documentos | Comercio A usa el de la primera entrega; Comercio B no tiene y nunca ve el de A | `tests/cortex/integration/test_docs.py::test_a_company_without_documents_gets_nothing_never_somebody_elses` |
| Cómo reiniciar el proceso en T10 | El adaptador arma objetos nuevos sobre los mismos archivos; `state_after_restart` es el modo de la conversación al final de la ejecución que hace el proceso reiniciado. Otro test lo repite con procesos reales | `tests/cortex/integration/test_chat.py::test_three_separate_processes_file_one_claim` |

## E. Por qué no se reutilizó el motor de la primera entrega tal cual

La consigna pide reutilizar los componentes; varios del motor anterior hacen hoy lo que está prohibido.

| Qué hacía | Decisión | Prueba |
|---|---|---|
| La máquina de estados (`transition`) recibe el horario como parámetro, pero la regla AUTO + `OPERATOR_TAKEOVER` exige horario abierto (habría que pasar `True` fijo para saltear una regla de negocio), no hay regla HUMAN + `OPERATOR_TAKEOVER` (una segunda toma lanza `InvalidTransition`) y no maneja versiones | La toma de control es un hecho informado por Zoco Chat, con versión y compare-and-set en una transacción. Del módulo se reutilizan los estados y `capabilities_for` | `tests/cortex/integration/test_tenant_store.py::test_a_newer_takeover_is_applied_at_its_version` |
| El ejecutor reintentaba las escrituras a ciegas | Las escrituras no pasan por los reintentos: se buscan por su id | `tests/cortex/unit/test_retry.py::test_only_transient_failures_are_ever_retried` |
| La clave de idempotencia salía del texto y la conversación, sin empresa | Sale de la empresa y el `operation_id` | `tests/cortex/integration/test_tenancy.py::test_every_query_on_company_data_filters_by_company`, `tests/cortex/integration/test_tenancy.py::test_the_same_operation_id_in_both_companies_files_one_claim_in_each` |
| El estado vivía en memoria y la confirmación no se podía serializar | Todo en SQLite, con una acción pendiente propia | `tests/cortex/integration/test_writes.py::test_the_same_command_after_a_restart_still_files_one_claim` |
| Había estado global (registro de herramientas, contador de ids, generador aleatorio) | Nada global: lo prohíbe un test sobre el código | `tests/cortex/unit/test_harness_invariants.py::test_no_module_holds_mutable_global_state` |

Se reutilizan por importación los componentes puros: el contrato de fechas y el modelo de ventas,
los argumentos estrictos y la guarda de argumentos, la huella de los valores, el repositorio de
reclamos, el buscador BM25 con su política de vigencia, y los estados y las capacidades por modo. Un contrato de arquitectura impide importar
el motor de turnos, la consola o el armado de la primera entrega.

## F. Preguntas que se le podrían hacer a IT

Ninguna bloquea el trabajo (todo quedó decidido arriba), pero confirmarlas quitaría supuestos:

1. ¿`customer_messages_sent` cuenta solo los mensajes que el sistema inicia, como sugiere el
   ejemplo del contrato (que es T01)? Aquí sí; las respuestas entregadas van en `ai_replies_delivered`.
2. ¿Qué motivo de fin corresponde a una escritura que espera la confirmación del usuario?
   Aquí es `NEEDS_HUMAN` con `finish_detail: awaiting_confirmation`.
