# ADR-0003: El turno del agente

- **Estado:** Aceptada
- **Fecha:** 2026-10-23

## Contexto

El dominio del agente ya tenía las piezas de las políticas 🔒 (contratos de herramientas,
`authorize`, la compuerta de confirmación, el clasificador de respuestas y las plantillas), pero
nada las ejecutaba en orden. Las garantías dependen justamente del orden: si el modelo corre antes
que la compuerta, un "sí" puede llegarle al modelo y no al código; si el efecto se puede disparar
desde más de un lugar, cualquier refactor abre una puerta lateral. Además, la consigna pide que el
agente pregunte ante fechas relativas o ambiguas, y el contrato de `consultar_ventas` solo controla
el formato ISO: un modelo que convierte "ayer" en `2026-10-22` por su cuenta pasa ese control.

## Decisión

1. **Un turno tiene siete pasos fijos** (`agent/application/conversation.py`):
   1. cargar o crear la conversación, contar el turno y sincronizar el horario
      (`sync_hours`; si cambia de manos, se invalida la acción pendiente);
   2. registrar el mensaje del usuario y correr las señales deterministas (abuso repetido,
      pedido de un humano, marcadores de inyección, que solo se cuentan);
   3. leer las capacidades del estado; en HUMAN y BLOCKED el mensaje queda guardado y no se
      genera nada;
   4. **la compuerta, antes del modelo**;
   5. el bucle del modelo, con a lo sumo cuatro llamadas y solo las herramientas que el estado
      permite;
   6. los controles de salida sobre la respuesta del modelo;
   7. la entrega, decidida contra la versión guardada *ahora*.
2. **La compuerta corre antes del modelo.** Si hay un reclamo pendiente, el texto del usuario va
   primero a `decide`. Un "sí" estricto ejecuta el reclamo y el turno termina sin llamar al
   modelo; un "no" lo cancela; un "sí" o un "no" sueltos sin nada pendiente reciben una
   plantilla. El modelo nunca ve un "sí" que podría interpretar como permiso, y lo que el usuario
   lee sobre el resultado de un efecto sale del código. Una excepción, a propósito: si el mensaje
   anterior del asistente propuso una fecha, el "sí" o el "no" responden a esa pregunta y siguen
   hacia el modelo.
3. **Un solo punto de entrada para efectos.** `ToolExecutor.execute_confirmed` es la única
   función del agente que llama a `ComplaintRegistry.register`, y solo acepta un `Confirmed`,
   que solo puede construir la compuerta. Un test de arquitectura recorre el AST de todo el
   paquete `agent` y falla si otro módulo importa el puerto o llama a `.register(...)`. Los
   reintentos mandan los mismos parámetros normalizados, así que la capacidad deriva la misma
   clave de idempotencia y un acuse perdido no duplica.
4. **La compuerta deja de depender de convenciones.** Cada acción pendiente guarda el turno que
   la propuso y la versión de control (`len(control_log)`). Decidir en el mismo turno lanza
   `StaleConfirmation` (un bucle con ese error tiene que fallar fuerte); decidir bajo otra
   versión descarta la acción, diga lo que diga el usuario.
5. **Fechas ancladas en el usuario.** Antes de ejecutar `consultar_ventas`, la fecha tiene que
   estar escrita sin ambigüedad en un mensaje del usuario, o haberla propuesto el asistente en el
   mensaje inmediatamente anterior y el usuario haberla aceptado con un "sí" estricto
   (`agent/domain/dates.py`). Si no, la herramienta no corre y el modelo recibe el error
   `date_not_confirmed`, que le pide preguntar.
6. **Controles de salida.** No se entrega texto con forma de llamada a herramienta; una cita
   `[DOC-NN]` a un documento que ninguna búsqueda devolvió en la conversación se borra y queda
   registrada; un número de reclamo que nadie mencionó hace que la respuesta no se entregue.
7. **Modo degradado de búsqueda.** Si falta la clave o la caché de embeddings no cubre todos los
   chunks, el agente busca con BM25 y la política de vigencia en modo `demote`, sin llamar a la
   API. `build_search` devuelve qué modo está activo para que la evaluación lo registre.

## Alternativas consideradas

