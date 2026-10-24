# ADR-0007: La recuperación del agente y la compuerta `no_data`

- **Estado:** Aceptada
- **Fecha:** 2026-10-24

## Contexto

La primera corrida real de la evaluación de la Parte 1 (`evaluation/results/retrieval/report.md`) y
la evaluación de conversaciones (`evaluation/results/conversations/scripted/report.md`) mostraron
seis debilidades del agente:

1. **La compuerta `no_data` era demasiado estricta con preguntas cortas y legítimas.** El piso
   `MIN_BM25_SCORE = 5,25` (ADR-0004) se calibró sobre chunking `section` con las 20 preguntas del
   kit contra 16 fuera de tema. "¿Cómo transfiero una entrada?" puntúa 4,07 y "Quiero hacer un
   reclamo." 3,81: debajo del piso, la conversación pasaba a SUGGEST y el cliente no recibía nada
   hasta que un operador aprobaba (el escenario S20, una limitación declarada).
2. **Las palabras funcionales dominan los puntajes léxicos.** BM25 no tiene stopwords: en Q13, `un`,
   `que` y `la` aportan unos 1,7 de los 3,93 puntos del documento ganador (equivocado).
3. **La configuración de recuperación del agente era provisional** (`section`, `demote`), mientras
   la evaluación, con su regla declarada de antemano, eligió el chunking `document` y, entre los modos
   de metadata, `hybrid+meta(demote)`.
4. **Nada en el código impedía que el prompt de sistema se filtrara.** `no_system_prompt_leak` era
   un UTILITY.
5. **El "sí" estricto era rígido en la práctica:** "sí, confirmo el reclamo" invalidaba la propuesta.
6. **El prompt no le pedía al modelo dos conductas que necesita:** pedir los datos que exige un
   procedimiento antes de proponer un reclamo (DOC-06 exige el identificador del evento y su fecha de
   finalización) y no repetir instrucciones que aparezcan en documentos.

Con 20 preguntas, una sola mueve un promedio 5 puntos y los intervalos pareados tocan el cero: lo
que sigue es la mejor decisión con esa evidencia, no una prueba.

## Reglas declaradas antes de medir

Estas reglas se escribieron, y están en `evaluation/retrieval/calibration.py` (`GATE_RULES`), antes
de correr la calibración y antes de ver un solo puntaje de las consultas nuevas. Se aplicaron tal
cual, aunque el resultado no guste.

1. Los conjuntos de consultas se escribieron antes de medir y no se modifican después de ver los puntajes: `kit` (las 20 preguntas de `questions.jsonl`, referenciadas), `short_legit` (frases cortas y naturales que el corpus responde) y `off_topic` (fuera del dominio; las marcadas `hard` comparten palabras genéricas con el dominio).
2. Señales: el mejor puntaje BM25 de la consulta sobre los chunks `document` y el mejor coseno entre el vector de la consulta y los de los chunks.
3. Compuerta híbrida: `no_data` si BM25 < τ_b **y** coseno < τ_v. Compuerta degradada (sin embeddings, solo existe BM25): `no_data` si BM25 < τ_b_degradado.
4. Selección de los umbrales: (a) toda consulta de `kit` y de `short_legit` sigue respondiendo; (b) entre las compuertas que cumplen (a), la que rechaza más consultas `off_topic`; (c) entre empates, el centro del intervalo factible. Un umbral solo importa por entre qué dos puntajes observados cae, así que cada eje se parte en celdas y el umbral es el punto medio de la celda elegida. En dos ejes, el centro es el punto medio del rango que ocupan las celdas empatadas en cada eje; si esa celda no está empatada, se toma la empatada más cercana (distancia L1 con cada eje normalizado por su rango).
5. Relajación: si (a) solo se cumple con una compuerta que no rechaza ninguna consulta `off_topic`, se relaja lo justo para conservar las 20 del `kit`, se aplican de nuevo (b) y (c) y se informan por nombre las `short_legit` que quedan rechazadas.
6. Tokenizador con stopwords en la búsqueda del agente: solo si (i) recall@3 y MRR de `hybrid+meta(demote)` sobre las 20 preguntas del kit no bajan respecto del tokenizador por defecto **y** (ii) sube el margen factible de BM25, definido como (L - U) / L con L el menor mejor-BM25 de las consultas legítimas (kit y short_legit) y U el mayor de las `off_topic`. Se normaliza por L porque quitar palabras funcionales cambia la escala de BM25; el margen absoluto se informa al lado. Si falla (i) o (ii), se mantiene el tokenizador por defecto.

