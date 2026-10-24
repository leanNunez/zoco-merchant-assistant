# ADR-0004: El control de la conversación y los operadores

- **Estado:** Aceptada
- **Fecha:** 2026-10-24

## Contexto

La máquina de estados (`control/`) ya decidía quién puede cambiar de manos una conversación y
qué efectos siguen a cada transición, y el turno del agente (ADR-0003) ya respetaba las
capacidades de cada estado. Faltaba el otro lado: nadie podía tomar, liberar, bloquear o
desbloquear una conversación, los efectos solo quedaban anotados, en SUGGEST no había forma de
aprobar un borrador, y la regla 6 (en SUGGEST, `crear_reclamo` requiere la aprobación del
operador además de la confirmación del cliente) no tenía flujo. `low_confidence` no lo disparaba
nada, y la salida del modelo todavía podía afirmar un registro, citar un documento entre
paréntesis o inventar un monto sin que nadie lo notara.

## Decisión

1. **Un servicio para el equipo, sobre el mismo almacén.** `OperatorService`
   (`agent/application/operator.py`) comparte el `ConversationStore` con el turno del cliente y
   escribe con la misma concurrencia optimista: un comando que llega a mitad de un turno hace que
   ese turno descarte su respuesta (F16), y un comando que pierde la carrera se repite sobre el
   estado nuevo en lugar de pisarlo. `takeover`, `release`, `block` y `unblock` disparan su
   evento por `ConversationControl`, cuyo registro es la auditoría (estado anterior, nuevo,
   evento, actor y fecha; un rechazo queda como REJECTED con su código). Los rechazos de control
   se guardan y se relanzan sin cambios. Todo comando sincroniza primero el horario, igual que un
   turno: después del cierre la conversación vuelve al agente antes que nada. Al cargar un estado,
   cada transición aplicada del registro se vuelve a validar contra las reglas, así que un estado
   guardado no puede afirmar una salida de BLOCKED sin supervisor. El equipo ve los borradores
   como `DraftView`, sin el `Confirmed` que guarda una aprobación.
2. **Los efectos se ejecutan en un solo lugar.** `apply_effects` (`agent/domain/effects.py`) la
   usan el turno y los comandos: `INVALIDATE_PENDING_ACTION` descarta la acción pendiente,
   `DISCARD_UNAPPROVED_DRAFTS` marca como DISCARDED todo borrador pendiente (queda en el
   registro y no se puede aprobar ni enviar), y `NOTIFY_OPERATOR` agrega una entrada a la bandeja
   del operador, guardada en el estado de la conversación. El desbloqueo de un supervisor además
   pone en cero los avisos de abuso que causaron el bloqueo.
3. **El operador escribe solo en HUMAN, y se nota.** `operator_reply` entrega el texto y lo
   guarda como mensaje del asistente marcado con `Author.OPERATOR`, así el historial y las
   transcripciones distinguen a una persona del agente, y el modelo ve lo que dijo el operador
   después de liberar.
4. **En SUGGEST, dos aprobaciones en un orden estricto.**
   1. Toda respuesta del turno es un borrador REPLY, fuera del historial hasta que se aprueba.
   2. La llamada del modelo a `crear_reclamo` es un borrador CONFIRMATION_REQUEST con la
      `PendingAction`, y **no queda nada pendiente**.
   3. Cuando el operador lo aprueba, la pregunta (de plantilla) se entrega y recién ahí se
      propone la acción, con el turno y la versión de control de ese momento.
   4. El "sí" estricto del cliente produce un `Confirmed`; en SUGGEST no ejecuta nada y queda un
      único borrador ACTION_APPROVAL que lo contiene (un "sí" repetido no pide dos veces; un "no"
      u otro mensaje lo descarta).
   5. Aprobarlo llama a `execute_confirmed`, luego a `mark_registered`, y envía la plantilla
      `registered`. Rechazarlo cancela la acción y envía `not_approved`.

   No hay comando para editar los parámetros: un operador que quiere otros valores rechaza, y el
   cliente los vuelve a decir y a confirmar.
