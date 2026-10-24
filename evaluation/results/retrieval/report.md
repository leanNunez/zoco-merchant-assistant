# Evaluación de retrieval

Generado por `make eval-retrieval` (`uv run python -m evaluation.retrieval run`). No editar a mano: se regenera igual en cada corrida.

## Procedencia

- Modelo de embeddings: `text-embedding-3-small`
- Fecha de referencia ("hoy"): 2026-10-23, fija para que el resultado no dependa del día en que se corre
- sha256 del corpus (`docs/*.md` y `questions.jsonl`): `e10416a93c50adb4a30383a9cee36ea9da3aa098f5eb40c91c5a665cd07f16d3`
- Llamadas a la API de embeddings durante esta corrida: 0
- Chunks por estrategia de chunking: `document` 12, `section` 49, `section+header` 49, `window-32-o0` 50, `window-32-o5` 58, `window-64-o0` 26, `window-64-o10` 30, `window-128-o0` 14, `window-128-o19` 14, `document+header` 12

## Reglas declaradas antes de ver las tablas

El chunking de la tabla principal es el de mayor MRR híbrido; ante un empate, el de mayor recall@3 híbrido; luego el de menos chunks; luego el tipo más simple (documento < sección < ventana) y, por último, el orden de la grilla (sin encabezado antes que con encabezado, ventanas chicas antes que grandes). Para elegir entre dos estrategias (el mejor modo de metadata, la mejor estrategia simple) se usa el mayor MRR, luego el mayor recall@3 y luego un orden fijo declarado de antemano. No se ajusta nada más sobre estas 20 preguntas.

Orden fijo para el modo de metadata: `hybrid+meta(demote)` > `hybrid+meta(filter)`. Para la mejor estrategia simple: `bm25` > `vector`.

## Definiciones

- La unidad del ranking es el documento: cada documento cuenta una vez, en la posición de su mejor chunk. A cada estrategia se le piden todos los chunks, así el MRR ve la lista completa.
- recall@3 = documentos relevantes en el top 3 / documentos relevantes. Con dos relevantes y uno encontrado vale 0.5.
- hit@3 = 1 si algún documento relevante está en el top 3.
- MRR = promedio de 1 / posición del primer documento relevante (0 si no aparece).
- Intervalos: bootstrap percentil al 95 %, 10000 remuestreos de las preguntas con `numpy.random.default_rng(0)`; todas las estrategias se remuestrean con los mismos sorteos.
- Hay 20 preguntas: **una sola pregunta mueve un promedio 5 puntos** (0.05 en recall@3, hit@3 o MRR). Una diferencia de ese tamaño no alcanza para preferir una estrategia.

## Grilla de chunking

BM25 sin stemming. Híbrido = RRF (k = 60) sobre los rankings de chunks de vector y BM25.

| Chunking | Chunks | Estrategia | recall@3 | hit@3 | MRR |
|---|---:|---|---:|---:|---:|
| `document` | 12 | vector | 0.975 | 1.000 | 0.900 |
| `document` | 12 | bm25 | 0.975 | 1.000 | 0.917 |
| `document` | 12 | hybrid | 0.975 | 1.000 | 0.975 |
| `section` | 49 | vector | 1.000 | 1.000 | 0.867 |
| `section` | 49 | bm25 | 1.000 | 1.000 | 0.925 |
| `section` | 49 | hybrid | 1.000 | 1.000 | 0.900 |
| `section+header` | 49 | vector | 0.975 | 1.000 | 0.925 |
| `section+header` | 49 | bm25 | 0.975 | 1.000 | 0.925 |
| `section+header` | 49 | hybrid | 0.975 | 1.000 | 0.950 |
| `window-32-o0` | 50 | vector | 1.000 | 1.000 | 0.892 |
| `window-32-o0` | 50 | bm25 | 1.000 | 1.000 | 0.892 |
| `window-32-o0` | 50 | hybrid | 1.000 | 1.000 | 0.875 |
| `window-32-o5` | 58 | vector | 1.000 | 1.000 | 0.842 |
| `window-32-o5` | 58 | bm25 | 0.875 | 0.900 | 0.847 |
| `window-32-o5` | 58 | hybrid | 1.000 | 1.000 | 0.900 |
| `window-64-o0` | 26 | vector | 0.975 | 1.000 | 0.925 |
| `window-64-o0` | 26 | bm25 | 0.975 | 1.000 | 0.917 |
| `window-64-o0` | 26 | hybrid | 1.000 | 1.000 | 0.950 |
| `window-64-o10` | 30 | vector | 0.975 | 1.000 | 0.908 |
| `window-64-o10` | 30 | bm25 | 0.975 | 1.000 | 0.917 |
| `window-64-o10` | 30 | hybrid | 0.975 | 1.000 | 0.925 |
| `window-128-o0` | 14 | vector | 0.975 | 1.000 | 0.925 |
| `window-128-o0` | 14 | bm25 | 0.975 | 1.000 | 0.917 |
| `window-128-o0` | 14 | hybrid | 0.975 | 1.000 | 0.975 |
| `window-128-o19` | 14 | vector | 0.975 | 1.000 | 0.925 |
| `window-128-o19` | 14 | bm25 | 0.975 | 1.000 | 0.917 |
| `window-128-o19` | 14 | hybrid | 0.975 | 1.000 | 0.975 |

