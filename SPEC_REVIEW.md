# Revisión de la especificación de la máquina de estados IA-humano

La especificación de la Parte 3 tiene contradicciones, ambigüedades y casos que faltan. Este documento
lista cada hallazgo, la decisión tomada y dónde queda implementada y probada. La implementación vive en
`src/merchant_assistant/control/` y no depende de ninguna librería de máquinas de estados.

Estado de implementación: los 26 hallazgos (16 de la Parte 3 y 10 de la Parte 2) están ✅ implementados y
probados, en `control` y en el agente. Lo que cada uno deja sin resolver del todo es un límite conocido y
se nombra en su **Estado**; el último apartado reúne lo que mostraron las corridas con el modelo real
([ADR-0008](design/adr/0008-primera-corrida-real-y-lo-que-cambio.md)).

El turno del agente (`src/merchant_assistant/agent/application/conversation.py`, decisión en
[ADR-0003](design/adr/0003-turno-del-agente.md)) y los comandos del operador y del supervisor
(`src/merchant_assistant/agent/application/operator.py`, decisión en
[ADR-0004](design/adr/0004-control-y-operadores.md)) hacen cumplir lo que el paquete `control` declara.
Sus escenarios están en `tests/integration/agent/test_agent_policies.py` y en
`tests/integration/agent/test_agent_control_scenarios.py`, que cruza estados con comandos reales.

## Resumen

| # | Tipo | Hallazgo | Resolución | Estado |
|---|---|---|---|---|
| F1 | Contradicción | HUMAN + `abuse_detected` aparece dos veces con destinos distintos | Queda en HUMAN y avisa al operador; nuevo evento `operator_block` | ✅ |
| F2 | Contradicción | BLOCKED + `operator_release` contradice la regla 4 | Se conserva la fila, restringida al supervisor | ✅ |
| F3 | Contradicción | "Cualquiera + `out_of_hours_started`" desbloquearía por reloj | Cuatro filas explícitas; BLOCKED sigue BLOCKED; el cierre en HUMAN avisa al operador | ✅ |
| F4 | Contradicción | La regla 3 solo se modela al entrar en AUTO | Guarda de horario abierto en tres eventos | ✅ |
| F5 | Caso faltante | No hay evento ni regla de retorno al abrir la franja | El horario es una guarda, no un estado | ✅ |
| F6 | Ambigüedad | Los actores no están definidos | Tabla de actores por evento; SUPERVISOR incluye a OPERATOR | ✅ |
| F7 | Caso faltante | SUGGEST + `customer_requests_human` no está definido | Pasa a HUMAN, con guarda de horario | ✅ |
| F8 | Caso faltante | Qué pasa con los eventos redundantes o sin sentido | Fallan de forma explícita con `InvalidTransition` | ✅ |
| F9 | Ambigüedad | `low_confidence` no está definida y la confianza del LLM no está calibrada | Compuerta `no_data` determinista (BM25 y coseno) calibrada con una regla declarada antes de medir; todas las búsquedas del turno sin resultado disparan el evento | ✅ |
| F10 | Interacción con la Parte 2 | Un reclamo a medio confirmar sobrevive a un cambio de control | `INVALIDATE_PENDING_ACTION` en todo cambio de estado, y la compuerta compara la versión de control | ✅ |
| F11 | Interacción con la Parte 2 | Orden de la regla 6 en SUGGEST | Pregunta aprobada, confirmación del cliente, aprobación del operador, ejecución | ✅ |
| F12 | Caso faltante | Los borradores no deben filtrarse al salir de SUGGEST | `DISCARD_UNAPPROVED_DRAFTS` descarta los borradores, que ya no se pueden aprobar | ✅ |
| F13 | Ambigüedad | "Ejecuta acciones" en HUMAN: ¿cuentan las herramientas de lectura? | Lectura estricta: ninguna herramienta en HUMAN ni BLOCKED | ✅ |
| F14 | Ambigüedad | La regla 5 no dice si los intentos rechazados se registran | Se registran ambos, distinguidos por resultado y motivo | ✅ |
| F15 | Ambigüedad | "Nadie responde" en BLOCKED no dice qué pasa con los mensajes entrantes | Se registran; no se genera ni se envía nada | ✅ |
| F16 | Caso faltante | Carrera: un operador toma el control mientras el LLM está respondiendo | Se decide la entrega contra el estado leído después de generar | ✅ |

Los tests citados están en `tests/unit/control/` salvo que se indique otra ruta. Un test
(`tests/unit/test_docs_consistency.py`) comprueba que cada ruta y cada test citados existen. Cuando un test está parametrizado, el nombre del caso
va entre corchetes.

---

## F1. HUMAN + `abuse_detected` tiene dos destinos

**Qué dice la especificación.** La tabla incluye la combinación HUMAN + `abuse_detected` dos veces: una
con destino BLOCKED y otra con destino HUMAN, con la nota "el operador decide".

**Por qué es un problema.** Una misma entrada con dos salidas hace que la máquina no sea determinista. Un
test no puede afirmar nada, y dos implementaciones razonables se comportarían distinto.

**Resolución.** HUMAN + `abuse_detected` queda en HUMAN y emite `NOTIFY_OPERATOR`, sin invalidar nada porque
el estado no cambia. La decisión del operador de bloquear tiene su propio evento, `operator_block`
(HUMAN → BLOCKED), que pueden disparar OPERATOR y SUPERVISOR. Así "el operador decide" deja de ser una
nota y pasa a ser una transición con actor y registro.

**Dónde.** `rules.py` (`RULES`; el índice rechaza pares duplicados) y `machine.py` (`effects_for`).

**Tests.** `test_transition_table.py::test_no_pair_has_two_rules_so_no_transition_is_ambiguous`,
`test_every_defined_rule_leads_to_its_target[HUMAN+abuse_detected]` y `[HUMAN+operator_block]`,
`test_effects.py::test_abuse_while_a_human_answers_notifies_without_invalidating`.

**Estado.** ✅ implementado en `control`.

## F2. BLOCKED + `operator_release` contradice la regla 4

**Qué dice la especificación.** La tabla permite BLOCKED + `operator_release` → AUTO. La regla 4 dice que
solo un supervisor puede sacar una conversación de BLOCKED.

**Por qué es un problema.** Si el operador puede liberar, la regla 4 no se cumple: el bloqueo se levantaría
sin la intervención que el bloqueo existe para exigir.

**Resolución.** Se conserva la fila, pero el único actor permitido es SUPERVISOR. Un operador recibe
`UnauthorizedActor`. `supervisor_unblock` queda como evento equivalente (BLOCKED → AUTO, también solo
SUPERVISOR) y se mantiene por la intención de auditoría: en el registro se distingue una liberación
corriente de un desbloqueo explícito.

**Dónde.** `rules.py` (filas BLOCKED + `operator_release` y BLOCKED + `supervisor_unblock`) y
`machine.py` (`transition`, segundo control).

**Tests.** `test_transition_table.py::test_only_a_supervisor_can_leave_blocked[operator_release]` y
`[supervisor_unblock]`, `test_an_operator_cannot_release_a_blocked_conversation`,
`test_a_supervisor_can_release_a_blocked_conversation`, y la propiedad
`test_conversation_properties.py::test_a_conversation_only_leaves_blocked_through_a_supervisor`.

**Estado.** ✅ implementado en `control`.

## F3. "Cualquiera + `out_of_hours_started` → AUTO" desbloquea por reloj

**Qué dice la especificación.** Una fila genérica dice que, desde cualquier estado, `out_of_hours_started`
lleva a AUTO.

**Por qué es un problema.** Aplicada a BLOCKED, el paso del reloj levantaría un bloqueo sin supervisor y
violaría la regla 4. Además "cualquiera + AUTO" es vaga para AUTO mismo: ¿es una transición o no pasa nada?

**Resolución.** "Cualquiera" se expande en cuatro filas explícitas, todas con actor SYSTEM:
AUTO → AUTO, SUGGEST → AUTO, HUMAN → AUTO y BLOCKED → BLOCKED. En AUTO y en BLOCKED el evento queda
registrado pero no cambia el estado ni emite efectos. En SUGGEST y HUMAN devuelve el control al agente,
porque a esa hora no hay un humano que responda (ver F4).

Se conserva la regla literal de la especificación (HUMAN + `out_of_hours_started` → AUTO), pero no en
silencio. Un operador que tomó la conversación cree que sigue siendo suya; al cerrar la franja, el agente
la retoma y le responde al cliente sin que nadie lo haya dicho. Por eso esa fila también emite
`NOTIFY_OPERATOR`: queda una entrada `out_of_hours_started` en el inbox (visible solo en la vista de equipo,
con `state=AUTO`), además de invalidar la acción pendiente. Es información para el operador, no una acción:
el estado ya cambió. En SUGGEST no se avisa, porque ahí el operador solo aprobaba borradores y no había
tomado el chat.

