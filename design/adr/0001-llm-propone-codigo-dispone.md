# ADR-0001: El LLM propone, el código dispone

- **Estado:** Aceptada
- **Fecha:** 2026-10-23

## Contexto

La consigna exige que ciertas políticas (las marcadas con 🔒) estén garantizadas en
código: si el LLM intenta violarlas, el código lo impide, y pedirlo en el prompt no
alcanza. Un prompt es una petición, no una garantía: el modelo puede ignorarlo por un
error propio o por una instrucción maliciosa escondida en un documento recuperado o en
la respuesta de una herramienta.

## Decisión

1. El LLM solo **propone** llamadas a herramientas. En cada turno se le ofrecen únicamente
   las herramientas que el estado de la conversación permite.
2. Cada llamada pasa por guardas deterministas antes de ejecutarse: que la herramienta
   esté disponible, que los argumentos cumplan el contrato (validación estricta que
   rechaza en vez de corregir) y la guarda de efectos.
3. Un efecto (registrar un reclamo) **nunca se ejecuta desde la llamada del LLM**. La
   llamada crea una acción pendiente, atada a un fingerprint de los parámetros exactos.
   Solo se ejecuta cuando el usuario confirma en un turno posterior. Si los parámetros
   cambian, el fingerprint cambia y hay que confirmar de nuevo.
4. La idempotencia la hace cumplir la base de datos con una restricción UNIQUE, no un
   `if` en la aplicación, que tendría una ventana de carrera.
5. El contenido no confiable (documentos, respuestas de herramientas) viaja como datos
   estructurados. Un detector de intentos de inyección puede avisar, pero ninguna
   garantía depende de él.

## Alternativas consideradas

| Alternativa | Por qué se descartó |
|---|---|
| Pedir la confirmación en el prompt | No es una garantía: es justo lo que la consigna dice que no alcanza |
| Un segundo LLM que juzgue si la acción es válida | Probabilístico: moverlo de lugar no lo vuelve una garantía |
| Un framework con aprobación humana incorporada (por ejemplo, herramientas diferidas) | La consigna evalúa fundamentos; implementarlo a mano muestra el mecanismo y mantiene el código explicable |
| Filtrar con palabras clave las inyecciones | Se evade con saltos de línea, caracteres invisibles o paráfrasis: sirve como alarma, no como barrera |

## Consecuencias

**A favor**

- Las garantías se prueban con un modelo guionado que intenta violarlas, sin gastar API.
- El comportamiento seguro no depende de qué modelo se use.

**En contra**

- Más código que un agente que ejecuta lo que el modelo pide.
- La confirmación es rígida: ante la duda sobre un "sí", no se ejecuta y se vuelve a preguntar.

## Cómo se verifica

Tests de las guardas con un modelo adversario que llama `crear_reclamo` sin confirmar,
con parámetros cambiados, repetido y con herramientas inexistentes. Los contratos de
`make arch` fijan quién puede depender de quién.