## Chunking seleccionado

**`document`**. `document` frente a `window-128-o0` (el segundo): empatan en MRR híbrido (0.975) y en recall@3 híbrido (0.975); gana por tener menos chunks (12 contra 14).

## Tabla principal (`document`)

| Estrategia | recall@3 | IC 95 % | hit@3 | MRR | IC 95 % |
|---|---:|---|---:|---:|---|
| vector | 0.975 | [0.925, 1.000] | 1.000 | 0.900 | [0.800, 0.975] |
| bm25 | 0.975 | [0.925, 1.000] | 1.000 | 0.917 | [0.825, 1.000] |
| hybrid | 0.975 | [0.925, 1.000] | 1.000 | 0.975 | [0.925, 1.000] |
| hybrid+meta(filter) | 0.975 | [0.925, 1.000] | 1.000 | 1.000 | [1.000, 1.000] |
| hybrid+meta(demote) | 0.975 | [0.925, 1.000] | 1.000 | 1.000 | [1.000, 1.000] |

## Diferencias pareadas de MRR

Diferencia por pregunta y bootstrap de su promedio. Un intervalo que contiene el 0 no muestra una diferencia.

| Comparación | Diferencia de MRR | IC 95 % |
|---|---:|---|
| mejor modo de metadata menos híbrido: hybrid+meta(demote) - hybrid | +0.025 | [0.000, 0.075] |
| híbrido menos la mejor estrategia simple: hybrid - bm25 | +0.058 | [0.000, 0.150] |

## Ablaciones

Informativas: **nunca cambian la selección declarada** (el chunking y los modos de la tabla principal se eligieron antes de mirarlas).

- `bm25(stem)`: BM25 con el stemming de plurales, sobre `document`.
- La misma tabla con el encabezado (título, tipo y vigencia) cambiado: `document+header`.
- `hybrid+kind-demotion(historico)`: baja los documentos de tipo `historico` sin mirar fechas, para ver si el tipo aporta algo que la vigencia no aporte.
- `bm25(no parts)`: BM25 que emite `zt-pro-2` sin sus partes (`zt`, `pro`, `2`).
- `bm25(stopwords)` y `bm25(stopwords, stem)`: BM25 sin una lista conservadora de palabras funcionales del español (artículos, preposiciones, conjunciones, pronombres, formas de ser, estar, haber e ir, interrogativos), sin y con stemming. Nunca quita `no`, `sin`, `mas`, `solo`, números ni palabras que puedan cambiar una política.
- `hybrid(stopwords)` y `hybrid+meta(demote)(stopwords)`: el mismo híbrido con el BM25 sin palabras funcionales (el lado vectorial no cambia).
- `hybrid(fuse by document)` y `hybrid+meta(demote)(fuse by document)`: RRF sobre documentos (cada uno en la posición de su mejor chunk) en lugar de sobre chunks. Con el chunking `document` hay un chunk por documento y la fusión no puede cambiar nada por construcción; por eso, si el chunking elegido no es `section`, se corre también sobre `section`.