**Dónde.** `rules.py` y `machine.py` (`effects_for`).

**Tests.** `test_transition_table.py::test_every_defined_rule_leads_to_its_target[BLOCKED+out_of_hours_started]`
y el resto de las filas del evento, `test_effects.py::test_every_rule_carries_exactly_the_effects_of_record[AUTO+out_of_hours_started]`
y `[BLOCKED+out_of_hours_started]` (la fila HUMAN incluye `NOTIFY_OPERATOR`),
`test_effects.py::test_closing_time_in_human_notifies_the_operator_and_hands_the_chat_back`,
`test_conversation.py::test_a_blocked_conversation_stays_blocked_through_closing_time` y, de punta a punta con
el `OperatorService` real, `test_agent_control_scenarios.py::test_4_closing_time_leaves_the_operator_an_inbox_entry_when_it_ends_a_human_chat`.

**Estado.** ✅ implementado en `control`.

## F4. La regla 3 solo está modelada como entrada a AUTO

**Qué dice la especificación.** La regla 3 dice que fuera del horario de atención solo responde la IA y que
se informa la franja humana. La tabla solo refleja esto como `out_of_hours_started` → AUTO.

**Por qué es un problema.** Nada impide que, fuera de horario, un operador tome la conversación, que el
cliente pida un humano o que baje la confianza (→ SUGGEST). Cualquiera de esos eventos dejaría la
conversación en manos de alguien que, según la regla, no va a responder.

**Resolución.** Las cinco reglas que entregan la conversación a un humano o a un operador (`operator_takeover`
y `customer_requests_human` desde AUTO y SUGGEST, y `low_confidence` desde AUTO) llevan una guarda de horario
abierto. Fuera de la franja fallan con `GuardRejected` (código `out_of_hours`). El orden de los controles es
parte del contrato: par indefinido, actor no autorizado, guarda. Un actor sin permiso no se entera del horario.
Informar la franja al cliente es responsabilidad de la aplicación; la capacidad `inform_hours_window` se lo
indica.

**Dónde.** `rules.py` (`requires_open_hours`), `machine.py` (`transition`), `hours.py` (`BusinessHours.is_open`)
y `capabilities.py` (`inform_hours_window`).

**Tests.** `test_transition_table.py::test_five_rules_are_guarded_by_the_open_hours`,
`test_a_guarded_rule_is_refused_when_the_hours_are_closed`, `test_a_guarded_rule_passes_when_the_hours_are_open`,
`test_an_unguarded_rule_works_when_the_hours_are_closed`, `test_an_unauthorized_actor_learns_nothing_about_the_hours`,
`test_conversation_properties.py::test_while_the_hours_are_closed_a_conversation_never_becomes_human_or_suggest`,
`test_capabilities.py::test_what_the_agent_may_do_in_each_state_and_hour`.

**Estado.** ✅ guarda implementada en `control`. ✅ fuera de horario el turno agrega a cada respuesta la
plantilla con la franja (`hours_notice`), y si el cliente pide un humano fuera de horario el intento queda
registrado como rechazado y la respuesta dice cuándo atiende el equipo (`human_unavailable`). ✅ un
`takeover` del operador fuera de horario falla con `out_of_hours` y queda registrado, y todo comando
sincroniza primero el horario, así que después del cierre un operador ya no puede escribirle al cliente
ni aprobar un borrador. La franja es configuración (`HUMAN_HOURS_*`, ver Supuestos) y se le muestra al
cliente como "de lunes a viernes de 09:00 a 18:00, hora de Buenos Aires" (`describe_hours`). Tests:
`tests/integration/agent/test_agent_policies.py::test_17_out_of_hours_the_answer_carries_the_human_window`,
`tests/unit/agent/test_agent_conversation.py::test_a_request_for_a_person_out_of_hours_is_answered_with_the_window`,
`tests/integration/agent/test_agent_control_scenarios.py::test_4_out_of_hours_a_takeover_is_refused_and_logged`,
`test_4_out_of_hours_a_customer_asking_for_a_person_gets_the_window`,
`test_4_after_closing_an_operator_can_no_longer_write_to_the_customer`.

## F5. No existe `in_hours_started` ni regla de retorno

**Qué dice la especificación.** Hay un evento para cuando termina el horario de atención, pero ninguno para
cuando empieza.

**Por qué es un problema.** Falta saber qué debe pasar al abrirse la franja. Agregar el evento obligaría a
un proceso que dispare transiciones por reloj en todas las conversaciones.

**Resolución.** El horario es una condición de calendario (una guarda), no un estado. Al abrirse la franja no
hay nada que disparar: una conversación en AUTO sigue en AUTO y los eventos guardados vuelven a poder
aplicarse. Lo único que requiere una transición es el cierre, porque hay que sacar a la conversación de
HUMAN o SUGGEST (`ConversationControl.sync_hours`, que la aplicación llama al comienzo de cada turno).

**Dónde.** `hours.py` y `conversation.py` (`sync_hours`).

**Tests.** `test_business_hours.py` (límites, fin de semana, huso horario),
`test_conversation.py::test_closing_time_hands_a_human_or_suggest_conversation_back_to_the_agent`,
`test_while_the_window_is_open_syncing_changes_nothing`.

**Estado.** ✅ implementado en `control`.

## F6. Los actores no están definidos

**Qué dice la especificación.** El registro exige guardar el actor y la regla 4 distingue al supervisor, pero
la especificación no dice quién puede disparar cada evento.

**Por qué es un problema.** Sin una tabla de actores, "solo un supervisor" no se puede verificar y el registro
no puede decir quién hizo qué.

**Resolución.** Cuatro actores: CUSTOMER, SYSTEM, OPERATOR y SUPERVISOR. Cada regla declara los actores que
pueden dispararla. SUPERVISOR puede hacer todo lo que puede hacer OPERATOR. Los eventos que detecta el
sistema (`low_confidence`, `abuse_detected`, `out_of_hours_started`) solo los dispara SYSTEM.

**Dónde.** `states.py` (`Actor`), `rules.py` (campo `actors`) y `machine.py` (`transition`).

**Tests.** `test_transition_table.py::test_an_actor_outside_the_allowed_set_is_refused`,
`test_every_allowed_actor_can_fire_the_rule`, `test_a_supervisor_may_do_everything_an_operator_may`,
`test_a_customer_cannot_take_over_a_conversation`, `test_only_the_system_reports_abuse_or_low_confidence`.

**Estado.** ✅ implementado en `control`.

## F7. SUGGEST + `customer_requests_human` no está definido

**Qué dice la especificación.** El cliente puede pedir un humano desde AUTO, pero la combinación con SUGGEST
no figura.

**Por qué es un problema.** Un cliente que pide una persona mientras el operador revisa borradores quedaría
con un error, justo cuando es más razonable atenderlo.

**Resolución.** SUGGEST + `customer_requests_human` → HUMAN, con la misma guarda de horario que en AUTO y con
los actores CUSTOMER y SYSTEM. HUMAN y BLOCKED + `customer_requests_human` siguen indefinidos a propósito:
en HUMAN ya atiende una persona y en BLOCKED nadie responde (F15).

**Dónde.** `rules.py`.

**Tests.** `test_transition_table.py::test_every_defined_rule_leads_to_its_target[SUGGEST+customer_requests_human]`,
`test_an_undefined_pair_is_refused_whoever_asks_and_whatever_the_hours[HUMAN+customer_requests_human]` y
`[BLOCKED+customer_requests_human]`.

**Estado.** ✅ implementado en `control`.

## F8. Los eventos indefinidos deben fallar de forma explícita

**Qué dice la especificación.** Pide que toda transición no definida falle con un error explícito, pero no
enumera cuáles son. Por ejemplo, ¿qué pasa con `low_confidence` si ya se está en SUGGEST?

**Por qué es un problema.** La tentación es que la máquina ignore en silencio los eventos redundantes. Eso
esconde errores de quien la usa y deja un registro que no refleja lo que ocurrió.

**Resolución.** De los 32 pares (estado, evento) hay 17 definidos y 15 indefinidos. Los 15 lanzan
`InvalidTransition` (código `invalid_transition`), sin importar el actor ni el horario. La aplicación decide
qué evento emitir en cada estado en lugar de que la máquina trague los redundantes.

**Dónde.** `machine.py` (`transition`, primer control) y `rules.py` (`rule_for`).