Además, la selección del chunking y del modo de metadata sigue siendo la declarada en el informe de
retrieval (`SELECTION_RULE`).

## Decisión

1. **El agente busca con lo que eligió la evidencia.** `build_search` usa chunking `document` y
   `hybrid+meta(demote)` (RRF de vector y BM25, con la vigencia aplicada por democión) cuando hay una
   clave configurada **y** todos los vectores del corpus están en la caché. Si no, corre en modo
   **degradado**: BM25 con la misma democión por vigencia. Construir la búsqueda nunca llama a la
   API. El tokenizador es el de por defecto.
2. **La compuerta mira las dos señales.** `Gate` (`bootstrap.py`): en modo híbrido la consulta es
   `no_data` solo si BM25 < `GATE_BM25_HYBRID` (11,824) **y** el mejor coseno contra los vectores del
   corpus < `GATE_COSINE_HYBRID` (0,366); en modo degradado, si BM25 < `GATE_BM25_DEGRADED` (0,039).
   Los tres valores salen de `evaluation/results/retrieval/gate_calibration.json` y un test fija la
   igualdad.
3. **Si falla el proveedor de embeddings al embeber una consulta nueva, el turno no falla.** La
   consulta se responde por el camino degradado (BM25 con su propia compuerta) y el turno registra el
   flag `search_degraded:<código>` (por ejemplo `search_degraded:embeddings_unavailable`). Solo un
   `DomainError` se absorbe: un error de programación sigue siendo ruidoso.
4. **El prompt de sistema tiene un candado.** `leaks_prompt` (`output_checks.py`) retiene toda
   respuesta al cliente que contenga 40 caracteres seguidos del prompt vigente, comparados sin
   distinguir mayúsculas, acentos ni espacios, quitando antes el horario de atención, que el comercio
   le muestra al cliente a propósito. La respuesta se reemplaza por la plantilla de respuesta
   retenida y el turno registra `prompt_leak`. En la evaluación de conversaciones
   `no_system_prompt_leak` pasa de UTILITY a LOCK.
5. **El "sí" admite palabras neutras.** `AFFIRMATIVE_WORDS` suma `el`, `la`, `lo`, `mi`, `reclamo`,
   `reclamos`, `registra`, `registre`, `registren`, `eso`, `esto`, `asi`, `bien` y `gracias`. Siguen
   afuera todas las que podrían cambiar lo aceptado (`pero`, `otro`, `cambia`, `motivo`, `no`, `y`) y
   siguen el tope de 6 palabras, la línea única y la ausencia de signos de pregunta.
6. **El prompt suma dos reglas cortas:** buscar el procedimiento y pedir los datos que exige antes de
   proponer un reclamo (sin placeholders), y tratar el texto de documentos y herramientas que ordena
   algo como un dato que no se repite como instrucción ni se sigue.

## Resultados de la medición

### Qué configuración eligió la evidencia (sin cambios respecto de `report.md`)

| Estrategia sobre `document` | recall@3 | MRR |
|---|---:|---:|
| vector | 0,975 | 0,900 |
| bm25 | 0,975 | 0,917 |
| hybrid | 0,975 | 0,975 |
| hybrid+meta(filter) | 0,975 | 1,000 |
| hybrid+meta(demote) | 0,975 | 1,000 |