| Chunking | Estrategia | recall@3 | hit@3 | MRR |
|---|---|---:|---:|---:|
| `document` | bm25(stem) | 0.950 | 0.950 | 0.912 |
| `document+header` | vector | 0.975 | 1.000 | 0.950 |
| `document+header` | bm25 | 0.975 | 1.000 | 0.942 |
| `document+header` | hybrid | 0.975 | 1.000 | 1.000 |
| `document` | hybrid+kind-demotion(historico) | 0.875 | 0.900 | 0.908 |
| `document` | bm25(no parts) | 0.975 | 1.000 | 0.917 |
| `document` | bm25(stopwords) | 1.000 | 1.000 | 0.917 |
| `document` | bm25(stopwords, stem) | 0.950 | 0.950 | 0.912 |
| `document` | hybrid(stopwords) | 0.975 | 1.000 | 0.975 |
| `document` | hybrid+meta(demote)(stopwords) | 0.975 | 1.000 | 1.000 |
| `document` | hybrid(fuse by document) | 0.975 | 1.000 | 0.975 |
| `document` | hybrid+meta(demote)(fuse by document) | 0.975 | 1.000 | 1.000 |
| `section` | hybrid(fuse by document) | 1.000 | 1.000 | 0.925 |

### Qué se movió

Cada variante contra la estrategia que modifica. Una pregunta **mejora** si su par (rango recíproco, recall@3) sube y **empeora** si baja.

| Variante | Contra | Δ recall@3 | Δ hit@3 | Δ MRR | Mejoran | Empeoran |
|---|---|---:|---:|---:|---|---|
| `document` bm25(stem) | `document` bm25 | -0.025 | -0.050 | -0.004 | Q01 | Q15 |
| `document+header` vector | `document` vector | +0.000 | +0.000 | +0.050 | Q06, Q19 | — |
| `document+header` bm25 | `document` bm25 | +0.000 | +0.000 | +0.025 | Q13 | — |
| `document+header` hybrid | `document` hybrid | +0.000 | +0.000 | +0.025 | Q17 | — |
| `document` hybrid+kind-demotion(historico) | `document` hybrid | -0.100 | -0.100 | -0.067 | — | Q17, Q18 |
| `document` bm25(no parts) | `document` bm25 | +0.000 | +0.000 | +0.000 | — | — |
| `document` bm25(stopwords) | `document` bm25 | +0.025 | +0.000 | +0.000 | Q01 | — |
| `document` bm25(stopwords, stem) | `document` bm25 | -0.025 | -0.050 | -0.004 | Q01 | Q15 |
| `document` hybrid(stopwords) | `document` hybrid | +0.000 | +0.000 | +0.000 | — | — |
| `document` hybrid+meta(demote)(stopwords) | `document` hybrid+meta(demote) | +0.000 | +0.000 | +0.000 | — | — |
| `document` hybrid(fuse by document) | `document` hybrid | +0.000 | +0.000 | +0.000 | — | — |
| `document` hybrid+meta(demote)(fuse by document) | `document` hybrid+meta(demote) | +0.000 | +0.000 | +0.000 | — | — |
| `section` hybrid(fuse by document) | `section` hybrid | +0.000 | +0.000 | +0.025 | Q03 | — |

- No mueven ninguna de las 20 preguntas: `document` bm25(no parts), `document` hybrid(stopwords), `document` hybrid+meta(demote)(stopwords), `document` hybrid(fuse by document), `document` hybrid+meta(demote)(fuse by document).
- Mueven exactamente una pregunta: `document+header` bm25, `document+header` hybrid, `document` bm25(stopwords), `section` hybrid(fuse by document). Una pregunta es 5 puntos de cualquier promedio: **una diferencia de una pregunta no es evidencia**, es lo que cambia un promedio por azar de redacción.
- Mueven dos o más preguntas: `document` bm25(stem), `document+header` vector, `document` hybrid+kind-demotion(historico), `document` bm25(stopwords, stem). Con 20 preguntas y los intervalos de arriba, ni siquiera eso alcanza para cambiar lo ya elegido.

## Sensibilidad al desempate de RRF

