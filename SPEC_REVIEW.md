# Revisión de la especificación de la máquina de estados IA-humano

La especificación de la Parte 3 tiene contradicciones, ambigüedades y casos que faltan. Este documento
lista cada hallazgo, la decisión tomada y dónde queda implementada y probada. La implementación vive en
`src/merchant_assistant/control/` y no depende de ninguna librería de máquinas de estados.

Estados de implementación:

- ✅ implementado en el paquete `control` y probado.
- ⏳ el paquete `control` emite la señal (un efecto, un error o una capacidad), pero la integración con el
  agente, todavía pendiente, debe actuar sobre ella.

## Resumen

| # | Tipo | Hallazgo | Resolución | Estado |
|---|---|---|---|---|
| F1 | Contradicción | HUMAN + `abuse_detected` aparece dos veces con destinos distintos | Queda en HUMAN y avisa al operador; nuevo evento `operator_block` | ✅ |
| F2 | Contradicción | BLOCKED + `operator_release` contradice la regla 4 | Se conserva la fila, restringida al supervisor | ✅ |
| F3 | Contradicción | "Cualquiera + `out_of_hours_started`" desbloquearía por reloj | Cuatro filas explícitas; BLOCKED sigue BLOCKED | ✅ |
| F4 | Contradicción | La regla 3 solo se modela al entrar en AUTO | Guarda de horario abierto en tres eventos | ✅ guarda / ⏳ informar |
| F5 | Caso faltante | No hay evento ni regla de retorno al abrir la franja | El horario es una guarda, no un estado | ✅ |
| F6 | Ambigüedad | Los actores no están definidos | Tabla de actores por evento; SUPERVISOR incluye a OPERATOR | ✅ |
| F7 | Caso faltante | SUGGEST + `customer_requests_human` no está definido | Pasa a HUMAN, con guarda de horario | ✅ |
| F8 | Caso faltante | Qué pasa con los eventos redundantes o sin sentido | Fallan de forma explícita con `InvalidTransition` | ✅ |
| F9 | Ambigüedad | `low_confidence` no está definida y la confianza del LLM no está calibrada | Debe salir de una señal determinista de retrieval | ⏳ |
| F10 | Interacción con la Parte 2 | Un reclamo a medio confirmar sobrevive a un cambio de control | `INVALIDATE_PENDING_ACTION` en todo cambio de estado | ✅ efecto / ⏳ agente |
| F11 | Interacción con la Parte 2 | Orden de la regla 6 en SUGGEST | Confirmación del usuario, aprobación del operador, ejecución | ✅ capacidad / ⏳ agente |
| F12 | Caso faltante | Los borradores no deben filtrarse al salir de SUGGEST | `DISCARD_UNAPPROVED_DRAFTS` al salir de SUGGEST | ✅ efecto / ⏳ bandeja de salida |
| F13 | Ambigüedad | "Ejecuta acciones" en HUMAN: ¿cuentan las herramientas de lectura? | Lectura estricta: ninguna herramienta en HUMAN ni BLOCKED | ✅ |
| F14 | Ambigüedad | La regla 5 no dice si los intentos rechazados se registran | Se registran ambos, distinguidos por resultado y motivo | ✅ |
| F15 | Ambigüedad | "Nadie responde" en BLOCKED no dice qué pasa con los mensajes entrantes | Se registran; no se genera ni se envía nada | ✅ capacidad / ⏳ registro |
| F16 | Caso faltante | Carrera: un operador toma el control mientras el LLM está respondiendo | Se decide la entrega contra el estado leído después de generar | ⏳ |

Los tests citados están en `tests/unit/control/`. Cuando un test está parametrizado, el nombre del caso
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

**Dónde.** `rules.py` y `machine.py` (`effects_for`).

**Tests.** `test_transition_table.py::test_every_defined_rule_leads_to_its_target[BLOCKED+out_of_hours_started]`
y el resto de las filas del evento, `test_effects.py::test_every_rule_carries_exactly_the_effects_of_record[AUTO+out_of_hours_started]`
y `[BLOCKED+out_of_hours_started]`, `test_conversation.py::test_a_blocked_conversation_stays_blocked_through_closing_time`.

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

**Estado.** ✅ guarda implementada en `control`. ⏳ la aplicación debe decirle al cliente la franja de atención.

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
la opinión de quien debería ser supervisado.

**Resolución.** El evento solo lo dispara SYSTEM, y debe originarse en una señal determinista de retrieval:
ninguna coincidencia, o un puntaje por debajo de un umbral calibrado con datos. Nunca de lo que el modelo diga
de sí mismo. `control` solo recibe el evento ya decidido.

