# Diagrama de estados IA-humano

Generado por `make diagram` a partir de la tabla de reglas de `src/merchant_assistant/control/rules.py`. No se edita a mano: un test falla si este archivo no coincide con el código.

- `[*] --> AUTO`: toda conversación empieza en AUTO.
- Cada flecha dice `evento (actores que pueden dispararlo)`.
- Una flecha que termina en `- open hours only` solo se permite dentro de la franja de atención humana; fuera de ella el evento se rechaza con el código `out_of_hours`.
- Cualquier par (estado, evento) que no figure como flecha es indefinido y falla con `invalid_transition`.

```mermaid
stateDiagram-v2
    [*] --> AUTO
    AUTO --> HUMAN: operator_takeover (OPERATOR/SUPERVISOR) - open hours only
    AUTO --> HUMAN: customer_requests_human (CUSTOMER/SYSTEM) - open hours only
    AUTO --> SUGGEST: low_confidence (SYSTEM) - open hours only
    AUTO --> BLOCKED: abuse_detected (SYSTEM)
    AUTO --> AUTO: out_of_hours_started (SYSTEM)
    SUGGEST --> HUMAN: operator_takeover (OPERATOR/SUPERVISOR) - open hours only
    SUGGEST --> HUMAN: customer_requests_human (CUSTOMER/SYSTEM) - open hours only
    SUGGEST --> AUTO: operator_release (OPERATOR/SUPERVISOR)
    SUGGEST --> BLOCKED: abuse_detected (SYSTEM)
    SUGGEST --> AUTO: out_of_hours_started (SYSTEM)
    HUMAN --> AUTO: operator_release (OPERATOR/SUPERVISOR)
    HUMAN --> HUMAN: abuse_detected (SYSTEM)
    HUMAN --> BLOCKED: operator_block (OPERATOR/SUPERVISOR)
    HUMAN --> AUTO: out_of_hours_started (SYSTEM)
    BLOCKED --> AUTO: operator_release (SUPERVISOR)
    BLOCKED --> AUTO: supervisor_unblock (SUPERVISOR)
    BLOCKED --> BLOCKED: out_of_hours_started (SYSTEM)
```

## Efectos que la aplicación debe ejecutar

Son instrucciones declarativas: el dominio las devuelve y no las ejecuta.

| Efecto | Cuándo | Qué hace |
|---|---|---|
| `invalidate_pending_action` | Todo cambio de estado | Descarta la acción pendiente (por ejemplo, un reclamo a medio confirmar). |
| `discard_unapproved_drafts` | Al salir de SUGGEST | Descarta los borradores que nadie aprobó, para que no se envíen después. |
| `notify_operator` | Abuso detectado en HUMAN | Avisa al operador, que decide si bloquea (`operator_block`). El estado no cambia. |
