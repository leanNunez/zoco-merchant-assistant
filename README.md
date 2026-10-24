# Merchant Assistant

Asistente conversacional en Python para un comercio de ZocoTickets. Responde con
información verificable de la documentación, consulta ventas, registra reclamos y
respeta quién controla la conversación: el agente o un operador humano.

Es un único sistema con cuatro partes que comparten repositorio:

1. **Buscador RAG desde cero** (`knowledge/`): chunking, BM25, coseno con numpy, RRF y metadata.
2. **Agente con herramientas y políticas** (`agent/`, `sales/`, `complaints/`): tool calling nativo y reglas 🔒 garantizadas en código.
3. **Máquina de estados IA-humano** (`control/`): AUTO, SUGGEST, HUMAN y BLOCKED.
4. **Evaluación** (`evaluation/`): métricas de retrieval y conversaciones repetidas con aserciones sobre hechos.

## Estado

En construcción. La base del repositorio (herramientas, contratos de arquitectura, CI
y reloj inyectado) está lista; el resto se marca abajo a medida que se entrega, con su
evidencia. Nada de la matriz figura como hecho sin un test o un resultado que lo respalde.

## Cómo ejecutarlo

Requisitos: Python 3.11 o superior y [uv](https://docs.astral.sh/uv/).

| Comando | Qué hace |
|---|---|
| `make setup` | Instala las dependencias del lockfile y los hooks de git |
| `make test` | Corre los tests en la zona horaria local y en UTC |
| `make chat` | Abre la consola: vistas de cliente, operador y supervisor sobre una misma conversación (requiere la [configuración](#configuración)) |
| `make diagram` | Regenera el diagrama de estados en `design/state-diagram.md` |
| `make check` | Lint, tipos, contratos de arquitectura y tests: lo mismo que corre CI |

Los comandos de indexado y evaluación se agregan a esta tabla cuando existen.

## Configuración

Las variables se leen del entorno y de un archivo `.env` en la raíz, que está en `.gitignore`: nunca
se versiona. El archivo `.env.example` se entrega por separado; mientras tanto, esta es la plantilla.
Copiala a `.env` y completá lo que necesites. Los valores vacíos significan "no configurado"; las
líneas comentadas muestran el valor por defecto y se descomentan solo para cambiarlo (un valor vacío
en esas variables es un error de configuración, y el programa lo dice al arrancar).

```dotenv
# Embeddings de la búsqueda híbrida. Sin clave, o con la caché incompleta, la búsqueda
# funciona en modo degradado (solo BM25 con la política de vigencia).
OPENAI_API_KEY=

# Modelo de chat. Con LLM_BASE_URL vacío se usa OpenAI y, si LLM_API_KEY está vacío, OPENAI_API_KEY.
# Con otro endpoint la clave de OpenAI NO se reutiliza: hace falta LLM_API_KEY.
LLM_API_KEY=
LLM_BASE_URL=
# LLM_PROVIDER=openai          # perfil de capacidades: openai | groq | generic
# LLM_MODEL=gpt-4o-mini
# LLM_TEMPERATURE=0.2          # entre 0 y 2
# LLM_TIMEOUT_SECONDS=30
# LLM_MAX_OUTPUT_TOKENS=700
# LLM_STRICT_TOOLS=false       # esquemas estrictos de herramientas; sin verificar contra la API real

# PIN de los roles de la consola. Vacíos: se generan al azar al arrancar y se muestran una vez.
# Si los definís, la consola nunca los imprime.
OPERATOR_PIN=
SUPERVISOR_PIN=

# Horario de atención humana (hora de BUSINESS_TIMEZONE; el fin es exclusivo).
# BUSINESS_TIMEZONE=America/Argentina/Buenos_Aires
# HUMAN_HOURS_START=09:00
# HUMAN_HOURS_END=18:00
# HUMAN_HOURS_DAYS=mon,tue,wed,thu,fri

# Fallas simuladas de consultar_ventas y crear_reclamo (0 nunca falla, 1 siempre falla).
# TOOL_FAILURE_RATE=0
# TOOL_FAILURE_SEED=0
# TOOL_FAILURE_MODES=timeout,error,lost_ack
```

**Perfil de desarrollo sin costo (Groq).** Cualquier endpoint compatible con la API de Chat
Completions sirve; para correr la consola sin gastar crédito de OpenAI:

```dotenv
LLM_PROVIDER=groq
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_MODEL=openai/gpt-oss-120b
LLM_API_KEY=
```

Los resultados que se reporten (evaluación y tablas) usan el modelo por defecto de OpenAI
(`gpt-4o-mini`), no este perfil. El perfil `groq` no envía `parallel_tool_calls` ni `strict` y
usa `max_tokens`; el motivo está en [ADR-0005](design/adr/0005-adaptador-llm-y-consola.md).

## La consola

`make chat` abre una consola con tres vistas **separadas** sobre la misma conversación en curso
(viven en un solo proceso, porque la prueba de confirmación de un reclamo existe solo en memoria).
Al arrancar imprime una línea con el modelo y el modo de recuperación activos, sin ningún secreto.
Si falta la clave del modelo, lo dice en español y termina con estado distinto de cero.

| Vista | Cómo se entra | Qué ve y qué puede hacer |
|---|---|---|
| Cliente (por defecto) | — | Escribe al asistente. Solo ve lo que el comercio le dijo: `Asistente: ...` y `Operador: ...`. Nunca un borrador, la bandeja, el estado, el registro de control, marcas ni infracciones. Comandos: `/hours`, `/help`, `/quit` |
| Operador | `/login operator <pin>` | `/state`, `/log`, `/inbox`, `/drafts`, `/approve <id>`, `/reject <id>`, `/takeover`, `/release`, `/block`, `/reply <texto>`. El texto sin `/` no se envía como cliente |
| Supervisor | `/login supervisor <pin>` | Todo lo del operador y `/unblock` (levantar una suspensión) |

`/logout` vuelve a la vista de cliente y es el momento en que esa vista muestra lo que el equipo le
entregó (un borrador aprobado, un mensaje de `/reply`). Un rol privilegiado se decide solo por el PIN
que se ingresó; el actor que recibe el servicio sale del rol, nunca de lo escrito.

Para recorrer el flujo completo de un reclamo (dos aprobaciones del equipo):

1. Como cliente, hacé una pregunta fuera de tema (por ejemplo "¿Cuál es la capital de Francia?"):
   si el modelo busca en la documentación y nada supera el piso de relevancia, la conversación pasa a
   SUGGEST y la vista de cliente queda en silencio, porque la respuesta es un borrador.
2. `/login operator <pin>`, `/drafts`, `/approve <id>` y `/logout`: el cliente recibe la respuesta.
3. Como cliente, pedí registrar un reclamo; el operador aprueba la pregunta de confirmación.
4. El cliente responde "sí"; el operador aprueba la acción (`/approve <id>`) y, al volver con
   `/logout`, el cliente ve el número `REC-...`.

Si la vista de cliente no responde nada, la conversación está en SUGGEST, HUMAN o BLOCKED: entrá como
operador y mirá `/state`. Pedir "quiero hablar con una persona" la pasa a HUMAN dentro del horario de
atención (por defecto, de lunes a viernes de 9:00 a 18:00 en Buenos Aires: ver `/hours`; fuera de
esa franja, tomar una conversación se rechaza). La conversación vive en memoria y se pierde al salir;
los reclamos quedan en `var/reclamos.db`. Los errores inesperados muestran una sola línea genérica y
su traza va a `var/chat.log` (en `.gitignore`).

**Los PIN son un simulador, no autenticación.** Si no definís `OPERATOR_PIN` y `SUPERVISOR_PIN`, la
consola genera uno al azar por rol y los imprime una vez, en un cartel DEMO, en la misma pantalla del
cliente; la terminal además muestra lo que se escribe. Sirve para separar vistas en una demostración;
no hay identidad por persona, ni bloqueo ante intentos fallidos, ni registro de quién es quién más allá
del rol. Los límites están en [ADR-0005](design/adr/0005-adaptador-llm-y-consola.md).

## Principio de diseño

**El LLM propone; el código dispone.** El modelo solo elige entre acciones fijas con
argumentos validados, y todo efecto pasa por guardas deterministas que quedan fuera de
él. Una instrucción dentro de un documento o de la respuesta de una herramienta es un
dato: no puede habilitar ninguna acción. Detalle y alternativas descartadas en
[`design/adr/0001-llm-propone-codigo-dispone.md`](design/adr/0001-llm-propone-codigo-dispone.md).

## Estructura

```
src/merchant_assistant/
  knowledge/    Parte 1: retrieval desde cero (solo stdlib y numpy en el dominio)
  sales/        consultar_ventas: contrato de fecha y repositorio
  complaints/   crear_reclamo: modelo, clave de idempotencia y repositorio
  control/      Parte 3: estados, eventos, roles y registro de transiciones
  agent/        Parte 2: domain/ (reglas puras), application/ (casos de uso),
                ports.py (interfaces), adapters/ (proveedores LLM, modelo guionado)
  shared/       reloj, errores, configuración y logging
  cli/          consola del cliente, del operador y del supervisor
  bootstrap.py  composición: el único lugar que conecta adaptadores con puertos
evaluation/     scripts de evaluación y resultados guardados
design/adr/     decisiones de arquitectura
```

Las dependencias entre paquetes no son una convención: las verifica `make arch`
(import-linter) en cada corrida de CI. Por ejemplo, el agente no puede importar las
implementaciones de las capacidades, y el dominio no puede tocar I/O ni proveedores.

## Matriz de cumplimiento de la consigna

Una fila por requisito. Estados: ⬜ pendiente · 🟨 en curso · ✅ hecho.

### Reglas generales

| Requisito | Estado | Dónde se garantiza | Evidencia |
|---|---|---|---|
| Python 3.11 o superior | ✅ | `requires-python` en `pyproject.toml` | CI corre 3.11 y 3.12 |
| LLM a elección | 🟨 | Adaptador de la API de Chat Completions de OpenAI (`agent/adapters/openai_chat_model.py`), válido para cualquier endpoint compatible mediante perfiles de capacidades; modelo por defecto `gpt-4o-mini` y perfil `groq` para desarrollo sin costo ([ADR-0005](design/adr/0005-adaptador-llm-y-consola.md)); nunca se corrió contra la API real | `tests/unit/agent/test_openai_chat_model.py` (cliente falso con la forma del SDK); `tests/integration/agent/test_chat_model_network.py` existe pero está deseleccionado y no se corrió |
| Declarar en el README el uso de asistentes de IA | ✅ | Sección final de este archivo | — |
| Un único repositorio y un único sistema | ✅ | Este repositorio | — |

### Parte 1: buscador RAG desde cero

| Requisito | Estado | Dónde se garantiza | Evidencia |
|---|---|---|---|
| Prohibido LangChain, LlamaIndex, bases vectoriales, scikit-learn, rank-bm25 y similares | ✅ | Contrato de import-linter sobre todo el paquete; el dominio de `knowledge/` solo usa stdlib y numpy | `make arch` |
| Chunking con criterio y tamaño justificados con evidencia | 🟨 | Tres estrategias en `knowledge/chunking.py`; grilla de 9 variantes y regla de selección declarada antes de las tablas en `evaluation/retrieval/pipeline.py` y `selection.py`; falta la corrida real con embeddings | `tests/unit/knowledge/test_chunking.py`, `tests/unit/evaluation/test_selection.py` |
| Búsqueda vectorial: embeddings y coseno a mano con numpy | 🟨 | Coseno e índice denso con numpy en `knowledge/vector.py`; cliente de OpenAI en `knowledge/adapters/openai_embedder.py` y `VectorRetriever` en `knowledge/retrievers.py`; falta probarlo contra la API real | `tests/unit/knowledge/test_vector.py` (valores a mano), `tests/unit/knowledge/adapters/test_openai_embedder.py` (cliente falso) |
| Embeddings cacheados en disco: una segunda corrida no paga la API | 🟨 | Caché direccionada por modelo y texto (`knowledge/embedding.py`) en SQLite (`knowledge/adapters/sqlite_cache.py`); decisión en [ADR-0002](design/adr/0002-embeddings-y-cache.md); falta generar y versionar `data/cache/embeddings.sqlite` | `tests/unit/knowledge/test_embedding.py`, `tests/integration/knowledge/test_sqlite_cache.py`, `tests/integration/evaluation/test_retrieval_cli.py` (0 llamadas con la caché completa) |
| BM25 a mano: tokenización, normalización (tildes, mayúsculas) e IDF | ✅ | `knowledge/bm25.py` (IDF de Lucene) y `knowledge/text.py` (normalización y tokenizador) | `tests/unit/knowledge/test_bm25.py` (puntajes calculados a mano), `test_text.py` |
| Búsqueda híbrida con RRF | ✅ | Fusión RRF en `knowledge/fusion.py`; `HybridRetriever` en `knowledge/retrievers.py` fusiona los rankings de chunks de vector y BM25 | `tests/unit/knowledge/test_fusion.py` (valores a mano), `tests/unit/knowledge/test_retrievers.py` |
| Uso de la metadata (vigencia y tipo): decisión medida | 🟨 | Período de la pregunta y solapamiento de vigencias en `knowledge/temporal.py`; modos `filter` y `demote` y la ablación por `tipo` en `knowledge/retrievers.py`; falta la medición real | `tests/unit/knowledge/test_temporal.py`, `tests/unit/knowledge/test_retrievers.py` |
| recall@3 y MRR sobre las 20 preguntas para las 4 estrategias | 🟨 | Métricas en `knowledge/metrics.py`; script en `evaluation/retrieval/` con intervalos bootstrap; falta la corrida real con embeddings | `tests/unit/knowledge/test_metrics.py`, `tests/unit/evaluation/` |
| Análisis por tipo de pregunta, con ejemplos; fallos clasificados como error de código o límite de la técnica | ⬜ | — | — |
| Tests unitarios de coseno, BM25 y RRF con valores calculados a mano | ✅ | La derivación aritmética está en el comentario junto a cada valor esperado | `test_vector.py`, `test_bm25.py`, `test_fusion.py` en `tests/unit/knowledge/` |
| Tabla de resultados de retrieval | 🟨 | El script escribe `evaluation/results/retrieval/metrics.json` y `report.md`; falta la corrida real | `tests/unit/evaluation/test_report.py`, `tests/integration/evaluation/test_retrieval_cli.py` (dos corridas, archivos idénticos) |

### Parte 2: agente con herramientas y políticas

| Requisito | Estado | Dónde se garantiza | Evidencia |
|---|---|---|---|
| Mantiene el contexto de una conversación de varios mensajes | 🟨 | El historial completo (mensajes, llamadas y resultados de herramientas) vive en `ConversationState` (`agent/domain/state.py`) y se le pasa al modelo en cada turno (`agent/application/conversation.py`); falta el modelo real | `tests/integration/agent/test_agent_policies.py` (`test_12_a_date_the_assistant_proposed_and_the_user_accepted_passes`, dos turnos) |
| Tool calling nativo del proveedor, sin parsear texto libre | 🟨 | El turno solo actúa sobre llamadas estructuradas (`ModelReply.tool_calls`); un texto con forma de llamada no se ejecuta ni se entrega (`agent/domain/signals.py`, `looks_like_tool_call`); el adaptador (`agent/adapters/openai_chat_model.py`) manda las herramientas con `tool_choice=auto` y `parallel_tool_calls=false` cuando el perfil lo admite y lee solo `tool_calls` estructurados; 🟨 porque nunca se corrió contra la API real | `tests/integration/agent/test_agent_policies.py` (`test_14_a_tool_call_written_as_text_is_not_delivered`), `tests/unit/agent/test_openai_chat_model.py` (forma del pedido y de la respuesta por perfil) |
| Cita el ID del documento (por ejemplo [DOC-07]) | 🟨 | El prompt lo pide; el turno revisa toda mención `DOC-NN` (entre corchetes, entre paréntesis o suelta) contra los documentos que una búsqueda devolvió en la conversación: en un grupo borra los desconocidos y una suelta retiene la respuesta (`agent/domain/output_checks.py`); que el modelo real cite se mide en la Parte 4 | `tests/integration/agent/test_agent_policies.py` (`test_14_a_citation_of_a_document_never_retrieved_is_removed_and_recorded`), `tests/integration/agent/test_agent_control_scenarios.py` (`test_7_every_document_mention_is_checked_against_what_was_retrieved`), `tests/unit/agent/test_agent_output_checks.py` |
| Aplicación de consola | ✅ | `cli/` (`make chat`): vistas de cliente, operador y supervisor sobre una conversación, con el actor derivado del rol; la vista de cliente solo imprime respuestas, avisos y mensajes del operador, y retiene antes del modelo, del historial y del log todo mensaje que contenga un PIN o una clave, o que parezca un `login` mal escrito; un secreto parafraseado o con separadores no se reconoce ([ADR-0005](design/adr/0005-adaptador-llm-y-consola.md)) | `tests/integration/cli/test_console_views.py` (incluye el flujo de dos aprobaciones de un reclamo, la guarda de credenciales con su reproducción exacta y dos propiedades de Hypothesis), `test_console_start.py`, `tests/unit/cli/test_console_text.py` |
| `buscar_documentacion(consulta)` con fragmentos, ID y vigencia | ⬜ | — | — |
| `consultar_ventas(fecha)` | ⬜ | — | — |
| `crear_reclamo(motivo, descripcion)` en SQLite, con identificador | 🟨 | Almacenamiento en `complaints/adapters/sqlite_repository.py` (ids `REC-000001`, ...), modelo y validación en `complaints/models.py`; contrato en `agent/domain/tools.py`; conectada al turno por `ComplaintsAdapter` (`bootstrap.py`) y ejecutada solo por `ToolExecutor.execute_confirmed`; la consola la expone con el flujo de dos aprobaciones | `tests/integration/complaints/test_sqlite_repository.py`, `tests/integration/agent/test_agent_policies.py` |
| `ventas.json` con 30 días o más, un día sin ventas, un neto negativo y una fecha futura | ✅ | `data/ventas.json` (37 días de historia, un hueco, un neto negativo y una preventa en 2027), generado con `make dataset` por `scripts/generate_ventas.py`; lo lee el repositorio estricto `sales/adapters/json_repository.py` | `tests/integration/sales/test_dataset.py` (incluye la regeneración byte a byte), `tests/integration/sales/test_json_repository.py` |
| Fallas simuladas por variable de entorno en `consultar_ventas` y `crear_reclamo` | 🟨 | `TOOL_FAILURE_RATE`, `TOOL_FAILURE_SEED` y `TOOL_FAILURE_MODES` en `shared/config.py`; inyector determinista en `shared/faults.py` y decoradores en `sales/adapters/` y `complaints/adapters/` (incluye `lost_ack`); `build_agent_service` (`bootstrap.py`) los conecta y el ejecutor reintenta solo las fallas reintentables, con backoff y jitter inyectados (`agent/application/executor.py`); la consola las usa con las mismas variables | `tests/unit/shared/test_faults.py`, `tests/unit/agent/test_agent_executor.py`, `tests/integration/agent/test_agent_policies.py` (`test_11_*`, `test_12_a_retryable_*`, `test_12_a_permanent_*`) |
| No inventar datos cuando una herramienta no devuelve resultados | 🟨 | La búsqueda tiene un piso BM25 calibrado y responde `no_data` por debajo (`bootstrap.py`, `MIN_BM25_SCORE`); el modelo nunca puede anunciar un registro (`registration_claim`), y un monto que ningún resultado de herramienta contiene queda marcado como `unverified_amount` (`agent/domain/output_checks.py`); que el modelo real no invente se mide en la Parte 4 | `tests/integration/agent/test_agent_retrieval_floor.py`, `tests/integration/agent/test_agent_control_scenarios.py` (`test_7_*`) |
| 🔒 No registrar un reclamo sin confirmación explícita de esos parámetros exactos | ✅ | La llamada del LLM solo crea una acción pendiente atada a un fingerprint (`agent/domain/pending.py`); la compuerta corre antes que el modelo y solo un "sí" estricto del usuario en un turno posterior, bajo la misma versión de control, produce un `Confirmed` (`agent/domain/replies.py`); la pregunta muestra los valores exactos (`agent/domain/templates.py`); el único punto de entrada para el efecto es `ToolExecutor.execute_confirmed` ([ADR-0003](design/adr/0003-turno-del-agente.md)) | `tests/integration/agent/test_agent_policies.py` (escenarios 1, 2, 9, 10 y 18 con Hypothesis), `tests/unit/agent/test_agent_architecture.py`, `test_agent_pending.py`, `test_agent_pending_properties.py`, `test_agent_replies.py` |
| 🔒 Si cambian el motivo o la descripción después de confirmar, se vuelve a confirmar | ✅ | El fingerprint es sensible a mayúsculas y puntuación y una propuesta nueva reemplaza a la anterior, que ya no se puede confirmar (`agent/domain/pending.py`) | `tests/integration/agent/test_agent_policies.py` (`test_4_a_yes_with_a_change_drops_the_proposal_and_only_the_new_one_registers`), `test_agent_pending.py::test_the_fingerprint_changes_with_any_change_the_user_could_see`, `::test_c_a_changed_proposal_supersedes_and_only_the_new_one_is_confirmed` |
| 🔒 Un reclamo confirmado no se registra dos veces (repetir "sí" o reintentar tras una falla) | ✅ | Idempotencia en la base con una restricción UNIQUE (`complaints/adapters/sqlite_repository.py`); en el dominio, `mark_registered` cierra la acción pendiente y un "sí" repetido no confirma nada, mientras que una falla deja la acción pendiente para reintentar con la misma clave (`agent/domain/pending.py`); el ejecutor reintenta con los mismos parámetros (`agent/application/executor.py`) | `tests/integration/agent/test_agent_policies.py` (escenarios 3, 8, 11 y 18), `tests/integration/complaints/test_sqlite_repository.py` (duplicados, carrera entre hilos, `lost_ack`), `test_agent_pending.py::test_e_repeated_yes_after_registering_carries_the_reference` |
| 🔒 No ejecutar herramientas no disponibles ni con argumentos fuera de su contrato | ✅ | El turno ofrece al modelo y le pasa a la guarda el mismo conjunto, construido desde las capacidades del estado (`offered_tools` en `agent/application/conversation.py`). `authorize` exige un nombre exacto entre las herramientas que el estado ofrece, un JSON objeto sin claves repetidas y validación estricta, y responde con pares campo/problema sin repetir el valor (`agent/domain/guards.py`, contratos en `agent/domain/tools.py`) | `tests/integration/agent/test_agent_policies.py` (escenarios 6, 7 y 17), `tests/unit/agent/test_agent_guards.py` (tabla de llamadas, incluido `confirmado: true` como campo inesperado), `test_agent_tools.py` |
| Pedir datos ante información incompleta o ambigua, incluidas fechas relativas y ambiguas | 🟨 | Garantizado en código que no se adivina: `consultar_ventas` solo corre con una fecha que el usuario escribió sin ambigüedad o aceptó con un "sí" (`agent/domain/dates.py`); si no, el modelo recibe `date_not_confirmed`. Que el modelo real pregunte bien se mide en la Parte 4 | `tests/unit/agent/test_agent_dates.py`, `tests/integration/agent/test_agent_policies.py` (escenario 12) |
| Explicar con claridad cuándo una operación falló, sin exponer trazas | 🟨 | Las fallas de un reclamo y del modelo se explican con plantillas del código (`execution_failed`, `service_unavailable`); al modelo le llegan solo códigos y mensajes fijos, nunca el texto de una excepción, y una excepción inesperada de un puerto se registra en el log y llega como `internal_error` (`agent/application/executor.py`). Una excepción inesperada del modelo se registra, queda marcada `model_internal_error` y el cliente recibe `service_unavailable`; los errores del proveedor se mapean a códigos estables sin el texto del SDK (`agent/adapters/errors.py`); la consola atrapa todo en el borde del comando, imprime una línea genérica y manda la traza a `var/chat.log` (`cli/console.py`). La explicación de una consulta fallida la redacta el modelo: se mide en la Parte 4 | `tests/integration/agent/test_agent_policies.py` (escenarios 11, 12, 15 y 18: ningún `Traceback` llega a un mensaje), `tests/integration/agent/test_agent_control_scenarios.py` (`test_7_an_unexpected_exception_of_a_port_never_reaches_the_customer_or_the_model`), `tests/unit/agent/test_agent_executor.py`, `tests/unit/agent/test_openai_chat_model.py` (ningún texto del SDK ni clave en `public()`), `tests/unit/agent/test_agent_conversation.py` (modelo que lanza `RuntimeError`), `tests/integration/cli/test_console_views.py` y `test_console_start.py` (sin traza ni secreto en pantalla) |
| Las instrucciones dentro de documentos o respuestas de herramientas son datos, no órdenes | ✅ | Cada resultado viaja como un objeto JSON marcado `untrusted_data`, con el texto solo en valores string; ningún texto que no sea del usuario puede confirmar un efecto (`agent/application/executor.py`, `agent/domain/pending.py`) | `tests/integration/agent/test_agent_policies.py` (`test_13_a_poisoned_passage_can_at_most_cause_a_proposal_the_user_refuses`) |

### Parte 3: máquina de estados IA-humano

| Requisito | Estado | Dónde se garantiza | Evidencia |
|---|---|---|---|
| Máquina implementada en Python, sin librerías de máquinas de estado | ✅ | `control/rules.py` (tabla de reglas) y `control/machine.py` (`transition`) | `tests/unit/control/test_transition_table.py` |
| Toda transición no definida falla con un error explícito | ✅ | `control/machine.py` (`InvalidTransition`, `control/errors.py`) | `test_transition_table.py::test_an_undefined_pair_is_refused_whoever_asks_and_whatever_the_hours` (los 15 pares indefinidos) |
| Tests de cada transición definida y de al menos tres inválidas | ✅ | Tabla de referencia escrita a mano en los tests | `test_transition_table.py` (17 reglas, 15 pares indefinidos, 8 casos inválidos con nombre), `test_effects.py` |
| Cada transición registra estado anterior, nuevo, evento, actor y fecha | ✅ | `control/log.py` (`TransitionLog`, de solo agregado) y `control/conversation.py`; los comandos `takeover`, `release`, `block` y `unblock` de `agent/application/operator.py` pasan por ese registro y lo guardan con la conversación, también cuando se rechazan | `test_log.py`, `test_conversation.py`, `test_conversation_properties.py`, `tests/integration/agent/test_agent_control_scenarios.py` (`test_9_every_command_leaves_a_record_with_the_five_fields`) |
| En HUMAN el agente no envía mensajes ni ejecuta acciones | ✅ | `control/capabilities.py` define las capacidades y el turno guarda el mensaje sin llamar al modelo ni enviar nada (`agent/application/conversation.py`); responde el operador con `operator_reply`, solo en HUMAN y marcado como suyo en el historial (`agent/application/operator.py`) | `test_capabilities.py`, `tests/integration/agent/test_agent_policies.py` (`test_17_a_state_that_generates_no_reply_never_calls_the_model`), `tests/integration/agent/test_agent_control_scenarios.py` (`test_1_*`, `test_5_*`, `test_10_*`) |
| En SUGGEST el agente nunca envía sin aprobación; `crear_reclamo` requiere además la del operador | ✅ | Toda respuesta del turno es un borrador; la pregunta de un reclamo también, y la acción recién queda pendiente cuando el operador la aprueba y se entrega; el "sí" del cliente crea un borrador de aprobación y solo aprobarlo ejecuta el reclamo (`agent/application/conversation.py`, `agent/application/operator.py`, `agent/domain/drafts.py`; [ADR-0004](design/adr/0004-control-y-operadores.md)) | `test_capabilities.py`, `tests/unit/agent/test_agent_conversation.py` (`test_in_suggest_*`), `tests/integration/agent/test_agent_control_scenarios.py` (`test_2_*` y las dos propiedades de Hypothesis `test_10_*`), `tests/unit/agent/test_agent_operator.py` |
| Fuera del horario de atención solo responde la IA e informa la franja humana | ✅ | Guarda en `control/rules.py`, `control/hours.py` y `control/capabilities.py`; la franja es configuración (`HUMAN_HOURS_*` en `shared/config.py`, armada en `bootstrap.py`); el turno agrega la franja a cada respuesta fuera de horario (`hours_notice`, `human_unavailable`) y todo comando del equipo sincroniza primero el horario; si el cierre le quita la conversación a una persona (HUMAN), el operador recibe una entrada `out_of_hours_started` en su inbox | `test_transition_table.py` (guarda), `test_business_hours.py`, `tests/unit/shared/test_config.py`, `tests/integration/agent/test_agent_policies.py` (`test_17_out_of_hours_the_answer_carries_the_human_window`), `tests/integration/agent/test_agent_control_scenarios.py` (`test_4_*`) |
| Solo un supervisor puede sacar una conversación de BLOCKED | ✅ | `control/rules.py` y `control/machine.py` (garantizado a nivel de `control`); `OperatorService.unblock` y `release` pasan por esa regla | `test_transition_table.py::test_only_a_supervisor_can_leave_blocked`, `test_conversation_properties.py::test_a_conversation_only_leaves_blocked_through_a_supervisor`, `tests/integration/agent/test_agent_control_scenarios.py` (`test_3_only_a_supervisor_brings_a_blocked_conversation_back`, `test_10_*`) |
| El estado decide qué herramientas hay y si la respuesta se envía, queda como borrador o no se genera | ✅ | `offered_tools` y la entrega según `Capabilities.delivery` en `agent/application/conversation.py`; las capacidades se recalculan cuando el propio turno pasa a SUGGEST por `low_confidence`, que sale de un piso BM25 calibrado (`bootstrap.py`, `MIN_BM25_SCORE`), nunca de la confianza del modelo | `tests/unit/agent/test_agent_conversation.py` (herramientas ofrecidas, borrador en SUGGEST, `low_confidence`), `tests/integration/agent/test_agent_policies.py` (escenario 17), `tests/integration/agent/test_agent_retrieval_floor.py` |
| Diagrama de estados | ✅ | `control/diagram.py`, generado con `make diagram` en `design/state-diagram.md` | `test_diagram.py` (sin deriva respecto del código) |
| `SPEC_REVIEW.md`: contradicciones, ambigüedades y casos faltantes, con la resolución implementada | ✅ | [`SPEC_REVIEW.md`](SPEC_REVIEW.md): 16 hallazgos de la Parte 3 y 10 de la Parte 2, todos implementados; queda pendiente solo el umbral del lado vectorial de F9, que necesita embeddings reales | Los tests citados en cada hallazgo |

### Parte 4: evaluación del sistema

| Requisito | Estado | Dónde se garantiza | Evidencia |
|---|---|---|---|
| Tabla de retrieval de la Parte 1 | ⬜ | — | — |
| 15 conversaciones de varios turnos o más: exitosas, ambiguas, con herramientas que fallan, con intentos de violar políticas (incluida la regla 7) | ⬜ | — | — |
| Al menos tres conversaciones que cruzan estados de la máquina | 🟨 | Escritas y probadas con un modelo guionado y comandos reales: takeover con un reclamo a medio confirmar, SUGGEST completo desde una pregunta fuera de tema, BLOCKED y el supervisor, cierre del horario (con el aviso al operador), HUMAN y la carrera (`tests/integration/agent/test_agent_control_scenarios.py`); faltan las corridas repetidas con el modelo real | `tests/integration/agent/test_agent_control_scenarios.py` |
| Cada conversación corrida 3 veces o más, con tasa de cumplimiento por caso | ⬜ | — | — |
| Cumplimiento decidido con aserciones sobre hechos observables | ⬜ | — | — |
| Si hay un LLM como juez, explicación de cómo se validó | ⬜ | — | — |
| Por caso: mensajes, comportamiento esperado, herramientas a usar y a no usar, resultado y tasa | ⬜ | — | — |

### Entregables

| Requisito | Estado | Dónde se garantiza | Evidencia |
|---|---|---|---|
| Repositorio Git con worktrees e historial de commits que muestre el avance | 🟨 | Git | `git log` |
| README con instrucciones de punta a punta, un comando por paso | 🟨 | Este archivo | — |
| `.env.example` sin credenciales reales | ⬜ | — | — |
| `ventas.json` y la base de reclamos inicial | ✅ | `data/ventas.json` (`make dataset`) y `data/reclamos.db`, vacía y con el esquema del adaptador (`make db`); la base de ejecución es `var/reclamos.db`, fuera de git | `tests/integration/sales/test_dataset.py`, `tests/integration/complaints/test_initial_database.py` |
| Tests unitarios: coseno, BM25, RRF, máquina de estados y guardas 🔒 | ✅ | `tests/unit/` (más los escenarios de integración en `tests/integration/`) | `make test` |
| Script de evaluación y sus resultados guardados | ⬜ | — | — |
| `SPEC_REVIEW.md` | 🟨 | [`SPEC_REVIEW.md`](SPEC_REVIEW.md) | — |
| Diagrama de estados | ✅ | [`design/state-diagram.md`](design/state-diagram.md) | `test_diagram.py` |
| README: decisiones técnicas, limitaciones conocidas, posibles mejoras | ⬜ | — | — |

## Decisiones técnicas

Pendiente. Las decisiones se registran como ADR en [`design/adr/`](design/adr/) a medida que se toman.

## Limitaciones conocidas

Pendiente.

## Posibles mejoras

Pendiente.

## Uso declarado de asistentes de IA

- **Claude Code** (Anthropic), con los modelos Opus 5.5 y Sonnet 5.5: se usó para revisar
  proyectos anteriores del autor y extraer prácticas y errores a no repetir, para proponer
  la arquitectura, y para escribir código, tests y documentación bajo la dirección del autor.
- Las decisiones de diseño y el alcance las aprobó el autor antes de implementarlas.
- El resultado se verifica con tests y con los chequeos de `make check`, no por
  confianza en la herramienta.