Informativa: **no cambia la selección declarada**. En RRF dos documentos pueden sumar exactamente el mismo puntaje: si A es primero en vector y segundo en BM25, y B al revés, los dos suman 1/61 + 1/62. `knowledge/fusion.py` desempata por mejor rango y luego por id ascendente: una convención arbitraria, no evidencia. Acá se recalcula la fusión sobre `document` con un RRF propio de `evaluation/retrieval/tiebreak.py` (el de producción no se toca):

- **por defecto**: la convención de producción; se verifica que reproduce las filas de la tabla principal.
- **invertido**: mejor rango y luego id descendente.
- **esperado**: promedio exacto sobre todos los órdenes posibles de cada grupo de empate exacto, ordenado al azar con igual probabilidad. Se calcula enumerando las permutaciones de cada grupo (son de dos documentos): no hay sorteo y el resultado es idéntico en cada corrida.
- **peor / mejor orden posible**: el MRR si en cada pregunta el empate cayera siempre en el peor (o el mejor) orden.

| Estrategia | Desempate | recall@3 | hit@3 | MRR |
|---|---|---:|---:|---:|
| hybrid | por defecto | 0.975 | 1.000 | 0.975 |
| hybrid | invertido | 0.975 | 1.000 | 0.925 |
| hybrid | esperado | 0.975 | 1.000 | 0.950 |
| hybrid | peor orden posible | — | — | 0.900 |
| hybrid | mejor orden posible | — | — | 1.000 |
| hybrid+meta(demote) | por defecto | 0.975 | 1.000 | 1.000 |
| hybrid+meta(demote) | invertido | 0.975 | 1.000 | 0.975 |
| hybrid+meta(demote) | esperado | 0.975 | 1.000 | 0.988 |
| hybrid+meta(demote) | peor orden posible | — | — | 0.975 |
| hybrid+meta(demote) | mejor orden posible | — | — | 1.000 |

- Preguntas con un empate exacto entre dos documentos (9 de 20): Q03, Q06, Q08, Q09, Q11, Q12, Q17, Q19, Q20.
- `hybrid`: el orden de los dos primeros documentos depende del desempate en Q03, Q06, Q08, Q09, Q17, Q19, Q20; el rango recíproco o el recall@3 cambian en Q03, Q06, Q17, Q19.
- `hybrid+meta(demote)`: el orden de los dos primeros documentos depende del desempate en Q03, Q09, Q17, Q20; el rango recíproco o el recall@3 cambian en Q03.

Lectura:

- **La ventaja del híbrido sobre BM25 es en parte suerte del desempate.** Con la convención de producción el MRR del híbrido es 0.975 contra 0.917 de BM25 (+0.058); con el desempate invertido es 0.925 (+0.008) y el valor esperado es 0.950 (+0.033). El rango de las tres variantes es [0.925, 0.975] y el de todos los órdenes posibles, [0.900, 1.000]. En el peor orden posible el híbrido queda por debajo de BM25.
- Efecto de la política de vigencia sobre el MRR del híbrido: +0.025 con el desempate de producción, +0.050 invertido y +0.038 esperado.
- Q06, Q17, Q19 dependían del desempate en el híbrido y con `hybrid+meta(demote)` ya no: la política ordena por vigencia antes de mirar el empate, así que su efecto en esas preguntas es una propiedad de la política y no del desempate.
- `hybrid+meta(demote)` sigue dependiendo del desempate en Q03: su MRR va de 0.975 a 1.000 según el orden (esperado 0.988), porque la política no resuelve ese empate.
- Con estas diferencias de una o dos preguntas no se puede declarar un ganador entre vector, BM25 e híbrido.

## Por pregunta

Sobre `document`. Cada celda: posición del primer documento relevante (— si no aparece) y el top 3 de documentos. **✗** marca las preguntas en las que alguna estrategia deja afuera del top 3 algún documento relevante; la última columna dice cuáles.