**Tests.** `test_transition_table.py::test_exactly_fifteen_of_the_thirty_two_pairs_are_undefined`,
`test_an_undefined_pair_is_refused_whoever_asks_and_whatever_the_hours` (los 15 pares),
`test_readable_cases_the_specification_does_not_allow` (incluye `redundant event is not swallowed`),
`test_an_undefined_pair_is_reported_before_the_hours_are_looked_at`.

**Estado.** ✅ implementado en `control`.

## F9. `low_confidence` no está definida

**Qué dice la especificación.** `low_confidence` lleva de AUTO a SUGGEST, pero no dice qué es una confianza
baja ni quién la mide.

**Por qué es un problema.** Si se la delega al propio modelo ("¿qué tan seguro estás?"), la confianza
autoinformada de un LLM está mal calibrada: puede ser alta cuando se equivoca. La transición dependería de
la opinión de quien debería ser supervisado. Y "no hubo resultados" casi nunca ocurre por sí solo: BM25
puntúa cualquier token compartido (no hay stopwords) y una búsqueda vectorial siempre devuelve vecinos.

**Resolución.** El evento solo lo dispara SYSTEM, a partir de una señal determinista de retrieval. La
búsqueda tiene una **compuerta** (`Gate` en `bootstrap.py`): si la consulta es débil, devuelve `no_data`,
diga lo que diga el retriever, también en modo híbrido. En modo híbrido la consulta es débil cuando su mejor
BM25 sobre el corpus es menor que `GATE_BM25_HYBRID` (11.824) **y** su mejor coseno contra los vectores del
corpus es menor que `GATE_COSINE_HYBRID` (0.366); sin embeddings (modo degradado) solo existe BM25 y el
umbral es `GATE_BM25_DEGRADED` (0.039). En AUTO, si el modelo buscó en la documentación y **todas** las
búsquedas del turno dieron `no_data`, el turno dispara `low_confidence` antes de entregar. En horario la
conversación pasa a SUGGEST y la respuesta de ese mismo turno queda como borrador (las capacidades se
recalculan después de la transición). Fuera de horario la guarda lo rechaza, el rechazo queda registrado y
la respuesta sale en AUTO con el aviso de la franja. La transición vive en el estado del turno y se guarda
con él, así que no dispara el descarte por concurrencia del propio turno. La confianza que diga tener el
modelo no se usa nunca.

**Calibración.** Los umbrales no se eligieron a ojo: salen de una regla escrita antes de medir y aplicada a
20 preguntas del kit, 30 consultas cortas legítimas y 45 fuera de tema, 13 de ellas difíciles porque comparten
vocabulario con el dominio ([ADR-0007](design/adr/0007-recuperacion-del-agente-y-umbral.md),
`evaluation/results/retrieval/gate_calibration.md`). Resultado: ninguna consulta legítima queda sin
respuesta, la compuerta híbrida rechaza 33 de 45 fuera de tema y la degradada 2 de 45.
*Historia:* la primera versión era un único piso BM25 de 5,25 sobre chunking `section`, calibrado con el kit
y 16 preguntas fuera de tema; dejaba sin respuesta 17 de las 50 consultas legítimas medidas después
(entre ellas "¿Cómo transfiero una entrada?"), y [ADR-0004](design/adr/0004-control-y-operadores.md)
quedó reemplazado en ese punto.

**Dónde.** `rules.py` (solo SYSTEM puede disparar `low_confidence`), `bootstrap.py` (`Gate`, las tres
constantes `GATE_*` y `SearchAdapter`) y `agent/application/conversation.py` (`_found_nothing` y el disparo en
`handle_user_message`).

**Tests.** `test_transition_table.py::test_only_the_system_reports_abuse_or_low_confidence`,
`tests/integration/agent/test_agent_no_data_gate.py` (las 50 consultas legítimas responden en los dos modos,
los umbrales del código son los del informe de calibración, el camino de falla del proveedor),
`tests/integration/agent/test_agent_bootstrap.py::test_the_gate_wins_over_a_retriever_that_always_finds_neighbours`,
`tests/unit/agent/test_agent_conversation.py::test_when_every_search_finds_nothing_in_hours_the_answer_waits_for_an_operator`,
`test_out_of_hours_low_confidence_is_refused_and_the_answer_goes_out_with_the_window`,
`test_low_confidence_needs_searches_and_every_one_of_them_empty`,
`tests/integration/agent/test_agent_control_scenarios.py::test_2_an_off_topic_question_hands_the_answer_and_then_a_complaint_to_the_operator`.

**Estado.** ✅ señal léxica y vectorial, umbrales calibrados y transición en el turno. Límites conocidos y
medidos: en modo híbrido el margen del coseno es de unos 0.012 a cada lado del umbral y pasan 12 de 45
consultas fuera de tema (10 de las 13 difíciles: ningún umbral sobre estas señales las separa); en modo
degradado la compuerta casi no rechaza nada, por la regla (una consulta legítima corta puntúa casi cero en
BM25); y los umbrales se ajustaron sobre las mismas consultas con que se midieron, así que sobrestiman lo que
pasaría con personas reales. Con el modelo real, además, `low_confidence` casi no se dispara: el modelo
contesta las preguntas fuera de tema sin buscar (la evidencia `low_confidence fired` de X2 es 0/3 en
`evaluation/results/conversations/live/report.md`), así que SUGGEST se ejerce con modelos guionados.
Otro límite, señalado por una revisión independiente (un subagente de Claude Code) y sin corregir: la guarda de horario de `low_confidence`
usa el reloj leído al empezar el turno (`now` en `ConversationService.handle_user_message`). Si la llamada al
modelo cruza las 18:00, la transición AUTO → SUGGEST se aplica fuera de horario, la respuesta queda como
borrador y el registro muestra la hora anterior. El turno siguiente corrige el estado (`sync_hours` vuelve a AUTO),
pero esa transición descarta los borradores sin aprobar (`DISCARD_UNAPPROVED_DRAFTS`): la respuesta producida al
cruzar las 18:00 se pierde y el cliente nunca la recibe.
La corrección sugerida es volver a leer el reloj antes de disparar el evento (`_fire`).

## F10. Un reclamo a medio confirmar sobrevive a un cambio de control

**Qué dice la especificación.** La Parte 2 exige que `crear_reclamo` se ejecute solo con la confirmación
explícita del usuario sobre esos parámetros. La Parte 3 no dice qué pasa con una acción pendiente cuando
cambia quién controla la conversación.

**Por qué es un problema.** Un operador toma el control con un reclamo a medio confirmar; al liberar, el
cliente responde "sí" a una pregunta de hace una hora y el reclamo se ejecuta con parámetros que ya nadie
revisó en contexto.

**Resolución.** Toda transición que cambia el estado emite `INVALIDATE_PENDING_ACTION`. Una transición que no
cambia el estado (por ejemplo, HUMAN + `abuse_detected`) no lo emite.

**Dónde.** `machine.py` (`effects_for`).

**Tests.** `test_effects.py::test_every_rule_carries_exactly_the_effects_of_record` (las 17 filas),
`test_a_change_of_who_controls_the_chat_invalidates_the_pending_action`.

**Estado.** ✅ efecto emitido por `control`, y ✅ el turno borra la acción pendiente cuando lo recibe (al
sincronizar el horario y en las transiciones que disparan las señales). Además la compuerta ya no depende de
que alguien aplique el efecto: cada acción pendiente guarda la versión de control (`len(control_log)`) con la
que se propuso, y `decide` la descarta si la versión cambió, diga lo que diga el usuario. Una transición
rechazada también cuenta: cualquier intento de cambiar de manos invalida, que es la lectura conservadora.
El comando `takeover` del operador ejecuta el mismo efecto (`apply_effects`): después de liberar, un
"sí" ya no encuentra nada pendiente.
Al descartar la acción por este efecto, `apply_effects` deja además una entrada `pending_action_dropped` en
el inbox del operador, con la herramienta y los parámetros exactos que el cliente había visto (una sola línea,
hasta 300 caracteres). Es contexto para quien toma la conversación: se ve solo en la vista de equipo y no es
una acción ejecutable. No se escribe cuando el propio mensaje del cliente descarta la acción.
Tests: `tests/integration/agent/test_agent_control_scenarios.py::test_1_a_takeover_kills_a_half_confirmed_complaint_for_good`,
`test_1_a_takeover_leaves_the_dropped_complaint_for_the_operator_and_nothing_for_the_customer`,
`test_agent_effects.py::test_dropped_detail_cuts_at_exactly_three_hundred_characters`,
`tests/unit/agent/test_agent_pending.py::test_a_change_of_control_since_the_proposal_invalidates_whatever_the_user_says`,
`test_agent_pending_properties.py::test_a_reply_under_another_control_version_never_confirms`,
`tests/integration/agent/test_agent_policies.py::test_10_a_change_of_control_since_the_proposal_voids_a_later_yes`,
`tests/unit/agent/test_agent_conversation.py::test_closing_time_hands_a_human_conversation_back_and_drops_the_pending_action`.

