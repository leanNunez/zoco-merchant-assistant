# ADR-0005: El adaptador del LLM y la consola

- **Estado:** Aceptada
- **Fecha:** 2026-10-24

## Contexto

Toda la política del agente (ADR-0001 a ADR-0004) estaba probada con un modelo guionado, pero nadie
podía conversar con el sistema: faltaba el borde real del modelo y una forma de que un cliente, un
operador y un supervisor hablen con la misma conversación. Las dos piezas son bordes: no deciden
nada de lo que el código ya garantiza, y no pueden debilitarlo.

Restricciones: el desarrollo diario no debe gastar crédito de OpenAI (se usa un endpoint
compatible gratuito, Groq); los resultados reportados salen del modelo por defecto de OpenAI; el
mensaje de un error del proveedor puede citar parte de una clave; y la prueba de confirmación de un
reclamo (`Confirmed`) vive en memoria por diseño (ADR-0003), así que los tres roles comparten un
proceso.

## Decisión

1. **Un adaptador sobre Chat Completions, con perfiles de capacidades.** `OpenAIChatModel`
   (`agent/adapters/openai_chat_model.py`) implementa `ChatModel` sobre cualquier cliente con la
   forma del SDK de OpenAI. Lo que cambia entre endpoints es un `ProviderProfile`:

   | Perfil | `strict` | `parallel_tool_calls` | Parámetro de tope de salida | `name` en mensajes |
   |---|---|---|---|---|
   | `openai` | admitido | admitido | `max_completion_tokens` | sí |
   | `groq` | no se envía | no se envía | `max_tokens` | no |
   | `generic` (conservador) | no se envía | no se envía | `max_tokens` | no |

   El perfil afirma lo que se sabe que un endpoint acepta; uno desconocido recibe el conservador en
   lugar de un pedido que quizá rechace. `tools` y `tool_choice="auto"` se envían solo cuando se
   ofrecen herramientas (y con ellas `parallel_tool_calls=false` si el perfil lo admite: cada efecto
   debe pasar solo por la compuerta). El mensaje de un operador viaja como `assistant`, con
   `name="operator"` únicamente si el perfil admite `name`.

2. **`strict` está apagado por defecto.** Con `LLM_STRICT_TOOLS=true` y un perfil que lo admite, la
   herramienta se declara `strict: true` y se envía un esquema **saneado** (sin `minLength`,
   `maxLength`, `pattern`, `title` ni `default`, que el modo estricto rechaza). El saneado solo
   afecta lo que se envía: nuestra validación (`agent/domain/tools.py`) sigue aplicando esas
   restricciones y sigue siendo la garantía. Quedó apagado porque **no se pudo verificar contra la
   API real**: un modo que quizá rechace pedidos no puede ser el predeterminado de un sistema cuyo
   resultado se reporta. Habilitarlo es una medición pendiente, no una decisión de diseño.

3. **El modelo que respondió se registra, no se impone.** `Completion.model` es el modelo que dice
   la respuesta; si no empieza con el pedido (los proveedores devuelven instantáneas con fecha, como
   `gpt-4o-mini-2024-07-18`, y eso es normal) se emite una advertencia en el log y no se falla. Un
   proveedor puede enrutar a otro modelo, y cortar la conversación del cliente por eso no lo
   arregla; lo que importa es que la evaluación (Parte 4) afirme el modelo servido sobre los hechos
   registrados en `TurnResult.served_models`.

4. **Política de errores.** Toda falla del SDK se convierte en un `DomainError` con código estable
   (`agent/adapters/errors.py`) y un mensaje fijo y seguro:

   | Falla del SDK | Error | Código | Reintentable |
   |---|---|---|---|
   | Conexión, timeout, 5xx, 408, 409, 429 | `ModelUnavailable` | `model_unavailable` | sí |
   | `AuthenticationError`, `PermissionDeniedError` | `ModelAuthError` | `model_auth` | no |
   | `NotFoundError` | `ModelNotFound` | `model_not_found` (el modelo puede haberse retirado) | no |
   | Otro `BadRequestError`, `APIError` u `OpenAIError` | `ModelRequestRejected` | `model_rejected` | no |
   | `finish_reason=length`, respuesta vacía o mal formada | `ModelIncomplete` | `model_incomplete` | no |
   | `content_filter` o `refusal` no vacío | `ModelRefused` | `model_refused` | no |

   El texto del SDK **nunca** llega a `public()`, al `detail` ni al log: el `detail` lleva solo la
   clase y el estado HTTP, porque el mensaje de un 401 puede citar parte de la clave. La excepción
   del SDK no se encadena (`from None`), así que un traceback registrado tampoco la repite. Una
   respuesta cortada por el tope de salida es `ModelIncomplete` aunque traiga texto o una llamada a
   herramienta: un argumento truncado no se ejecuta. El SDK ya reintenta (`max_retries=2` en
   `bootstrap.py`); no se agrega una segunda capa de reintentos, que multiplicaría las esperas.

