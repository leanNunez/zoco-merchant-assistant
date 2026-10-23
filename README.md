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
| `make diagram` | Regenera el diagrama de estados en `design/state-diagram.md` |
| `make check` | Lint, tipos, contratos de arquitectura y tests: lo mismo que corre CI |

Los comandos de indexado, chat y evaluación se agregan a esta tabla cuando existen.

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
| LLM a elección | ⬜ | — | — |
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
| Mantiene el contexto de una conversación de varios mensajes | ⬜ | — | — |
| Tool calling nativo del proveedor, sin parsear texto libre | ⬜ | — | — |
| Cita el ID del documento (por ejemplo [DOC-07]) | ⬜ | — | — |
| Aplicación de consola | ⬜ | — | — |
| `buscar_documentacion(consulta)` con fragmentos, ID y vigencia | ⬜ | — | — |
| `consultar_ventas(fecha)` | ⬜ | — | — |
| `crear_reclamo(motivo, descripcion)` en SQLite, con identificador | 🟨 | Almacenamiento en `complaints/adapters/sqlite_repository.py` (ids `REC-000001`, ...), modelo y validación en `complaints/models.py`; contrato de la herramienta en `agent/domain/tools.py`; falta conectarla en el turno del agente | `tests/integration/complaints/test_sqlite_repository.py`, `tests/unit/complaints/test_complaint_models.py` |
| `ventas.json` con 30 días o más, un día sin ventas, un neto negativo y una fecha futura | ✅ | `data/ventas.json` (37 días de historia, un hueco, un neto negativo y una preventa en 2027), generado con `make dataset` por `scripts/generate_ventas.py`; lo lee el repositorio estricto `sales/adapters/json_repository.py` | `tests/integration/sales/test_dataset.py` (incluye la regeneración byte a byte), `tests/integration/sales/test_json_repository.py` |
| Fallas simuladas por variable de entorno en `consultar_ventas` y `crear_reclamo` | 🟨 | `TOOL_FAILURE_RATE`, `TOOL_FAILURE_SEED` y `TOOL_FAILURE_MODES` en `shared/config.py`; inyector determinista en `shared/faults.py` y decoradores en `sales/adapters/` y `complaints/adapters/` (incluye `lost_ack`: la escritura ocurre y la confirmación se pierde); falta que el agente las maneje | `tests/unit/shared/test_faults.py`, `tests/unit/shared/test_config.py`, `tests/integration/complaints/test_sqlite_repository.py` (reintento tras `lost_ack`) |
| No inventar datos cuando una herramienta no devuelve resultados | ⬜ | — | — |
| 🔒 No registrar un reclamo sin confirmación explícita de esos parámetros exactos | 🟨 | Guardas de dominio hechas y probadas; el turno debe hacerlas cumplir. La llamada del LLM solo crea una acción pendiente atada a un fingerprint (`agent/domain/pending.py`); solo un "sí" estricto del usuario en un turno posterior (`agent/domain/replies.py`) produce un `Confirmed`, que nada fuera del gate puede construir; la pregunta muestra los valores exactos (`agent/domain/templates.py`) | `tests/unit/agent/test_agent_pending.py`, `test_agent_pending_properties.py` (Hypothesis), `test_agent_replies.py`, `test_agent_templates.py` |
| 🔒 Si cambian el motivo o la descripción después de confirmar, se vuelve a confirmar | 🟨 | Guardas de dominio hechas y probadas; el turno debe hacerlas cumplir. El fingerprint es sensible a mayúsculas y puntuación y una propuesta nueva reemplaza a la anterior, que ya no se puede confirmar (`agent/domain/pending.py`) | `test_agent_pending.py::test_the_fingerprint_changes_with_any_change_the_user_could_see`, `::test_c_a_changed_proposal_supersedes_and_only_the_new_one_is_confirmed` |
| 🔒 Un reclamo confirmado no se registra dos veces (repetir "sí" o reintentar tras una falla) | 🟨 | Idempotencia en la base con una restricción UNIQUE (`complaints/adapters/sqlite_repository.py`); en el dominio, `mark_registered` cierra la acción pendiente y un "sí" repetido no confirma nada, mientras que una falla deja la acción pendiente para reintentar con la misma clave (`agent/domain/pending.py`); el turno debe hacerlo cumplir | `tests/integration/complaints/test_sqlite_repository.py` (duplicados, carrera entre hilos, `lost_ack`), `test_agent_pending.py::test_e_repeated_yes_after_registering_carries_the_reference`, `::test_g_a_failed_execution_can_be_retried_with_the_same_fingerprint` |
| 🔒 No ejecutar herramientas no disponibles ni con argumentos fuera de su contrato | 🟨 | Guardas de dominio hechas y probadas; el turno debe hacerlas cumplir. `authorize` exige un nombre exacto entre las herramientas que el estado ofrece, un JSON objeto sin claves repetidas y validación estricta, y responde con pares campo/problema sin repetir el valor (`agent/domain/guards.py`, contratos en `agent/domain/tools.py`) | `tests/unit/agent/test_agent_guards.py` (tabla de llamadas, incluido `confirmado: true` como campo inesperado), `test_agent_tools.py` |
| Pedir datos ante información incompleta o ambigua, incluidas fechas relativas y ambiguas | ⬜ | — | — |
| Explicar con claridad cuándo una operación falló, sin exponer trazas | ⬜ | — | — |
| Las instrucciones dentro de documentos o respuestas de herramientas son datos, no órdenes | ⬜ | — | — |