## F11. Orden de la regla 6 en SUGGEST

**Qué dice la especificación.** En SUGGEST, `crear_reclamo` requiere la aprobación del operador además de la
confirmación del usuario. No dice en qué orden ni qué pasa si el operador edita el texto.

**Por qué es un problema.** Sin un orden, el operador podría aprobar parámetros que el usuario nunca vio, o
el usuario confirmar algo que el operador después cambió. Y en SUGGEST la propia pregunta de confirmación
es un mensaje al cliente, que no puede salir sin aprobación.

**Resolución.** Cinco pasos en un orden estricto:

1. El modelo propone `crear_reclamo`: queda un borrador CONFIRMATION_REQUEST con la acción, y **nada
   pendiente**. La propiedad de la compuerta ("el cliente vio estos valores exactos") solo vale cuando la
   pregunta se entregó de verdad.
2. El operador aprueba ese borrador: la pregunta de plantilla se entrega y recién ahí se propone la acción,
   con el turno y la versión de control de ese momento. La respuesta del cliente es necesariamente un turno
   posterior.
3. El "sí" estricto del cliente produce un `Confirmed`; como `effects_need_operator_approval` es verdadero,
   no se ejecuta nada: queda un único borrador ACTION_APPROVAL que lo contiene. Un "sí" repetido no pide
   dos veces; un "no" o cualquier otro mensaje lo descarta.
4. El operador lo aprueba: `execute_confirmed`, `mark_registered` y la plantilla `registered`. Ese mensaje
   es la consecuencia directa y determinista de la decisión del operador, así que cuenta como aprobado por
   él; lo mismo vale para `not_approved`, que se envía si el operador lo rechaza (y la acción se cancela).
5. Salir de SUGGEST por cualquier evento descarta los borradores e invalida la acción, así que ni una
   pregunta ni una aprobación se pueden aprobar después.

No hay un comando para editar los parámetros. Un operador que quiere otros valores rechaza, y el cliente
los vuelve a decir y a confirmar; es más rígido, pero no abre otra entrada al efecto. Una aprobación también
se rechaza (y su borrador se descarta) si la acción que contiene ya no es la pendiente o si la versión de
control cambió desde el "sí", aunque haya sido por un intento rechazado: la lectura conservadora de F10.

**Dónde.** `capabilities.py` (`Capabilities.effects_need_operator_approval`),
`agent/application/conversation.py` (`_propose`, `_execute`, `_ask_for_approval`, `_forget_approvals`),
`agent/application/operator.py` (`approve_draft`, `reject_draft`) y `agent/domain/drafts.py`.

**Tests.** `test_capabilities.py::test_a_complaint_needs_the_operator_only_while_the_operator_reviews`,
`tests/integration/agent/test_agent_control_scenarios.py::test_2_in_suggest_a_complaint_needs_the_customer_and_then_the_operator`,
`test_2_rejecting_the_approval_registers_nothing_and_says_so`, `test_2_rejecting_the_question_leaves_nothing_pending`,
`test_2_a_repeated_yes_asks_the_operator_once`, `test_2_a_customer_who_says_something_else_voids_the_approval`,
`test_10_the_two_approvals_interleaved_with_anything_keep_every_rule` (Hypothesis: una fila solo existe
después de la confirmación del cliente y, en SUGGEST, de la aprobación del operador),
`tests/unit/agent/test_agent_operator.py` (falla de la ejecución, carrera perdida después del efecto, versión
de control cambiada).

**Estado.** ✅ implementado. Si una carrera perdida repite una aprobación después de que el efecto corrió,
no lo corre de nuevo; si en esa ventana la conversación cambió de manos, el registro queda guardado y la
bandeja del operador dice que no se anunció, porque el estado ya no deja enviar nada.

## F12. Los borradores no deben filtrarse al salir de SUGGEST

**Qué dice la especificación.** En SUGGEST el agente nunca envía sin aprobación. No dice qué pasa con los
borradores pendientes cuando el estado cambia.

**Por qué es un problema.** Un borrador pendiente que se aprobara o enviara después de salir de SUGGEST (el
operador tomó la conversación, o se bloqueó por abuso) llegaría al cliente fuera de contexto, o por encima de
un bloqueo.

**Resolución.** Toda transición que sale de SUGGEST emite `DISCARD_UNAPPROVED_DRAFTS`. Entrar a SUGGEST no lo
emite. El agente lo ejecuta en un solo lugar (`apply_effects`), tanto en el turno como en los comandos: cada
borrador pendiente pasa a DISCARDED, queda en el registro de la conversación con quién (SYSTEM) y cuándo, y no
se puede aprobar ni enviar. Un borrador se decide una sola vez, solo un operador o un supervisor lo decide, y
solo mientras la conversación sigue en SUGGEST.

**Dónde.** `machine.py` (`effects_for`), `agent/domain/effects.py`, `agent/domain/drafts.py` y
`agent/application/operator.py` (`drafts`, `approve_draft`, `reject_draft`).

**Tests.** `test_effects.py::test_leaving_suggest_discards_the_drafts_nobody_approved`,
`test_entering_suggest_discards_nothing`,
`test_effects.py::test_every_rule_carries_exactly_the_effects_of_record[SUGGEST+...]` (las cinco filas de SUGGEST),
`test_conversation.py::test_leaving_suggest_at_closing_time_discards_the_drafts`,
`tests/unit/agent/test_agent_effects.py`, `tests/unit/agent/test_agent_drafts.py`,
`tests/integration/agent/test_agent_control_scenarios.py::test_2_releasing_to_auto_discards_the_drafts_for_good`
(con una pregunta y con una aprobación pendientes), `test_2_only_the_team_decides_on_drafts`, y las dos
propiedades `test_10_*` (en SUGGEST no llega al cliente nada más que borradores aprobados y plantillas que
siguen a una decisión del operador).

**Estado.** ✅ implementado.

## F13. "Ejecuta acciones" en HUMAN es ambiguo

**Qué dice la especificación.** En HUMAN el agente no envía mensajes ni ejecuta acciones. No aclara si las
herramientas de solo lectura (documentación, ventas) cuentan como acciones.

**Por qué es un problema.** Con la lectura laxa, el agente consultaría ventas o documentación en una
conversación que atiende una persona, sin que nadie se lo pida y sin utilidad para el cliente.

**Resolución.** Lectura estricta: en HUMAN y en BLOCKED no hay herramientas de ningún tipo y no se genera
respuesta. Clasificar un mensaje con detectores deterministas (abuso, pedido de un humano) sigue ocurriendo,
porque clasificar no es actuar.

**Dónde.** `capabilities.py` (`capabilities_for`).

**Tests.** `test_capabilities.py::test_what_the_agent_may_do_in_each_state_and_hour[HUMAN-open]` y `[HUMAN-closed]`,
`test_in_human_and_blocked_the_agent_has_no_tools_at_all`.

**Estado.** ✅ implementado en `control`.

## F14. La regla 5 no dice si se registran los intentos rechazados

**Qué dice la especificación.** Cada transición registra estado anterior, estado nuevo, evento, actor y fecha.

**Por qué es un problema.** Un operador que intenta liberar un BLOCKED y es rechazado, o un takeover fuera de
horario, son hechos que una auditoría quiere ver. Si solo se registran los éxitos, desaparecen.

**Resolución.** Se registran ambos, distinguidos por `outcome` (`APPLIED` o `REJECTED`). En un rechazo, `reason`
lleva el código del error y el estado nuevo es igual al anterior. Las fechas deben tener zona horaria. El
registro es de solo agregado, valida la cadena al agregar (cada registro parte del estado actual) y el estado
de la conversación se deriva de él, por lo que ambos no pueden divergir.

**Dónde.** `log.py` (`TransitionRecord`, `TransitionLog`) y `conversation.py` (`ConversationControl.apply`).

**Tests.** `test_log.py` (campos, fecha ingenua, cadena, rechazos), `test_conversation.py::test_an_applied_transition_logs_the_five_fields_the_brief_requires`,
`test_a_refused_attempt_is_logged_with_its_reason_and_raised_again`,
`test_conversation_properties.py::test_replaying_the_applied_records_from_auto_reproduces_the_state` y
`test_a_refused_attempt_never_changes_the_state`.