**Dónde.** `rules.py` (solo SYSTEM puede disparar `low_confidence`). La señal se calcula fuera de este
paquete.

**Tests.** `test_transition_table.py::test_only_the_system_reports_abuse_or_low_confidence`. El umbral y su
calibración se prueban cuando exista el retrieval.

**Estado.** ⏳ `control` define quién puede emitir el evento; el cálculo de la señal y su umbral pertenecen
a la integración con el agente y el retrieval.

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

**Estado.** ✅ efecto emitido por `control`. ⏳ el agente debe borrar su acción pendiente cuando recibe el efecto.

## F11. Orden de la regla 6 en SUGGEST

**Qué dice la especificación.** En SUGGEST, `crear_reclamo` requiere la aprobación del operador además de la
confirmación del usuario. No dice en qué orden ni qué pasa si el operador edita el texto.

**Por qué es un problema.** Sin un orden, el operador podría aprobar parámetros que el usuario nunca vio, o
el usuario confirmar algo que el operador después cambió.

**Resolución.** El orden es: confirmación del usuario, aprobación del operador, ejecución. Si el operador edita
los parámetros, cambia el fingerprint de la acción pendiente (ADR-0001) y el usuario debe confirmar de nuevo.
La pregunta de confirmación es en sí un borrador: no llega al cliente hasta que el operador la aprueba.
`control` expresa solo la parte de estado: en SUGGEST, `effects_need_operator_approval` es verdadero, y solo ahí.

**Dónde.** `capabilities.py` (`Capabilities.effects_need_operator_approval`).

**Tests.** `test_capabilities.py::test_a_complaint_needs_the_operator_only_while_the_operator_reviews`,
`test_a_suggested_reply_is_never_sent_without_approval`.

**Estado.** ✅ indicador de capacidad en `control`. ⏳ el agente debe hacer cumplir el orden y volver a pedir
confirmación cuando cambia el fingerprint.

## F12. Los borradores no deben filtrarse al salir de SUGGEST

**Qué dice la especificación.** En SUGGEST el agente nunca envía sin aprobación. No dice qué pasa con los
borradores pendientes cuando el estado cambia.

**Por qué es un problema.** Un borrador pendiente que se aprobara o enviara después de salir de SUGGEST (el
operador tomó la conversación, o se bloqueó por abuso) llegaría al cliente fuera de contexto, o por encima de
un bloqueo.

**Resolución.** Toda transición que sale de SUGGEST emite `DISCARD_UNAPPROVED_DRAFTS`. Entrar a SUGGEST no lo
emite.

**Dónde.** `machine.py` (`effects_for`).

**Tests.** `test_effects.py::test_leaving_suggest_discards_the_drafts_nobody_approved`,
`test_entering_suggest_discards_nothing`,
`test_every_rule_carries_exactly_the_effects_of_record[SUGGEST+...]` (las cinco filas de SUGGEST),
`test_conversation.py::test_leaving_suggest_at_closing_time_discards_the_drafts`.

**Estado.** ✅ efecto emitido por `control`. ⏳ la bandeja de salida debe descartar los borradores al recibirlo.

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

**Estado.** ✅ implementado en `control`.

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

**Estado.** ✅ capacidad implementada en `control`. ⏳ la aplicación debe registrar los mensajes entrantes
mientras la conversación está bloqueada.

## F16. Carrera entre un takeover y una llamada al LLM en curso

**Qué dice la especificación.** Nada. El estado se consulta al comienzo del turno.

**Por qué es un problema.** La llamada al modelo tarda segundos. Si un operador toma la conversación (o se
detecta abuso) mientras tanto, la respuesta generada con el estado anterior llegaría al cliente después del
cambio de control, y se enviaría desde un estado que ya no la permite.

**Resolución.** La entrega se decide contra el estado leído después de la generación, no antes. Si cambió, la
respuesta se descarta. `ConversationControl.state` siempre refleja el último registro, así que esa lectura es
barata y consistente.

**Dónde.** No está en `control`: pertenece al caso de uso del turno en la capa de aplicación del agente.

**Tests.** Pendientes hasta que exista el turno del agente; se cubrirán con un modelo guionado que cambie el
estado durante la generación.

**Estado.** ⏳ pendiente en la integración con el agente.

---

## Supuestos

- **La franja de atención humana es configuración.** La consigna no la define, por lo que `BusinessHours` se
  inyecta desde la raíz de composición y el dominio no tiene ningún valor por defecto. Se evalúa en la zona
  horaria del comercio, `opens` es inclusivo, `closes` es exclusivo y no se admiten franjas que crucen la
  medianoche.
- **El estado inicial de toda conversación es AUTO.** La consigna no lo dice; el registro vacío equivale a AUTO.