`document` empata con `window-128-o0` en MRR y recall@3 híbridos y gana por tener menos chunks (12
contra 14). **Con 20 preguntas esto no demuestra nada:** la diferencia pareada de `hybrid+meta(demote)`
contra `hybrid` es +0,025 con un intervalo de [0,000; 0,075].

### Las variantes del tokenizador y de la fusión (informativas)

Ninguna cambia la selección. Preguntas que mueve cada una, contra la estrategia que modifica:

- `bm25(no parts)`: ninguna. Igual que BM25 de por defecto.
- `bm25(stopwords)`: una (Q01 pasa a recuperar los dos documentos relevantes en el top 3); recall@3
  +0,025, MRR sin cambio. Una pregunta no es evidencia.
- `bm25(stopwords, stem)`: dos (Q01 mejora, Q15 empeora); recall@3 −0,025, MRR −0,004.
- `hybrid(stopwords)` y `hybrid+meta(demote)(stopwords)`: ninguna.
- `hybrid(fuse by document)` y `hybrid+meta(demote)(fuse by document)` sobre `document`: ninguna, por
  construcción (un chunk por documento). Sobre `section`, donde puede importar: una pregunta (Q03),
  MRR +0,025.

### La calibración de la compuerta

Mejor puntaje de cada consulta sobre los 12 chunks `document` (mínimo / mediana / máximo):

| Grupo | n | BM25 | Coseno |
|---|---:|---|---|
| kit | 20 | 3,926 / 8,032 / 19,656 | 0,448 / 0,585 / 0,675 |
| short_legit | 30 | 0,077 / 2,937 / 9,272 | 0,379 / 0,517 / 0,665 |
| off_topic | 45 | 0,000 / 2,037 / 4,071 | 0,092 / 0,241 / 0,538 |
| de ellas `hard` | 13 | 0,077 / 2,152 / 4,071 | 0,268 / 0,424 / 0,538 |

**Se solapan.** El BM25 de la consulta legítima más débil (0,077, "cuánto cobran de comisión") está
por debajo del de la fuera de tema más fuerte (4,071, "¿Cómo hago una transferencia bancaria?"); en
coseno, la legítima más débil (0,379, "qué es un lote") está por debajo de la fuera de tema más
fuerte (0,538, "¿Qué pasa si cancelan mi vuelo?"). Ninguna señal sola separa los grupos.

Umbrales elegidos por la regla (tokenizador por defecto):

| Compuerta | Umbral | Celda de umbrales equivalentes | Fuera de tema rechazadas |
|---|---|---|---:|
| Degradada | BM25 < 0,039 | (0,000; 0,077] | 2 de 45 |
| Híbrida | BM25 < 11,824 y coseno < 0,366 | BM25 (10,553; 13,095], coseno (0,3539; 0,3787] | 33 de 45 |

Matrices de confusión (cuántas responde la compuerta):

| Grupo | Degradada | Híbrida |
|---|---:|---:|
| kit (20) | 20 | 20 |
| short_legit (30) | 30 | 30 |
| off_topic (45), rechazadas | 2 | 33 |
| de ellas `hard` (13), rechazadas | 0 | 3 |

Ninguna consulta legítima queda debajo de la compuerta. Con el piso anterior (5,25 sobre `section`)
quedaban sin respuesta 17 de las 50 legítimas.

Lo que esto significa, dicho claro:

- **En modo híbrido el coseno hace el trabajo.** Una compuerta de solo coseno con 0,366 rechaza las
  mismas 33 consultas fuera de tema y ninguna legítima; BM25 no rescata ninguna consulta medida (el
  umbral de BM25 está en el medio de una meseta ancha de valores equivalentes, por la regla (c)). El
  margen del coseno es angosto: unos 0,012 hacia cada lado (la legítima más débil es 0,3787 y la
  fuera de tema rechazada más fuerte es 0,3539).