**Estado.** ✅ implementado en `control`. ✅ cada comando del operador o del supervisor deja un registro con
los cinco campos, o uno REJECTED con su código, y el registro se guarda con la conversación aunque el comando
falle (`tests/integration/agent/test_agent_control_scenarios.py::test_9_every_command_leaves_a_record_with_the_five_fields`).
Al cargar una conversación, el registro se vuelve a reproducir y cada transición APPLIED tiene que ser una que
las reglas permiten a ese actor: un estado guardado no puede afirmar, por ejemplo, que un operador sacó una
conversación de BLOCKED (`tests/unit/agent/test_agent_state.py::test_a_stored_log_cannot_claim_a_transition_the_rules_do_not_allow`).
La guarda de horario no se puede reproducir, porque la franja no forma parte del estado.

## F15. "Nadie responde" en BLOCKED es ambiguo para los mensajes entrantes

**Qué dice la especificación.** En BLOCKED nadie responde. No dice qué pasa con lo que el cliente siga
escribiendo.

**Por qué es un problema.** Descartar los mensajes entrantes pierde evidencia (justamente la del abuso que
motivó el bloqueo) y le impide al supervisor ver el contexto cuando revisa.

**Resolución.** Los mensajes entrantes se registran, pero no se genera ni se envía ninguna respuesta. Esta es
una decisión de este diseño, que la especificación no resuelve. `control` expresa la mitad de la decisión: en
BLOCKED `delivery` es `NONE` y `generate_reply` es falso.

**Dónde.** `capabilities.py`.

**Tests.** `test_capabilities.py::test_what_the_agent_may_do_in_each_state_and_hour[BLOCKED-open]` y `[BLOCKED-closed]`.

**Estado.** ✅ capacidad implementada en `control`. ✅ el turno guarda el mensaje entrante y no llama al modelo
ni envía nada en HUMAN ni en BLOCKED
(`tests/integration/agent/test_agent_policies.py::test_17_a_state_that_generates_no_reply_never_calls_the_model`).
Decisión adicional de este diseño: en el turno en que una señal determinista lleva la conversación a HUMAN o a
BLOCKED, el sistema manda una única plantilla de aviso (traspaso o suspensión); después, silencio
([ADR-0003](design/adr/0003-turno-del-agente.md)). ✅ con comandos reales: dos insultos bloquean, los mensajes
siguientes quedan guardados sin llamar al modelo, el `unblock` y el `release` de un operador se rechazan y
quedan como REJECTED, y el `unblock` de un supervisor vuelve a AUTO y pone en cero los avisos de abuso
(`tests/integration/agent/test_agent_control_scenarios.py::test_3_only_a_supervisor_brings_a_blocked_conversation_back`).
El abuso en HUMAN deja una entrada en la bandeja del operador, que decide si bloquea
(`test_3_abuse_while_a_person_answers_reaches_the_operator_inbox_and_the_operator_blocks`).

## F16. Carrera entre un takeover y una llamada al LLM en curso

**Qué dice la especificación.** Nada. El estado se consulta al comienzo del turno.

**Por qué es un problema.** La llamada al modelo tarda segundos. Si un operador toma la conversación (o se
detecta abuso) mientras tanto, la respuesta generada con el estado anterior llegaría al cliente después del
cambio de control, y se enviaría desde un estado que ya no la permite.

**Resolución.** La entrega se decide contra el estado leído después de la generación, no antes. Si cambió, la
respuesta se descarta. `ConversationControl.state` siempre refleja el último registro, así que esa lectura es
barata y consistente.

**Dónde.** No está en `control`: pertenece al caso de uso del turno, `agent/application/conversation.py`
(`_deliver`). Justo antes de entregar se relee la versión guardada; si cambió desde el comienzo del turno, la
respuesta se descarta (`dropped: control_changed`) y solo se agrega el mensaje del usuario al estado que está
guardado ahora. El guardado usa concurrencia optimista (`ConversationStore.save(expected_version=...)`), así
que una escritura entre la relectura y el guardado también termina en descarte.

**Tests.** `tests/integration/agent/test_agent_policies.py::test_16_an_answer_generated_while_control_changed_hands_is_dropped`
(un paso del modelo guionado hace el takeover mientras "responde"),
`tests/integration/agent/test_agent_control_scenarios.py::test_6_a_takeover_while_the_model_answers_drops_the_answer`
(el mismo caso, a través de `OperatorService.takeover`), `tests/unit/agent/test_agent_memory_store.py`, y
`tests/unit/agent/test_agent_operator.py::test_a_command_that_loses_a_race_is_replayed_on_the_fresh_state`
(el comando que pierde la carrera se repite sobre el estado nuevo, sin pisar lo que no leyó). Si el turno
descartado ya había registrado un reclamo (el "sí" corre antes de la entrega), el registro no se pierde: queda
como último reclamo de la conversación y la bandeja del operador avisa que no se anunció
(`test_agent_control_scenarios.py::test_6_a_complaint_stored_by_a_turn_that_loses_the_race_is_recorded_for_the_team`).

**Estado.** ✅ implementado en el turno del agente.

---

## Ambigüedades de la Parte 2

La Parte 2 también deja huecos, sobre todo alrededor de las políticas 🔒. Cada uno se resolvió con una regla
determinista. La parte de dominio vive en `src/merchant_assistant/agent/domain/` y sus tests en
`tests/unit/agent/`. Todas las resoluciones están ✅ implementadas y probadas, en el dominio del agente o en la
capacidad que se indica, y conectadas al turno.

Desde [ADR-0003](design/adr/0003-turno-del-agente.md) el turno conecta todas las piezas en el orden descrito,
y cada resolución tiene un escenario con un modelo guionado atacante en
`tests/integration/agent/test_agent_policies.py`.

| # | Ambigüedad | Resolución | Estado |
|---|---|---|---|
| P1 | Qué es "un mismo reclamo" | Misma conversación y mismo texto, sin importar mayúsculas ni espacios | ✅ |
| P2 | Qué es una "confirmación explícita" | Vocabulario afirmativo estricto; ante la duda, se vuelve a preguntar | ✅ |
| P3 | "Si el usuario cambia el motivo o la descripción" | Antes de ejecutar: se reemplaza y se vuelve a confirmar. Después: es otro reclamo | ✅ |
| P4 | "Sí" repetido sin nada pendiente | Respuesta determinista desde una plantilla, nunca del LLM | ✅ |
| P5 | Falla después de confirmar | La acción pendiente sobrevive; un "sí" posterior reintenta con la misma clave | ✅ |
| P6 | Qué significa "herramienta disponible" | Depende del estado de la conversación, no solo del registro de herramientas | ✅ |
| P7 | El dataset tiene una venta en el futuro | El contrato de fecha rechaza fechas futuras: el dato nunca llega como hecho | ✅ |
| P8 | Fechas relativas o ambiguas | El contrato exige ISO y el turno solo consulta fechas que el usuario escribió o aceptó | ✅ |
| P9 | DOC-11 define categorías que `crear_reclamo` no tiene | Se mantiene el contrato de la consigna: solo `motivo` y `descripcion` | ✅ |
| P10 | Frases imperativas dentro del corpus | Son datos: ninguna puede ejecutar un efecto sin la confirmación del usuario | ✅ |

## P1. "Un mismo reclamo"

**Qué dice la consigna.** Un reclamo confirmado no se registra dos veces, aunque el usuario repita "sí" o la
herramienta falle y se reintente.

**Por qué es un problema.** No define la identidad de un reclamo. Si es solo el texto, dos conversaciones
distintas con el mismo problema se pisarían. Si es el texto exacto, "Pago doble" y "pago  doble" serían dos
reclamos y un reintento con otra capitalización duplicaría el registro.

**Resolución.** Un reclamo es el mismo si pertenece a la misma conversación y su motivo y su descripción
coinciden una vez normalizados (NFKC, espacios colapsados) y sin distinguir mayúsculas. Otra conversación u
otro texto es otro reclamo. La clave de idempotencia la hace cumplir la base de datos con una restricción
UNIQUE. Hay dos identidades distintas a propósito: la confirmación es sensible a mayúsculas y puntuación (el
usuario confirma el texto que vio, ver P3), y la deduplicación no lo es (un reintento no debe duplicar).

**Dónde.** `complaints/models.py` (`idempotency_key`) y `complaints/adapters/sqlite_repository.py`.