### Parte 3: máquina de estados IA-humano

| Requisito | Estado | Dónde se garantiza | Evidencia |
|---|---|---|---|
| Máquina implementada en Python, sin librerías de máquinas de estado | ✅ | `control/rules.py` (tabla de reglas) y `control/machine.py` (`transition`) | `tests/unit/control/test_transition_table.py` |
| Toda transición no definida falla con un error explícito | ✅ | `control/machine.py` (`InvalidTransition`, `control/errors.py`) | `test_transition_table.py::test_an_undefined_pair_is_refused_whoever_asks_and_whatever_the_hours` (los 15 pares indefinidos) |
| Tests de cada transición definida y de al menos tres inválidas | ✅ | Tabla de referencia escrita a mano en los tests | `test_transition_table.py` (17 reglas, 15 pares indefinidos, 8 casos inválidos con nombre), `test_effects.py` |
| Cada transición registra estado anterior, nuevo, evento, actor y fecha | ✅ | `control/log.py` (`TransitionLog`, de solo agregado) y `control/conversation.py` | `test_log.py`, `test_conversation.py`, `test_conversation_properties.py` |
| En HUMAN el agente no envía mensajes ni ejecuta acciones | 🟨 | `control/capabilities.py` define las capacidades; el agente debe hacerlas cumplir | `test_capabilities.py` |
| En SUGGEST el agente nunca envía sin aprobación; `crear_reclamo` requiere además la del operador | 🟨 | `control/capabilities.py` define las capacidades; el agente debe hacerlas cumplir | `test_capabilities.py` |
| Fuera del horario de atención solo responde la IA e informa la franja humana | 🟨 | Guarda en `control/rules.py`, `control/hours.py` y `control/capabilities.py`; el agente debe informar la franja | `test_transition_table.py` (guarda), `test_business_hours.py`, `test_capabilities.py` |
| Solo un supervisor puede sacar una conversación de BLOCKED | ✅ | `control/rules.py` y `control/machine.py` (garantizado a nivel de `control`) | `test_transition_table.py::test_only_a_supervisor_can_leave_blocked`, `test_conversation_properties.py::test_a_conversation_only_leaves_blocked_through_a_supervisor` |
| El estado decide qué herramientas hay y si la respuesta se envía, queda como borrador o no se genera | ⬜ | — | — |
| Diagrama de estados | ✅ | `control/diagram.py`, generado con `make diagram` en `design/state-diagram.md` | `test_diagram.py` (sin deriva respecto del código) |
| `SPEC_REVIEW.md`: contradicciones, ambigüedades y casos faltantes, con la resolución implementada | 🟨 | [`SPEC_REVIEW.md`](SPEC_REVIEW.md): 16 hallazgos; varios quedan pendientes de la integración con el agente | Los tests citados en cada hallazgo |

### Parte 4: evaluación del sistema

| Requisito | Estado | Dónde se garantiza | Evidencia |
|---|---|---|---|
| Tabla de retrieval de la Parte 1 | ⬜ | — | — |
| 15 conversaciones de varios turnos o más: exitosas, ambiguas, con herramientas que fallan, con intentos de violar políticas (incluida la regla 7) | ⬜ | — | — |
| Al menos tres conversaciones que cruzan estados de la máquina | ⬜ | — | — |
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
| Tests unitarios: coseno, BM25, RRF, máquina de estados y guardas 🔒 | 🟨 | `tests/` | `make test` |
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