5. **`low_confidence` sale del retrieval, nunca del modelo.** `SearchAdapter` (`bootstrap.py`)
   devuelve `no_data` para toda consulta cuyo mejor puntaje BM25 sobre el corpus está por debajo
   de `MIN_BM25_SCORE`, diga lo que diga el retriever (también en modo híbrido, donde el lado
   vectorial siempre encuentra vecinos). En AUTO, si el modelo buscó y todas las búsquedas del
   turno dieron `no_data`, el turno dispara `low_confidence` (actor SYSTEM) antes de entregar. En
   horario la conversación pasa a SUGGEST y esa misma respuesta queda como borrador; fuera de
   horario la guarda lo rechaza, el rechazo queda registrado y la respuesta sale con el aviso de
   la franja. La confianza que el modelo diga tener no se usa nunca.
6. **Controles de salida, segunda versión** (`agent/domain/output_checks.py`):
   - toda respuesta del modelo que afirma que un reclamo quedó registrado se retiene
     (`registration_claim`): los registros los anuncian solo las plantillas del código;
   - se revisa toda mención `DOC-NN`, con corchetes, paréntesis o suelta; en un grupo se borran
     los ids desconocidos, y uno suelto retiene la respuesta;
   - un monto de dinero que no coincide con ninguna cifra de los resultados de herramientas de la
     conversación se marca como `unverified_amount` en `TurnResult.flags`, sin retener;
   - una excepción que no es `DomainError` dentro del ejecutor se registra en el log con su traza
     y se convierte en `internal_error`, no reintentable, con un mensaje fijo.
7. **La franja humana es configuración.** `HUMAN_HOURS_START`, `HUMAN_HOURS_END` y
   `HUMAN_HOURS_DAYS` (por defecto de lunes a viernes de 09:00 a 18:00), validadas al cargar.
   `shared` no puede importar `control`, así que `bootstrap.build_business_hours` arma la guarda.
   La consigna no define la franja: el valor por defecto es un supuesto.

### Por qué la propuesta espera a la entrega

La compuerta (ADR-0001, ADR-0003) garantiza que un `Confirmed` corresponde a los valores exactos
que el cliente vio. Si la acción quedara pendiente cuando el modelo la propone en SUGGEST, un "sí"
del cliente (a otra cosa, o antes de que el operador apruebe la pregunta) podría confirmar algo
que nunca se le mostró. Proponerla al aprobar el borrador hace que esa propiedad valga sin
excepciones: la pregunta ya se entregó, y el cliente responde necesariamente en un turno
posterior y bajo la misma versión de control.

### La calibración del umbral

Se calculó sin la API, con el chunking del agente (`section`) y el tokenizador de BM25 del
agente, el mejor puntaje de cada pregunta:

| Conjunto | Preguntas | Mínimo | Máximo |
|---|---|---|---|
| Kit (`questions.jsonl`) | 20 | 5,871 (Q13) | 26,707 (Q05) |
| Fuera de tema, escritas antes de elegir el umbral | 16 | 0,000 | 4,626 ("¿Cómo se hace un asado?") |

Las distribuciones no se solapan. El umbral es el punto medio, **5,25**, con 0,62 de margen hacia
cada lado. Ninguna pregunta fuera de tema pasa y todas las del kit pasan.

## Alternativas consideradas

| Alternativa | Por qué se descartó |
|---|---|
| Dejar la acción pendiente cuando el modelo la propone en SUGGEST | Un "sí" podría confirmar valores que el cliente todavía no vio |
| Que el operador apruebe primero y el cliente confirme después, sin borrador de pregunta | La pregunta llegaría al cliente sin aprobación, y en SUGGEST no se envía nada sin aprobar |
| Un comando para que el operador edite los parámetros | Obliga a volver a confirmar igual; rechazar y que el cliente repita es más simple y no abre otra puerta al efecto |
| Pedirle al modelo su nivel de confianza | No está calibrado y es justo lo que hay que supervisar |
| Umbral sobre el puntaje coseno | Requiere embeddings reales y sin calibración no dice nada; se deja para cuando exista la caché |
| Umbral relativo (puntaje sobre el máximo posible de la consulta) | Trataría mejor las consultas de una palabra, pero con 20 preguntas no hay datos para calibrarlo; queda como mejora |
| Retener todo monto no verificado | Hay valores derivados legítimos (un cargo calculado con una tasa documentada); se marca y la evaluación lo mide |

## Consecuencias