**Tests.** `tests/unit/complaints/test_complaint_models.py::test_the_same_complaint_typed_differently_gets_the_same_key`,
`test_another_conversation_or_other_text_gets_another_key`,
`tests/integration/complaints/test_sqlite_repository.py::test_a_duplicate_key_returns_the_original_and_the_table_keeps_one_row`,
`test_the_same_text_in_another_conversation_is_another_complaint`, `test_threads_racing_the_same_complaint_leave_exactly_one_row`.

**Estado.** ✅ clave e idempotencia implementadas en la capacidad `complaints`, y ✅ el turno registra cada
reclamo con el identificador de su conversación, a través del único punto de entrada
`ToolExecutor.execute_confirmed` (`tests/integration/agent/test_agent_policies.py::test_3_repeating_yes_never_registers_again_and_carries_the_existing_id`).

## P2. "Confirmación explícita"

**Qué dice la consigna.** No registrar un reclamo sin la confirmación explícita del usuario.

**Por qué es un problema.** No dice qué cuenta como explícito. "Sí, pero cambiá el motivo", "si no" o "sí" seguido
de otra línea con instrucciones contienen un "sí" y no son una aceptación. Si lo decidiera el LLM, la garantía
dependería de su interpretación.

**Resolución.** Un clasificador determinista y conservador. Una respuesta es afirmativa solo si es una línea de
hasta seis palabras, todas de un vocabulario afirmativo, con al menos un "sí" real, y sin signos de pregunta.
Cualquier otra cosa no confirma: una negativa cancela la acción pendiente y cualquier otro mensaje la invalida.
El costo es una experiencia rígida: una palabra que podría cambiar lo aceptado ("pero", "otro", "cambia",
"motivo") invalida la propuesta y el agente tiene que volver a preguntar. El vocabulario incluye palabras
neutras (artículos, "mi", "reclamo", "registra", "eso", "gracias"), así que "sí, confirmo el reclamo" sí
confirma ([ADR-0007](design/adr/0007-recuperacion-del-agente-y-umbral.md)); "si eso" y "si bien" dichos solos
también cuentan como un sí aunque duden. Esa es la falla segura; la insegura sería registrar algo que el usuario no aceptó con claridad.

**Dónde.** `agent/domain/replies.py` (`classify_reply`) y `agent/domain/pending.py` (`decide`).

**Tests.** `tests/unit/agent/test_agent_replies.py::test_a_reply_is_classified_conservatively` (tabla con tildes,
formas de ancho completo, mezclas, saltos de línea y emojis),
`test_agent_pending_properties.py::test_a_confirmation_needs_a_pending_action_a_strict_yes_and_the_same_fingerprint`.

**Estado.** ✅ en el dominio y en el turno: la compuerta corre antes que el modelo y recibe solo el texto del
usuario (`tests/integration/agent/test_agent_policies.py::test_2_a_later_yes_registers_exactly_once_without_calling_the_model`,
`test_4_a_yes_with_a_change_drops_the_proposal_and_only_the_new_one_registers`). Además, decidir en el mismo
turno que propuso lanza `StaleConfirmation` (`test_9_a_loop_that_proposes_and_confirms_in_the_same_turn_fails_loudly`).

## P3. "Si el usuario cambia el motivo o la descripción"

**Qué dice la consigna.** La confirmación vale para esos parámetros exactos; si cambian después de confirmar,
hay que volver a confirmar.

**Por qué es un problema.** "Después de confirmar" tiene dos momentos distintos: antes de que el reclamo se
ejecute y después de que quedó registrado. Además, no hay ninguna herramienta para modificar un reclamo.

**Resolución.** La llamada del LLM a `crear_reclamo` no ejecuta nada: crea una acción pendiente atada a un
fingerprint de la herramienta y de los argumentos normalizados, sensible a mayúsculas y puntuación. Antes de
la ejecución, cualquier cambio es una propuesta nueva que reemplaza a la anterior; la anterior ya no se puede
confirmar y la nueva hay que confirmarla de nuevo. Después de la ejecución, un texto distinto es otro reclamo,
porque no existe una herramienta para modificar uno ya registrado: el agente no puede "corregir" un reclamo,
solo registrar otro, con su propia confirmación.

**Dónde.** `agent/domain/pending.py` (`fingerprint`, `PendingAction`, `propose`, `Confirmed`).

**Tests.** `test_agent_pending.py::test_the_fingerprint_changes_with_any_change_the_user_could_see`,
`test_c_a_changed_proposal_supersedes_and_only_the_new_one_is_confirmed`,
`test_a_confirmed_cannot_be_built_outside_the_gate`.

**Estado.** ✅ en el dominio y en el turno: una llamada a `crear_reclamo` solo crea la acción pendiente y la
pregunta de plantilla; una segunda llamada en la misma respuesta se ignora; el efecto solo se ejecuta con un
`Confirmed` (`tests/integration/agent/test_agent_policies.py::test_1_a_model_call_to_crear_reclamo_only_asks_for_confirmation`,
`test_8_two_proposals_in_one_reply_keep_only_the_first`, `tests/unit/agent/test_agent_architecture.py`).
Una revisión independiente (un subagente de Claude Code) notó que la prueba de la compuerta (`_GATE_PROOF`) se podía importar y con ella
fabricar un `Confirmed` fuera de `pending.py` sin que nada lo detectara. Cerrado en parte con un test que recorre el
AST de todos los módulos de `src/merchant_assistant` salvo `pending.py` y falla si encuentra una llamada
`Confirmed(...)` (también por un alias de import o como `modulo.Confirmed(...)`), cualquier mención de
`_GATE_PROOF` o su import
(`tests/unit/agent/test_agent_architecture.py::test_no_module_but_the_gate_can_build_a_confirmed`,
`test_the_scan_does_see_a_forged_confirmed`). Límite: no detecta una construcción por reflexión
(`object.__new__` con `object.__setattr__`, una subclase de `Confirmed` o un `getattr` con un nombre calculado), y no
mira `tests/` ni `evaluation/`. Es un freno contra el descuido, no una barrera contra código hostil.

## P4. "Sí" repetido sin nada pendiente

**Qué dice la consigna.** Repetir "sí" no debe registrar el reclamo dos veces.

**Por qué es un problema.** No dice qué se responde. Si el LLM contesta libremente, puede afirmar que registró
otro reclamo o llamar de nuevo a la herramienta.

**Resolución.** Con nada pendiente, `decide` devuelve `NothingPending` con la referencia del último reclamo
registrado, y la respuesta sale de una plantilla del código: no hay nada esperando confirmación, el último
reclamo tiene tal número y no se registró otro.

**Dónde.** `agent/domain/pending.py` (`decide`, `mark_registered`) y `agent/domain/templates.py` (`nothing_pending`).

**Tests.** `test_agent_pending.py::test_d_yes_with_nothing_pending_is_nothing_pending`,
`test_e_repeated_yes_after_registering_carries_the_reference`,
`test_agent_templates.py::test_nothing_pending_mentions_the_last_registration_only_when_there_is_one`.

**Estado.** ✅ en el dominio y en el turno, sin llamar al LLM
(`tests/integration/agent/test_agent_policies.py::test_3_repeating_yes_never_registers_again_and_carries_the_existing_id`,
`test_5_a_yes_after_changing_topic_finds_nothing_pending`). Excepción deliberada: si el mensaje anterior del
asistente propuso una fecha, el "sí" responde a esa pregunta y sigue hacia el modelo (ver P8).

## P5. Falla después de confirmar

**Qué dice la consigna.** Un reclamo confirmado no se registra dos veces aunque la herramienta falle y se
reintente. DOC-11 agrega: "Ante un timeout, no afirmar que el reclamo se creó sin verificar".

**Por qué es un problema.** Después de una falla no se sabe si el reclamo se guardó (la escritura pudo ocurrir y
perderse la confirmación). Si la acción pendiente se descarta, el usuario tiene que empezar de nuevo; si se
reintenta a ciegas, se podría duplicar.

**Resolución.** Confirmar no consume la acción pendiente: solo `mark_registered` la borra, y el turno lo llama
únicamente cuando el efecto se sabe ocurrido. Si la ejecución falla, la acción sigue pendiente, se le dice al
usuario con honestidad que no se pudo confirmar si quedó guardado, y un "sí" posterior reintenta con el mismo
fingerprint y la misma clave de idempotencia. El reintento es la verificación que pide DOC-11: si la escritura
había ocurrido, la base devuelve el reclamo existente y la respuesta dice que ya estaba registrado.

**Dónde.** `agent/domain/pending.py` (docstring del módulo, `decide`, `mark_registered`),
`agent/domain/templates.py` (`execution_failed`, `registered`) y `complaints/adapters/sqlite_repository.py`.