5. **Una excepción inesperada del puerto del modelo no tumba el turno.** `ConversationService`
   la registra con `logger.exception`, la marca `model_internal_error` y la trata como cualquier
   `DomainError` del modelo: el cliente recibe `service_unavailable()` y no queda nada pendiente.
   `AssertionError` se relanza a propósito: un guion de prueba agotado o un invariante roto no deben
   parecer una caída del proveedor.

6. **Una consola con tres vistas separadas sobre una conversación** (`cli/`). Solo la consola
   imprime. Las vistas son de cliente (por defecto), operador y supervisor.
   - El `Actor` que recibe `OperatorService` sale **únicamente** del rol con sesión iniciada; el
     texto escrito nunca lo elige. `/unblock` no se filtra en la consola a propósito: lo rechaza la
     máquina de control (queda REJECTED en el registro) y la consola traduce el código.
   - La vista de cliente imprime solo `reply` (como `Asistente:`), `notice` (el aviso de traspaso o
     de suspensión, que forma parte de lo que el cliente recibe) y `operator` (como `Operador:`).
     Una lista de tipos permitidos, no una de prohibidos: un tipo nuevo no se muestra hasta que
     alguien lo habilite. Los borradores, la bandeja, el estado, el registro de control, las marcas y
     las infracciones no existen para esa vista, y un comando del equipo se ve igual que uno
     desconocido, sin revelar cuáles existen.
   - Lo que el equipo entrega al cliente (un borrador aprobado, un `/reply`) se encola y se
     muestra al volver a la vista de cliente: la pantalla del equipo no hace de pantalla del cliente.
   - Todo lo que se imprime pasa por una limpieza: se descartan caracteres de control y de
     dirección bidireccional y las líneas siguientes de un mensaje se sangran, para que un texto del
     modelo, del cliente o de un operador no pueda falsificar una etiqueta (`Operador:`) ni borrar la
     pantalla.
   - Todo error se atrapa en el borde del comando: una línea genérica en español en pantalla y la
     traza en `var/chat.log`, creado de solo lectura para su dueño y alcanzado por todo WARNING del
     proceso (la consola, el agente y el SDK), de modo que nada técnico llegue a la terminal.

7. **El PIN es un interruptor de rol de un simulador, no autenticación.** Se compara con
   `secrets.compare_digest`. Sin PIN configurado se generan seis dígitos al azar por rol en cada
   arranque y se muestran una vez en un cartel que dice que es un simulador; un PIN configurado no
   se imprime nunca.

## Alternativas consideradas

| Alternativa | Por qué se descartó |
|---|---|
| `strict` activado por defecto | No se pudo verificar contra la API real; un rechazo del proveedor rompería toda conversación |
| Exigir que el modelo servido sea el pedido | Las instantáneas con fecha son normales y un proveedor puede enrutar; cortar al cliente no arregla nada. Se registra y la evaluación lo afirma |
| Una capa propia de reintentos sobre la del SDK | Multiplica las esperas (el timeout se aplica a cada intento) y hace dos políticas donde alcanza una |
| Copiar el mensaje del SDK a `detail` para depurar | Un 401 puede citar parte de la clave; el detalle útil (clase y estado) alcanza |
| Tres procesos, uno por rol | La prueba de confirmación (`Confirmed`) vive en memoria por diseño; no cruzaría procesos |
| Filtrar `/unblock` en la consola para el operador | Duplicaría la regla de la máquina de control y no dejaría el rechazo en el registro de auditoría |
| Mostrar al cliente un "esperando al equipo" cuando hay un borrador | Revelaría el estado de la conversación; un canal real de chat tampoco lo muestra |
| Autenticación real (usuarios, sesiones) | Fuera de alcance de un simulador; se declara como límite en lugar de simularla |

## Consecuencias

**A favor**