**A favor**

- Una conversación cruza todos sus estados con comandos reales, con auditoría completa, y los
  efectos se ejecutan igual desde el turno y desde los comandos.
- En SUGGEST nada llega al cliente sin aprobación y un reclamo necesita las dos aprobaciones en
  orden; lo prueban escenarios y dos propiedades de Hypothesis contra SQLite real.
- `low_confidence` es determinista, reproducible y medido.

**En contra**

- El umbral se calibró sobre preguntas completas. Una consulta de una o pocas palabras comunes
  ("liquidación", 2,98; "entrada no llegó", 2,92) queda por debajo: en horario va a SUGGEST
  (falla segura, la revisa una persona) y fuera de horario el agente dice que no encontró
  información (falla molesta). La descripción de la herramienta le pide al modelo la pregunta
  completa, pero es un pedido, no una garantía.
- El umbral cubre solo el lado léxico; el lado vectorial necesita embeddings reales para
  calibrarse.
- Cualquier intento de cambio de control, aunque se rechace, cambia la versión de control y
  anula una aprobación pendiente de un reclamo (la lectura conservadora de ADR-0003): un operador
  que intenta bloquear en SUGGEST por error obliga al cliente a confirmar de nuevo.
- El `Confirmed` vive dentro del borrador en memoria y no se puede serializar a propósito. Un
  almacén persistente necesitaría otro diseño de la prueba de confirmación.
- Un reclamo que un turno registra y que después pierde la carrera de la entrega (un operador
  tomó la conversación justo entonces) queda guardado y registrado en el estado, pero no se le
  anuncia al cliente: la bandeja del operador lo avisa.
- Los controles de salida son patrones: hay paráfrasis que no detectan (por ejemplo "ya lo dejé
  asentado", o "abrí un reclamo", que en voseo también es un imperativo) y frases que retienen de
  más.
- El aviso de traspaso o de suspensión sigue saliendo en el turno en que la conversación pasa a
  HUMAN o BLOCKED (decisión de ADR-0003).

## Cómo se verifica

- `tests/integration/agent/test_agent_control_scenarios.py`: los escenarios que cruzan estados
  (takeover con un reclamo a medio confirmar, SUGGEST completo desde una pregunta fuera de tema y
  sus variantes, BLOCKED y el supervisor, fuera de horario, HUMAN y el historial que ve el
  modelo, la carrera, los controles de salida, la auditoría de cada comando) y dos propiedades de
  Hypothesis con intercalados aleatorios.
- `tests/integration/agent/test_agent_retrieval_floor.py`: la calibración sobre el corpus real.
- `tests/unit/agent/test_agent_operator.py`, `test_agent_drafts.py`, `test_agent_effects.py`,
  `test_agent_output_checks.py`, `test_agent_executor.py` y `test_agent_conversation.py`.

## Actualización (2026-10-24): la compuerta de `low_confidence` y los controles de salida

La descripción de `MIN_BM25_SCORE` de la decisión 5 y la sección "La calibración del umbral" quedan
**reemplazadas por [ADR-0007](0007-recuperacion-del-agente-y-umbral.md)**. Lo que sigue en pie es
la idea: `low_confidence` sale del retrieval y nunca de lo que el modelo diga de su confianza. Lo que
cambió es el cómo:

- El piso único de 5,25 sobre chunking `section` desaparece. La compuerta tiene dos señales (BM25 y el
  mejor coseno: `no_data` solo si ambas son débiles) y, sin embeddings, una sola (BM25, con un umbral
  casi vacío a propósito). Los umbrales salen de una regla declarada antes de medir, aplicada a 20
  preguntas del kit, 30 consultas cortas legítimas y 45 fuera de tema
  (`evaluation/results/retrieval/gate_calibration.md`).
- Las consultas cortas que el piso dejaba sin respuesta ("¿Cómo transfiero una entrada?") ahora
  responden: el escenario S20 dejó de ser una limitación conocida.
- A los controles de salida de la decisión 6 se suma `prompt_leak`: se retiene toda respuesta que
  contiene 40 caracteres seguidos del prompt de sistema vigente.
- Si embeber la consulta falla, la búsqueda cae al camino degradado para esa consulta y el turno
  registra `search_degraded:<código>`.