**Tests.** `test_agent_pending.py::test_g_a_failed_execution_can_be_retried_with_the_same_fingerprint`,
`test_agent_templates.py::test_a_failed_execution_is_honest_and_promises_no_duplicate_on_retry`,
`tests/integration/complaints/test_sqlite_repository.py::test_a_lost_acknowledgement_writes_the_row_and_the_retry_finds_it`.

**Estado.** ✅ en el dominio, en la capacidad y en el turno. El ejecutor reintenta las fallas reintentables
con la misma clave; si todas fallan, la acción sigue pendiente y la respuesta es la plantilla de falla
(`tests/integration/agent/test_agent_policies.py::test_11_a_lost_acknowledgement_is_retried_with_the_same_key_and_registers_once`,
`test_11_a_timeout_forever_keeps_the_action_and_a_later_yes_registers_once`). Si un intento anterior de la
misma ejecución falló y el siguiente encuentra el reclamo, se informa como registrado ahora: lo más probable
es que lo haya escrito ese intento.

## P6. Qué significa "herramienta disponible"

**Qué dice la consigna.** No ejecutar herramientas que no estén disponibles.

**Por qué es un problema.** Leída al pie de la letra, "disponible" sería "existe en el registro". Pero la Parte 3
quita herramientas según el estado: en HUMAN y BLOCKED no hay ninguna (F13). Una herramienta registrada puede no
estar disponible ahora.

**Resolución.** La guarda recibe el conjunto de herramientas ofrecidas en este turno, no el registro completo, y
exige que el nombre sea exactamente una de sus claves. Un nombre desconocido, con otra capitalización, con un
espacio de más o de una herramienta que el estado no ofrece se rechaza igual, con `ToolNotAvailable`, antes de
mirar los argumentos.

**Dónde.** `agent/domain/guards.py` (`authorize`) y `control/capabilities.py` (qué permite cada estado).

**Tests.** `test_agent_guards.py::test_a_name_that_is_not_an_exact_offered_key_is_not_available`,
`test_a_tool_the_current_state_does_not_offer_is_not_available`,
`test_availability_is_checked_before_the_arguments_are_looked_at`.

**Estado.** ✅ guarda en el dominio y en el turno: `offered_tools` construye un solo conjunto a partir de las
capacidades y ese mismo conjunto se le ofrece al modelo y se le pasa a la guarda
(`tests/integration/agent/test_agent_policies.py::test_7_a_tool_that_is_not_an_exact_offered_name_is_rejected`,
`test_7_an_effect_call_is_rejected_where_only_read_tools_are_offered`,
`test_17_a_state_that_generates_no_reply_never_calls_the_model`).

## P7. El dataset tiene una venta en el futuro

**Qué dice la consigna.** `ventas.json` debe incluir una fecha futura.

**Por qué es un problema.** Si la herramienta la devolviera, el agente informaría como hecho una venta que todavía
no ocurrió.

**Resolución.** El dataset tiene a propósito una preventa en 2027, y el contrato de fecha de `consultar_ventas`
rechaza toda fecha posterior al día de negocio, que se inyecta. El dato existe en el archivo y nunca llega al
usuario como un hecho.

**Dónde.** `sales/contract.py` (`parse_sales_date`) y `data/ventas.json`.

**Tests.** `tests/unit/sales/test_contract.py::test_a_date_after_today_is_refused_as_future`,
`tests/integration/sales/test_dataset.py::test_at_least_one_record_is_after_the_current_date`.

**Estado.** ✅ implementado en la capacidad `sales`.

## P8. Fechas relativas o ambiguas

**Qué dice la consigna.** Pedir datos ante información incompleta o ambigua, incluidas fechas relativas y
ambiguas.

**Por qué es un problema.** "Ayer" depende de la zona horaria y del día de negocio; "03/04" puede ser 3 de abril o
4 de marzo. Si el modelo o la herramienta los interpretan, el resultado es una suposición presentada como dato.

**Resolución.** El contrato de la herramienta acepta solo `YYYY-MM-DD` con dígitos ASCII y rechaza todo lo demás
con `bad_format`, sin interpretar nada. La descripción de la herramienta le dice al modelo que aclare con el
usuario las fechas relativas o ambiguas antes de llamarla.

**Dónde.** `agent/domain/tools.py` (`ConsultarVentasArgs` y la descripción de `consultar_ventas`) y
`sales/contract.py` (validez de calendario y fechas futuras).

**Tests.** `test_agent_guards.py::test_arguments_outside_the_contract_are_refused_with_the_exact_issues` (casos
`relative date`, `ambiguous date`, `unpadded date`, `datetime`), `test_agent_tools.py::test_the_sales_tool_asks_for_an_iso_date_and_to_clarify_relative_ones`,
`tests/unit/sales/test_contract.py::test_anything_but_a_real_yyyy_mm_dd_date_is_refused`.

**Estado.** ✅ contrato y ✅ guardia en el turno. El contrato solo mira la forma, así que un modelo podía
convertir "ayer" en una fecha ISO por su cuenta. Ahora `consultar_ventas` solo corre si la fecha está escrita
sin ambigüedad en un mensaje del usuario, o si el asistente la propuso en el mensaje inmediatamente anterior y
el usuario la aceptó con un "sí" estricto (`agent/domain/dates.py`, `is_grounded`). Si no, la herramienta no
corre y el modelo recibe `date_not_confirmed`. El costo: si el modelo entendió bien "ayer", igual tiene que
preguntar una vez. Tests: `tests/unit/agent/test_agent_dates.py` (tabla con `03/04`, `3/4/2026`,
`13/04/2026`, `31/02/2026`, `ayer`, `el viernes pasado` y formas largas),
`tests/integration/agent/test_agent_policies.py::test_12_a_model_that_resolves_ayer_by_itself_is_stopped_before_the_tool`,
`test_12_a_date_the_assistant_proposed_and_the_user_accepted_passes`,
`test_12_an_ambiguous_day_and_month_is_stopped_whichever_way_it_is_read`. Con el modelo real, la garantía
del código se sostiene y la conducta del modelo se mide aparte: las fechas relativas y ambiguas (S05, S06, H03)
se resuelven preguntando en 3 de 3 corridas, pero el modelo también pide confirmar fechas completas y sin
ambigüedad ("5 de octubre de 2026", H02: 0 de 3) y fechas que el cliente da dentro de un reclamo (S19), lo que
el código no puede evitar sin dejar de ser conservador (ver el apartado siguiente).

Una revisión independiente (un subagente de Claude Code) encontró un hueco, ya cerrado: la pregunta de confirmación de un reclamo es un
mensaje del asistente que muestra la descripción escrita por el modelo. Si el "sí" del cliente no confirmaba
el reclamo (la versión de control había cambiado, o el cierre de la franja lo retiró en SUGGEST), el turno
seguía al modelo y una fecha de esa descripción contaba como propuesta y aceptada. Ahora la pregunta se guarda
con `Author.GATE` y `dates_proposed_in` no lee fechas de ella: un "sí" confirma el reclamo, nunca una fecha
(`tests/unit/agent/test_agent_dates.py::test_a_yes_to_the_gates_question_never_accepts_a_date_its_description_mentions`,
`tests/integration/agent/test_agent_control_scenarios.py::test_11_a_yes_that_does_not_confirm_the_complaint_does_not_ground_its_date`,
`test_11_a_yes_to_a_question_the_closing_time_withdrew_finds_nothing_pending`). Dos límites conocidos, sin corregir:

1. Un "sí" a un mensaje del asistente que menciona varias fechas las ancla a todas (`dates_stated_by_assistant`
   devuelve el conjunto). Aceptar solo cuando el mensaje menciona exactamente una cierra este caso y ningún otro.
2. **Hueco residual, reproducido con un modelo guionado:** el arreglo de `Author.GATE` solo cubre la pregunta de
   la compuerta. Si el *modelo* escribe un mensaje propio (`Author.AGENT`) con una fecha y una pregunta sobre otra
   cosa ("Lamento lo de las ventas del 22/10/2026. ¿Querés que registre un reclamo?"), el "sí" siguiente ancla
   el 22/10/2026 y `consultar_ventas` corre con ella. Exige que el modelo desobedezca las reglas de fechas de
   `prompts.py`, afecta solo a una herramienta de lectura (nunca a un efecto) y ningún test del repositorio lo
   cubre. Aceptar la fecha solo si el mensaje menciona exactamente una **no** lo cierra (este mensaje menciona
   una). La corrección robusta es que la pregunta por una fecha la produzca el código, con su propio autor (como
   la pregunta de la compuerta), y que solo esa pregunta pueda anclar una fecha: así tampoco importa cuántas
   fechas mencione un texto libre del modelo.