- Cambiar de proveedor es elegir un perfil y una URL; el agente no se entera.
- Ningún texto de un proveedor, ninguna clave y ninguna traza llegan a un cliente, a un envelope ni a
  la terminal, y hay tests que lo afirman con claves falsas puestas en los mensajes de error.
- Una caída o un bug del adaptador degrada a una respuesta fija en lugar de cortar la consola.

**En contra**

- `strict` y el perfil `groq` no se verificaron contra las APIs reales; el adaptador nunca se corrió
  en vivo (el test `network` existe y está deseleccionado).
- **Lo que la separación de roles NO garantiza:**
  - No es autenticación. Cualquiera con el PIN entra, y los PIN generados se imprimen en la misma
    pantalla que ve el cliente. La terminal muestra lo que se escribe, y el historial de la terminal
    guarda lo que mostró la vista del equipo.
  - No hay bloqueo ni límite de intentos ante PIN incorrectos (cada intento fallido solo queda como
    advertencia en el log), ni identidad por persona: la auditoría registra el rol, no quién fue.
  - Quien escribe un PIN sin la barra (`login operator 123456`) lo manda como mensaje: llega al
    modelo y al historial. La consola solo protege el comando.
  - Ocultar el estado no es ocultar el hecho: el silencio de la vista de cliente delata que la
    conversación no está en AUTO, y el aviso de traspaso es parte de lo que el cliente recibe.
  - `var/chat.log` guarda el texto crudo de las excepciones inesperadas, que podría contener un
    secreto: se crea privado y fuera de git, pero hay que tratarlo como sensible.
  - La conversación vive en memoria y desaparece al salir.

## Cómo se verifica

- `tests/unit/agent/test_openai_chat_model.py`: forma del pedido por perfil (strict, bandera de
  paralelismo, `tool_choice`, nombre del parámetro de tope, esquema saneado), mapeo de mensajes y de
  respuestas, `finish_reason`, rechazos, advertencia por modelo distinto, y cada clase de excepción
  del SDK con una clave falsa en su texto sin rastro en `public()`, `detail` ni traceback.
- `tests/unit/agent/test_agent_conversation.py`: un modelo que lanza `RuntimeError("sk-FAKE-SECRET")`
  produce la respuesta fija, la marca `model_internal_error` y ninguna aparición del secreto.
- `tests/integration/cli/test_console_views.py`: la vista de cliente no muestra borrador, bandeja,
  estado, registro, marcas ni infracciones (con un borrador y un aviso realmente en su lugar); el
  actor sigue al rol; el operador no puede desbloquear y el supervisor sí; el flujo de dos
  aprobaciones de un reclamo llega hasta el `REC-` en la vista de cliente; una excepción inesperada
  imprime una línea y deja la traza solo en el log; y una propiedad de Hypothesis sobre sesiones
  aleatorias (sin traza, PIN, clave ni borrador en la vista de cliente).
- `tests/integration/cli/test_console_start.py`: el cartel DEMO, los PIN configurados que no se
  imprimen y el arranque sin clave (estado distinto de cero, sin traza).
- `make arch`: el adaptador puede importar `openai`; `agent.domain` no; nada importa `cli`.

## Actualización: guarda de credenciales en la vista de cliente

La limitación de arriba ("quien escribe un PIN sin la barra lo manda como mensaje") era real y se cerró
en parte. `ConsoleRuntime` lleva `protected_secrets` (los dos PIN y las claves efectivas del modelo y de los
embeddings, fuera del repr). Antes de llamar al servicio, la vista de cliente retiene una línea que contiene
alguno de esos valores (coincidencia exacta de subcadena, también sobre la forma NFKC) o que parece un login mal
escrito (`login ...`, `login:`, o `/login` precedido de otros caracteres). Lo retenido no llega al modelo, no se
guarda en el historial y no se escribe en el log; el cliente ve una línea neutra que le indica usar
`/login <rol> <pin>`. Un número de seis dígitos que no es un PIN (un número de pedido) pasa.

El límite es deliberado: **un secreto parafraseado o escrito con separadores (`482 913`, `4-8-2-9-1-3`, "cuatro
ocho dos...") no se reconoce**, porque unir dígitos sueltos bloquearía pedidos legítimos. La guarda tampoco cubre
el texto que el equipo escribe con `/reply`. Y retiene de más a quien empieza su mensaje con "login ..." para
quejarse de un acceso. La defensa de fondo sigue siendo la de siempre: los PIN son de un simulador y el
`var/chat.log` se trata como sensible.