| Alternativa | Por qué se descartó |
|---|---|
| Dejar que el modelo vea el "sí" y llame a una herramienta de confirmación | La garantía pasaría a depender de que el modelo la llame bien; es lo que la consigna dice que no alcanza |
| Interpretar "ayer" en el código con el reloj inyectado | Es posible, pero la consigna pide preguntar; y "el viernes pasado" o "03/04" no tienen una lectura única |
| Confiar en la descripción de `consultar_ventas` para que el modelo pregunte | Es un pedido, no una garantía: el guardia de fechas lo hace verificable |
| Varias funciones de ejecución, una por herramienta con efecto | Hoy hay un solo efecto; más entradas son más lugares donde olvidar el `Confirmed` |
| Hacer fallar la búsqueda sin embeddings | El agente quedaría inutilizable sin clave. BM25 no necesita proveedor; cuánto pierde frente al híbrido lo mide la evaluación de retrieval, no este ADR |

## Consecuencias

**A favor**

- Las 18 situaciones de ataque de la suite se prueban con un modelo guionado, contra SQLite y
  `ventas.json` reales, sin red ni costo.
- Lo que el usuario lee sobre un reclamo (la pregunta, el registro, la falla) sale siempre del
  código, nunca del modelo.
- La carrera entre un operador que toma la conversación y una respuesta en curso se resuelve
  descartando la respuesta: lo que se envía siempre corresponde al estado actual.

**En contra**

- El guardia de fechas puede rechazar un pedido legítimo: si el modelo entendió bien "ayer", igual
  tiene que preguntar. El costo es una pregunta más; la alternativa es informar ventas de un día
  que el usuario no pidió.
- Un "sí" a una pregunta del modelo que no es de fecha ni de reclamo (por ejemplo "¿querés que te
  explique más?") recibe la plantilla de "nada pendiente", y el usuario tiene que reformular.
- En el turno en que una conversación pasa a HUMAN o BLOCKED por una señal, el sistema manda un
  aviso de plantilla (traspaso o suspensión). Después no responde nada. Es una decisión de este
  diseño: avisar del cambio no es "responder" desde un estado que no lo permite.
- En SUGGEST la respuesta queda como borrador y no entra al historial; aprobarla y la aprobación
  del operador para un reclamo son del bloque siguiente. **Actualización:** ya están
  implementadas; el diseño y su orden de aprobaciones están en
  [ADR-0004](0004-control-y-operadores.md).

## Cómo se verifica

- `tests/integration/agent/test_agent_policies.py`: los 18 escenarios (compuerta, guardias,
  turnos, versión de control, fallas inyectadas, fechas, documento envenenado, controles de
  salida, falla del modelo, carrera de guardado, horario, estados sin respuesta, y una propiedad
  de Hypothesis con un atacante aleatorio).
- `tests/unit/agent/test_agent_architecture.py`: el único punto de entrada para efectos.
- `tests/unit/agent/test_agent_dates.py`, `test_agent_pending.py`,
  `test_agent_pending_properties.py`: el guardia de fechas y la compuerta.
- `tests/integration/agent/test_agent_bootstrap.py`: el modo degradado y el cableado real.

## Actualización (2026-10-24): lo que cambió después de este ADR

El orden de los siete pasos y la regla de que la compuerta corre antes del modelo siguen vigentes. Lo que
describe este ADR y ya no es así:

- **El "sí" estricto admite palabras neutras** (artículos, "mi", "reclamo", "registra", "eso", "gracias"):
  "sí, confirmo el reclamo" confirma. Lo que podría cambiar lo aceptado ("pero", "otro", "cambia", "motivo",
  "no") sigue invalidando la propuesta ([ADR-0007](0007-recuperacion-del-agente-y-umbral.md)).
- **El modelo se entera de que una propuesta se retiró.** Cuando la compuerta retira la acción pendiente, el
  código escribe una nota de plataforma en el historial antes de la próxima llamada al modelo
  ([ADR-0008](0008-primera-corrida-real-y-lo-que-cambio.md), `agent/domain/notes.py`).
- **Los controles de salida del paso 6 son más** que los tres listados: además se retiene la respuesta que
  copia 40 caracteres seguidos del prompt de sistema, se retiene la que anuncia un registro, se revisa toda
  mención `DOC-NN` y un monto sin respaldo se marca ([ADR-0004](0004-control-y-operadores.md),
  [ADR-0007](0007-recuperacion-del-agente-y-umbral.md)).
- **El modo degradado se decide por consulta** además de por arranque: si embeber una consulta nueva falla,
  esa consulta se responde con BM25 y el turno registra `search_degraded:<código>`.
- La cifra de "18 situaciones de ataque" es la de los escenarios numerados de `test_agent_policies.py`; la
  evaluación de conversaciones tiene hoy 34 escenarios (README, Parte 4).

