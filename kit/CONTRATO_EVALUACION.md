# Contrato comun de evaluacion v2.0

Este documento define una interfaz de pruebas, no una solucion.

Comando: `python evaluation_adapter.py --scenario examples/T01.json --output resultado.json`

Entrada: un escenario JSON, con `tenant_id` y `principal_id` **como fixtures confiables del evaluador** (no como texto enviado por el usuario al agente). `model_script` representa un proveedor LLM simulado deterministico. `preconditions` inicia el estado de prueba. `incoming_event` es un evento externo simulado.

Salida minima JSON:
```json
{
 "run_id":"run-test", "tenant_id":"comercio_a", "conversation_id":"conv_t01",
 "finish_reason":"COMPLETED", "step_count":1,
 "events":[{"type":"final_answer","timestamp":"2026-10-28T12:00:00Z"}],
 "effects":{"claims_created":0,"customer_messages_sent":0},
 "state":{"conversation_mode":"AUTO"}
}
```

Resultados permitidos: `COMPLETED`, `BLOCKED`, `NEEDS_HUMAN`, `LIMIT_REACHED`, `FAILED`. La salida puede incluir campos adicionales. En un escenario de fallo o autorizacion denegada se debe incluir evento `tool_rejected` o `tool_error`, con razon y sin secretos.

Un rechazo NO debe ejecutar el adaptador. Una escritura repetida con el mismo `operation_id` debe producir un unico efecto, incluso si el proceso se reinicia. Un timeout despues de potencial escritura exige conciliacion o consulta del estado; no se reintenta a ciegas. La entrega de una respuesta de IA requiere verificar nuevamente el estado/version de control antes de enviarla.

Para eventos `followup_due`, registrar una decision de `CONTACTAR`, `TAREA_INTERNA`, `REPROGRAMAR` o `CANCELAR`, y el motivo. No construir un scheduler.

Todos los escenarios son publicos; IT puede ejecutar nuevas variantes para verificar reglas ya divulgadas. El adaptador no debe devolver respuestas precalculadas por ID de escenario.

Los tests han de verificar estado y efectos reales; la mera impresion de PASS no basta. Si un escenario requiere varios pasos o reinicio, el adaptador puede orquestarlos.