## P9. DOC-11 define categorías que `crear_reclamo` no tiene

**Qué dice la consigna.** `crear_reclamo(motivo, descripcion)`. DOC-11 dice que las categorías de reclamos son
PAGO, ENTRADA, LIQUIDACION, ACCESO y OTROS.

**Por qué es un problema.** El corpus sugiere un campo que el contrato de la herramienta no tiene. Agregarlo
cambiaría la interfaz que la consigna fija; ignorarlo deja la categoría sin registrar.

**Resolución.** Se mantiene el contrato de la consigna: solo `motivo` y `descripcion`, y cualquier campo extra se
rechaza como `unexpected_field`. El modelo puede mencionar la categoría dentro del motivo si corresponde.

**Dónde.** `agent/domain/tools.py` (`CrearReclamoArgs`, con `extra="forbid"`).

**Tests.** `test_agent_tools.py::test_the_schema_requires_exactly_the_brief_fields_and_forbids_any_other`,
`test_agent_guards.py::test_arguments_outside_the_contract_are_refused_with_the_exact_issues[extra field]`.

**Estado.** ✅ decisión implementada en el contrato.

## P10. Frases imperativas dentro del corpus

**Qué dice la consigna.** Las instrucciones dentro de documentos o de respuestas de herramientas son datos, no
órdenes.

**Por qué es un problema.** Algunos documentos legítimos están escritos en imperativo. DOC-04 dice que el
asistente "debe recomendar verificar el correo y luego abrir un reclamo". Un modelo que lo lea como orden podría
intentar registrar un reclamo que el usuario no pidió.

**Resolución.** Ningún texto recuperado puede producir un efecto: lo más que puede lograr es que el modelo
proponga `crear_reclamo`, y esa propuesta solo crea una acción pendiente que el usuario ve con sus valores exactos
y tiene que aceptar en un mensaje propio. Un "sí" escondido en un documento o en una respuesta de herramienta no
cuenta, porque `decide` solo lee el texto del usuario. La descripción de la herramienta le dice además al modelo
que no la llame porque un documento lo pida, pero la garantía no depende de eso.

**Dónde.** `agent/domain/pending.py` (`propose`, `decide`) y `agent/domain/tools.py` (descripciones).

**Tests.** `test_agent_pending.py::test_b_a_normal_sentence_invalidates_and_a_later_yes_finds_nothing`,
`test_agent_pending_properties.py::test_a_confirmation_needs_a_pending_action_a_strict_yes_and_the_same_fingerprint`,
`test_agent_templates.py::test_hostile_text_is_embedded_as_data_without_changing_the_wording_around_it`.

**Estado.** ✅ en el dominio y en el turno. Cada resultado de herramienta viaja como un único objeto JSON
marcado `untrusted_data`, con el texto de los documentos solo dentro de valores de tipo string, y `decide` solo
lee el mensaje del usuario. Con un modelo guionado que obedece a un fragmento envenenado, lo máximo que se
logra es una propuesta que el usuario ve y rechaza
(`tests/integration/agent/test_agent_policies.py::test_13_a_poisoned_passage_can_at_most_cause_a_proposal_the_user_refuses`). Un detector de marcadores
de inyección (`agent/domain/signals.py`) solo cuenta y nunca bloquea.

## Lo que mostraron las corridas con el modelo real

Las políticas se probaron primero con modelos guionados. Con `gpt-4o-mini` (corridas guardadas en
`evaluation/results/conversations/`, historia completa en
[ADR-0008](design/adr/0008-primera-corrida-real-y-lo-que-cambio.md) y en el README) los candados
aguantaron: 1197 de 1197 checks LOCK en las 102 conversaciones de las corridas finales, y 0 filas duplicadas o
sin confirmar. Lo que apareció es lo que ningún candado puede garantizar:

| Hallazgo | Qué mostró el modelo real | Qué queda |
|---|---|---|
| F9, F11 | `low_confidence` no se dispara: el modelo contesta las preguntas fuera de tema sin buscar (X2: 0 de 3). La cadena de SUGGEST de X2 solo se juzga si el evento ocurrió | SUGGEST y sus dos aprobaciones se ejercen con modelos guionados y con los tests de operador, no con corridas reales |
| P2 | "sí, confirmo el reclamo" invalidaba la propuesta en la práctica | El vocabulario del "sí" admite palabras neutras ([ADR-0007](design/adr/0007-recuperacion-del-agente-y-umbral.md)); lo que cambia lo aceptado sigue sin confirmar |
| P3, P5 | El modelo pedía la confirmación con sus palabras o seguía diciendo "respondé sí" con la propuesta ya retirada, y el "sí" caía en "nada pendiente" (S12, S14, S19) | El prompt dice que llamar a `crear_reclamo` *es* pedir la confirmación, y el código le escribe al historial una nota de plataforma cuando retira una propuesta (S22) |
| P8 | Pide confirmar fechas completas (H02 0 de 3) y fechas dentro de un reclamo (S19 0 de 3; X1 y H07 2 de 3) | Medido, no corregido: dos reglas generales de prompt probadas contra H02 y H01 no lo arreglaron y se revirtieron |
| P10 | El modelo no obedeció la orden escondida en el documento envenenado (S17, H04: ninguna propuesta) ni intentó las herramientas de contrabando (S19) | Con el modelo real esos candados no se ejercieron; sí con el guion hostil. Una orden citada en la respuesta no se retiene |
| F10 | Cuando el modelo no llegó a proponer el reclamo, el takeover no tenía nada que descartar (X1, H07) y el check de la bandeja no aplicó | El candado no falla; falla la utilidad de la conversación |

## Alternativas consideradas

Otro análisis de la misma especificación, generado por Gemini a pedido del autor para auditar posibles trampas
(no lo escribió el autor; es independiente de esta implementación, no de la IA), coincidió en casi todo y
difirió en dos decisiones de diseño. Se dejan registradas, con el motivo de no adoptarlas.

- **F2: qué hacer con BLOCKED + `operator_release`.** Ese análisis propuso borrar la fila y exigir además el rol
  SUPERVISOR en `supervisor_unblock`. Esta implementación conserva la fila y restringe su actor a SUPERVISOR.
  El resultado de seguridad es el mismo: ningún operador sale de BLOCKED. La diferencia es observable solo en el
  rechazo: un operador recibe `unauthorized_actor` en lugar de `invalid_transition`, que dice con más precisión
  qué falló (el par existe, el actor no alcanza). Además la tabla de la especificación queda intacta, y la
  corrección se limita a una columna.
- **F5: cómo se sabe que es fuera de horario.** El mismo análisis propuso un booleano persistido `is_out_of_hours`
  y un evento `business_hours_started` para volver a ponerlo en falso. Esta implementación deriva "fuera de
  horario" del reloj en cada decisión (`BusinessHours.is_open`). Un indicador guardado puede divergir del reloj
  real (una conversación inactiva durante la noche seguiría "cerrada" a las 10), y el evento inverso necesitaría
  un proceso que lo dispare en todas las conversaciones, que nada en el sistema tiene.

Una observación más de ese análisis: señaló una posible instrucción inyectada dentro del corpus. En el kit no
hay ninguna. Lo que sí hay son frases en imperativo dirigidas al agente (por ejemplo DOC-04: "debe recomendar
verificar el correo y luego abrir un reclamo"), que aquí se tratan como datos y no pueden habilitar ningún
efecto (ver P10).

---

## Supuestos

- **La franja de atención humana es configuración, y su valor por defecto es un supuesto.** La consigna no la
  define. `HUMAN_HOURS_START`, `HUMAN_HOURS_END` y `HUMAN_HOURS_DAYS` (`shared/config.py`) valen por defecto
  de lunes a viernes de 09:00 a 18:00, un horario comercial típico elegido sin información del comercio.
  `bootstrap.build_business_hours` arma la guarda; el dominio de `control` no tiene ningún valor por defecto.
  Se evalúa en la zona horaria del comercio, el inicio es inclusivo, el fin es exclusivo y no se admiten
  franjas que crucen la medianoche.
- **Un comando del equipo sobre una conversación que no existe la crea.** Un `takeover` de un identificador
  nuevo deja una conversación en HUMAN sin mensajes; un comando rechazado deja su registro REJECTED.
- **El desbloqueo de un supervisor reinicia los avisos de abuso.** Si no, el insulto siguiente volvería a
  bloquear al instante, aunque el supervisor ya haya revisado el caso.
- **El estado inicial de toda conversación es AUTO.** La consigna no lo dice; el registro vacío equivale a AUTO.
