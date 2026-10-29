# Zoco Cortex - Kit tecnico v2.0

Este kit acompana la segunda prueba revisada por IT. No contiene solucion ni implementacion del harness.

## Contenido
- `harness_config.json`: limites y politicas iniciales.
- `tools_config.json`: esquema comun de herramientas.
- `fixtures/tenants_ventas.json`: dos empresas ficticias, con el mismo nombre de contacto.
- `fixtures/chat_events.json`: eventos de takeover y seguimiento.
- `fixtures/faults.json`: fallas transitorias, permanentes y escritura incierta.
- `mock_responses.json`: respuestas ficticias.
- `scenarios.json` y `examples/`: 13 casos publicos de referencia.
- `CONTRATO_EVALUACION.md`: contrato del adaptador de evaluacion.

La fuente de verdad para los requisitos es el PDF de la consigna. Debes reutilizar tu proyecto de la primera instancia. No hace falta volver a producir el RAG.

## Igualdad de condiciones
Los escenarios de referencia definen comportamientos observables, no una arquitectura interna. Se permite adaptar los fixtures a tu codigo respetando la semantica. Se requiere un modo con modelo simulado sin claves ni internet.

## Ejemplo
`python evaluation_adapter.py --scenario examples/T02.json --output resultado.json`

No se enviaran datos reales de Zoco.
