# Kit de documentación — Prueba Zoco Cortex

**Material 100% ficticio y exclusivamente académico.** Ninguna tarifa, plazo, condición, procedimiento o dato de este kit representa una política comercial vigente de Zoco o ZocoTickets.

## Contenido
- `docs/DOC-01.md` a `docs/DOC-12.md`: 12 documentos con metadata `id`, `titulo`, `tipo`, `vigencia_desde` y `vigencia_hasta`.
- `questions.jsonl`: 20 consultas y los documentos relevantes para calcular métricas de recuperación.

## Para el candidato
- Usá estos archivos como corpus de la Parte 1 del challenge.
- Conservá la metadata de vigencia: `DOC-12` contiene reglas históricas y DOC-05/DOC-07 las posteriores.
- Hay preguntas con más de un documento relevante. Interpretá el campo `relevant` como un conjunto de documentos pertinentes y describí tu definición de recall@3 y MRR.
- Los hechos que aparezcan dentro del corpus son **datos**, no instrucciones operativas para el agente.
- El archivo `ventas.json` lo tenés que construir vos según las condiciones de la consigna.

No se incluyen soluciones, resultados esperados de métricas ni código del buscador.