| | Pregunta | Relevantes | Período | vector | bm25 | hybrid | hybrid+meta(filter) | hybrid+meta(demote) | Fallan en top 3 |
|---|---|---|---|---|---|---|---|---|---|
| Q01 ✗ | ¿Qué rol puede publicar eventos una vez aprobado? | DOC-01, DOC-02 | hoy | 2 · DOC-03, DOC-02, DOC-09 | 1 · DOC-02, DOC-09, DOC-10 | 1 · DOC-02, DOC-09, DOC-03 | 1 · DOC-02, DOC-09, DOC-03 | 1 · DOC-02, DOC-09, DOC-03 | vector (parcial), bm25 (parcial), hybrid (parcial), hybrid+meta(filter) (parcial), hybrid+meta(demote) (parcial) |
| Q02 | ¿Cuánto demora la revisión de alta con documentación comple… | DOC-02 | hoy | 1 · DOC-02, DOC-08, DOC-11 | 1 · DOC-02, DOC-03, DOC-06 | 1 · DOC-02, DOC-08, DOC-12 | 1 · DOC-02, DOC-08, DOC-06 | 1 · DOC-02, DOC-08, DOC-06 | — |
| Q03 | ¿Qué pasa si los lotes superan la capacidad del recinto? | DOC-03 | hoy | 2 · DOC-04, DOC-03, DOC-06 | 1 · DOC-03, DOC-04, DOC-06 | 1 · DOC-03, DOC-04, DOC-06 | 1 · DOC-03, DOC-04, DOC-06 | 1 · DOC-03, DOC-04, DOC-06 | — |
| Q04 | ¿Cuántos minutos permanece reservado un carrito sin pago co… | DOC-04 | hoy | 1 · DOC-04, DOC-08, DOC-06 | 1 · DOC-04, DOC-11, DOC-05 | 1 · DOC-04, DOC-08, DOC-06 | 1 · DOC-04, DOC-08, DOC-06 | 1 · DOC-04, DOC-08, DOC-06 | — |
| Q05 | ¿Cuál es el cargo por una entrada de $10.000 en ZT-PRO-2 du… | DOC-05 | 2026-10-01 a 2026-10-31 | 1 · DOC-05, DOC-12, DOC-07 | 1 · DOC-05, DOC-12, DOC-07 | 1 · DOC-05, DOC-12, DOC-07 | 1 · DOC-05, DOC-07, DOC-01 | 1 · DOC-05, DOC-07, DOC-01 | — |
| Q06 | ¿En cuántos días hábiles se liquida normalmente un evento f… | DOC-06 | 2026-10-01 a 2026-10-31 | 2 · DOC-12, DOC-06, DOC-08 | 1 · DOC-06, DOC-12, DOC-08 | 1 · DOC-06, DOC-12, DOC-08 | 1 · DOC-06, DOC-08, DOC-03 | 1 · DOC-06, DOC-08, DOC-03 | — |
| Q07 | ¿Cuál es el mínimo de entradas por mes del plan ZT-PRO-2? | DOC-07 | hoy | 1 · DOC-07, DOC-12, DOC-05 | 1 · DOC-07, DOC-12, DOC-05 | 1 · DOC-07, DOC-12, DOC-05 | 1 · DOC-07, DOC-05, DOC-01 | 1 · DOC-07, DOC-05, DOC-01 | — |
| Q08 | ¿Cuentan las entradas gratuitas para el mínimo mensual del… | DOC-07 | hoy | 1 · DOC-07, DOC-05, DOC-12 | 1 · DOC-07, DOC-12, DOC-05 | 1 · DOC-07, DOC-05, DOC-12 | 1 · DOC-07, DOC-05, DOC-01 | 1 · DOC-07, DOC-05, DOC-01 | — |
| Q09 | ¿Se devuelve el cargo de servicio si el organizador cancela… | DOC-08 | hoy | 1 · DOC-08, DOC-06, DOC-03 | 1 · DOC-08, DOC-05, DOC-07 | 1 · DOC-08, DOC-05, DOC-06 | 1 · DOC-08, DOC-05, DOC-06 | 1 · DOC-08, DOC-05, DOC-06 | — |
| Q10 | ¿Hasta cuándo puede pedirse devolución por reprogramación? | DOC-08 | hoy | 1 · DOC-08, DOC-11, DOC-02 | 1 · DOC-08, DOC-12, DOC-02 | 1 · DOC-08, DOC-02, DOC-12 | 1 · DOC-08, DOC-02, DOC-09 | 1 · DOC-08, DOC-02, DOC-09 | — |
| Q11 | ¿Cuántas veces puede transferirse una entrada? | DOC-09 | hoy | 1 · DOC-09, DOC-04, DOC-03 | 1 · DOC-09, DOC-04, DOC-08 | 1 · DOC-09, DOC-04, DOC-03 | 1 · DOC-09, DOC-04, DOC-03 | 1 · DOC-09, DOC-04, DOC-03 | — |
| Q12 | ¿Qué pasa con el QR original después de una transferencia? | DOC-09 | hoy | 1 · DOC-09, DOC-10, DOC-04 | 1 · DOC-09, DOC-12, DOC-10 | 1 · DOC-09, DOC-10, DOC-12 | 1 · DOC-09, DOC-10, DOC-04 | 1 · DOC-09, DOC-10, DOC-04 | — |
| Q13 | ¿Qué estados devuelve la validación de un QR? | DOC-10 | hoy | 1 · DOC-10, DOC-02, DOC-09 | 2 · DOC-11, DOC-10, DOC-02 | 1 · DOC-10, DOC-02, DOC-11 | 1 · DOC-10, DOC-02, DOC-11 | 1 · DOC-10, DOC-02, DOC-11 | — |
| Q14 | ¿Cuál es el riesgo de escanear offline en dos dispositivos? | DOC-10 | hoy | 1 · DOC-10, DOC-09, DOC-02 | 1 · DOC-10, DOC-01, DOC-08 | 1 · DOC-10, DOC-01, DOC-02 | 1 · DOC-10, DOC-01, DOC-02 | 1 · DOC-10, DOC-01, DOC-02 | — |
| Q15 | ¿Puede abrirse un reclamo sin que el usuario confirme sus d… | DOC-11 | hoy | 1 · DOC-11, DOC-02, DOC-08 | 3 · DOC-06, DOC-02, DOC-11 | 1 · DOC-11, DOC-02, DOC-06 | 1 · DOC-11, DOC-02, DOC-06 | 1 · DOC-11, DOC-02, DOC-06 | — |
| Q16 | ¿Qué significa que crear_reclamo devuelva REC-000123? | DOC-11 | hoy | 1 · DOC-11, DOC-02, DOC-06 | 1 · DOC-11, DOC-04, DOC-09 | 1 · DOC-11, DOC-04, DOC-06 | 1 · DOC-11, DOC-04, DOC-06 | 1 · DOC-11, DOC-04, DOC-06 | — |
| Q17 | ¿Cuál era el mínimo mensual de ZT-PRO-2 en julio de 2026? | DOC-12 | 2026-07-01 a 2026-07-31 | 1 · DOC-12, DOC-07, DOC-05 | 2 · DOC-07, DOC-12, DOC-05 | 2 · DOC-07, DOC-12, DOC-05 | 1 · DOC-12, DOC-01, DOC-10 | 1 · DOC-12, DOC-01, DOC-10 | — |
| Q18 | ¿Cuánto cobraba ZT-START-1 en agosto de 2026? | DOC-12 | 2026-08-01 a 2026-08-31 | 1 · DOC-12, DOC-05, DOC-07 | 1 · DOC-12, DOC-05, DOC-07 | 1 · DOC-12, DOC-05, DOC-07 | 1 · DOC-12, DOC-08, DOC-01 | 1 · DOC-12, DOC-08, DOC-01 | — |
| Q19 | ¿Qué documento contiene las tarifas aplicables en octubre d… | DOC-05 | 2026-10-01 a 2026-10-31 | 2 · DOC-12, DOC-05, DOC-07 | 1 · DOC-05, DOC-12, DOC-07 | 1 · DOC-05, DOC-12, DOC-07 | 1 · DOC-05, DOC-07, DOC-01 | 1 · DOC-05, DOC-07, DOC-01 | — |
| Q20 | ¿Puede un organizador en estado EN_REVISION publicar un eve… | DOC-02, DOC-03 | hoy | 1 · DOC-03, DOC-02, DOC-08 | 1 · DOC-02, DOC-03, DOC-08 | 1 · DOC-02, DOC-03, DOC-08 | 1 · DOC-02, DOC-03, DOC-08 | 1 · DOC-02, DOC-03, DOC-08 | — |