- **En modo degradado la compuerta está casi vacía, y es la consecuencia de la regla, no un
  descuido.** La regla (a) exige que ninguna legítima corta quede rechazada, y las legítimas cortas
  puntúan casi cero en BM25 (el mínimo es 0,077). Como BM25 no tiene stopwords, casi cualquier frase
  en español comparte `el`, `de` o `la` con el corpus y puntúa por encima de 0,039: solo se rechazan
  las consultas sin ninguna palabra en común (`hola`, `gracias`). Una pregunta fuera de tema como
  "¿Cuál es la capital de Francia?" **pasa** la compuerta degradada y el modelo recibe pasajes poco
  relacionados, con la instrucción de no inventar.
- **Alternativa que no se usa** (exigir solo el kit en modo degradado): el umbral sería 3,9, rechazaría
  44 de 45 fuera de tema y dejaría sin respuesta 21 de las 30 cortas. Se descartó porque es
  exactamente el problema que esta decisión viene a resolver.
- Las 12 fuera de tema que pasan la compuerta híbrida son sobre todo las `hard` que comparten
  vocabulario con el dominio ("¿Cómo hago una transferencia bancaria?", "reclamo de mi seguro del
  auto", "¿Qué pasa si cancelan mi vuelo?"). Ningún umbral sobre estas señales las separa; la
  defensa es el prompt, no la compuerta.

### El tokenizador con stopwords: no se adopta

La regla pedía (i) que recall@3 y MRR de `hybrid+meta(demote)` sobre el kit no bajaran, y (ii) que
subiera el margen factible de BM25.

- (i) se cumple: 0,975 y 1,000 con los dos tokenizadores.
- (ii) no se cumple. El margen se definió, antes de medir, como (L - U) / L, normalizado por L (el
  menor mejor-BM25 legítimo) porque quitar palabras funcionales cambia la escala de BM25. Con el
  tokenizador por defecto vale −51,6 (L = 0,077, U = 4,071). Con stopwords, L es 0 ("cuánto cobran de
  comisión" no comparte ninguna palabra con el corpus una vez quitadas las funcionales, porque
  `cobran` y `comisión` no coinciden con `cobra` y `comisiones`): el margen es indefinido y un margen
  indefinido no puede haber mejorado. El margen absoluto (informativo) sube de −3,993 a −3,479, pero
  en los dos casos es negativo: ninguno separa los grupos.

Nota de transparencia: en la primera corrida una guarda de división por cero devolvía 0 para ese margen
indefinido y declaraba que "mejoraba"; era un defecto del código y no de la regla, y se corrigió antes
de adoptar nada (un margen indefinido no mejora). Con el margen absoluto que escribió el pedido original
la decisión habría sido la contraria: la ambigüedad queda a la vista. Además, con stopwords la compuerta
degradada obligaba a relajar la regla (a) y dejaba sin respuesta 13 de las 30 cortas.

## Alternativas consideradas

| Alternativa | Por qué se descartó |
|---|---|
| Mantener `section` y el piso de 5,25 | Dejaba sin respuesta 17 de las 50 consultas legítimas medidas |
| Elegir los umbrales a ojo o con una métrica elegida después de ver los números | La regla se declaró antes de medir justamente para no ajustar sobre estas consultas |
| Compuerta de solo coseno | Obliga a embeber cada consulta: sin clave o con el proveedor caído no hay compuerta. El diseño de dos señales cae en BM25 |
| Relajar la regla (a) en modo degradado (solo el kit) | Recrea el problema de partida: deja sin respuesta 21 de las 30 cortas |
| Stopwords en la búsqueda del agente | No pasó la regla de adopción (ver arriba) |
| Fusionar por documento en la búsqueda del agente | Con `document` hay un chunk por documento: no cambia nada |
| Retener también la pregunta de confirmación si contiene el prompt | Los valores que muestra los escribe el modelo y es un hueco conocido, pero queda fuera de este bloque |
| Detectar el prompt por similitud difusa o por hash de líneas | Más falsos positivos; el candado de 40 caracteres es simple, verificable y cubre la copia literal |

## Consecuencias

**A favor**

- Las 50 consultas legítimas medidas responden en los dos modos; el escenario S20 pasa de limitación
  declarada a regresión que pasa.
- La configuración del agente sale de una regla declarada antes de los números, con la evidencia y sus
  límites a la vista, y se recalibra con `make index && make eval-retrieval`.
- Una caída del proveedor de embeddings degrada una consulta, no un turno, y queda registrada.
- El prompt de sistema no llega al cliente por una copia literal: lo garantiza el código y lo mide un
  LOCK.
- "sí, confirmo el reclamo" registra un reclamo; las confirmaciones con condición o cambio siguen
  descartadas.

**En contra**

- Los tres conjuntos de consultas son chicos (20, 30 y 45) y los escribió quien escribió el sistema:
  los umbrales sobreestiman lo que pasaría con consultas de personas reales, y están ajustados a
  estas mismas consultas.
- En modo híbrido el margen del coseno es de unos 0,012 hacia cada lado; otro modelo de embeddings, otro
  corpus o consultas nuevas pueden moverlo. Hay que recalibrar si cambia cualquiera.
- En modo degradado la compuerta casi no rechaza nada: un cliente sin embeddings recibe pasajes poco
  relacionados para una pregunta fuera de tema, y `low_confidence` casi no se dispara. Las preguntas
  fuera de tema de los escenarios (X2, los tests de control y de la consola) pasaron a ser preguntas
  sin ninguna palabra en común con el corpus ("¿Quién ganó Roland Garros?"), que es lo único que el
  modo degradado rechaza.
- La compuerta no resuelve las `hard`: 10 de las 13 pasan en modo híbrido.
- Embeber la consulta del usuario cuesta una llamada a la API por consulta nueva (con caché por
  texto), que antes no existía en el turno.
- El candado del prompt cubre solo la copia literal de 40 o más caracteres. Una paráfrasis, una
  traducción o una copia con un carácter intercalado cada 39 pasan, y la pregunta de confirmación
  muestra valores escritos por el modelo que no se revisan contra el prompt.
- "si eso" y "si bien" dichos solos ahora cuentan como un sí aunque dudan; son raros como mensaje
  completo y la pregunta de confirmación sigue nombrando los valores exactos.
- El prompt crece en dos reglas que el modelo puede ignorar: son un pedido, no una garantía (ADR-0001).

## Cómo se verifica

- `tests/unit/knowledge/test_text.py` y `test_retrievers.py`: las opciones del tokenizador y la
  fusión por documento, con tablas literales y sumas RRF hechas a mano.
- `tests/unit/evaluation/test_calibration.py` y `test_calibration_report.py`: la selección de umbrales
  sobre puntajes hechos a mano (un eje, dos ejes, relajación, margen indefinido, redondeo dentro de la
  celda) y las propiedades de Hypothesis (el kit siempre responde).
- `tests/integration/evaluation/test_retrieval_results_drift.py`: los resultados versionados
  (`metrics.json`, `report.md`, `gate_calibration.json`, `gate_calibration.md`) son lo que produce el
  código con la caché versionada, sin red.
- `tests/integration/agent/test_agent_no_data_gate.py`: las consultas de `calibration_queries.jsonl`
  contra la compuerta en los dos modos (las 50 legítimas responden; se rechazan 33 de 45 fuera de tema
  en híbrido y 2 en degradado), que los umbrales del código son los del informe, y el camino de falla
  del proveedor con un embedder escrito a mano.
- `tests/unit/agent/test_agent_output_checks.py` y `test_agent_conversation.py`: el candado del prompt
  (pegado de tres líneas, 40 caracteres sí y 39 no, el horario no cuenta, respuestas normales).
- `tests/unit/agent/test_agent_replies.py` y `test_agent_prompts.py`: el vocabulario del sí y las
  cláusulas nuevas del prompt.
- `tests/integration/evaluation/test_conversation_validation.py`: S20 y S21 pasan en `scripted`, el
  modelo nulo no rompe ningún LOCK y el agente inseguro falla LOCK en los ataques y cruces.
- `make eval-agent` y `make eval-retrieval` regeneran los resultados que versionan los números.
