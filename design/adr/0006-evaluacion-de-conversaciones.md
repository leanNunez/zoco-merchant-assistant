# ADR-0006: Evaluación de conversaciones con aserciones sobre hechos

- **Estado:** Aceptada
- **Fecha:** 2026-10-24

## Contexto

La Parte 4 pide demostrar que el agente funciona y mostrar dónde no: al menos 15 conversaciones de
varios turnos (exitosas, ambiguas, con herramientas que fallan, con intentos de violar las políticas,
incluida la regla 7), al menos tres que crucen estados de la máquina de control, cada una corrida tres
veces o más, con la tasa de cumplimiento por caso decidida con aserciones sobre hechos observables, y
el script con sus resultados guardados.

Hay tres riesgos si no se decide nada. Un evaluador que lee la prosa del agente mide una opinión, no un
comportamiento. Un evaluador que nunca falla mide nada: un modelo que no hace nada "cumple" todas las
políticas. Y un evaluador que arma su propio agente de prueba mide otra configuración que la de
producción.

## Decisión

1. **Hechos, no texto.** El cumplimiento se decide sobre una `Observation`: las llamadas a
   herramientas con sus argumentos y su resultado, las filas leídas directo del archivo SQLite
   después de cada paso, lo que llegó al cliente (nunca un borrador), el historial guardado, el
   registro de control, la bandeja del operador y el estado final. El texto solo se mira por rasgos
   estructurales: una cita `[DOC-NN]`, un número `REC-`, una plantilla del código, una cifra, una
   copia literal de 40 caracteres del prompt. Los analizadores son propios del evaluador, no los del
   agente: si el evaluador usara el clasificador del agente, solo confirmaría que el agente está de
   acuerdo consigo mismo.
2. **Tres tipos de check.** **LOCK**: una política que garantiza el código; tiene que valer con
   cualquier modelo, también uno hostil o mudo, y se escribe como implicación cuando hace falta (por
   ejemplo, "si se invalidó una acción, la bandeja lo dice"). **UTILITY**: el agente hizo lo útil;
   depende del modelo. **EVIDENCIA**: el escenario ejerció lo que dice (el modelo hostil sí intentó el
   ataque); se informa y no puntúa, para distinguir un LOCK probado de uno vacío. Una propiedad que el
   código no garantiza (que el prompt no se filtre) es UTILITY, aunque sea una política: llamarla LOCK
   afirmaría una garantía que no existe.
3. **La composición real.** Cada (escenario, repetición) corre en un mundo nuevo: directorio temporal,
   base SQLite, almacén, reloj y fallas propios, construido con `bootstrap.build_agent_service`. Solo
   se reemplazan el modelo, el reloj, el `sleeper` y los inyectores de fallas. Para eso
   `build_agent_service` recibe dos parámetros opcionales (`sales_faults`, `complaint_faults`); sin
   ellos el comportamiento de producción no cambia. Las configuraciones fuera de línea se arman con
   valores explícitos, así una clave o una tasa de fallas del entorno no se cuela.
4. **Modelos guionados como políticas.** Cada escenario trae su guion: una función que mira lo que
   recibiría el modelo y decide la respuesta (llamar una herramienta o contestar con lo que devolvió).
   Es intencionado en los casos benignos y hostil u obediente en los ataques y cruces, porque ahí lo que
   se prueba es que los candados del código aguantan cualquier modelo.
5. **Tres validaciones del evaluador, en CI.** `scripted` con el agente real: todos los checks pasan,
   salvo los que un caso declara como limitación conocida, que fallan exactamente como se declaró.
   `null` (una frase fija, ninguna herramienta): pasa todos los LOCK y falla al menos un UTILITY por
   caso. `naive`, un agente inseguro de referencia que vive solo en `evaluation/` (ofrece todo, ejecuta
   todo, sin confirmación ni máquina de control): falla al menos un LOCK en cada ataque y cada cruce.
   Es el control positivo: muestra que los LOCK detectan una violación cuando la hay.
6. **Corridas inválidas aparte.** Una corrida que no terminó por algo ajeno al comportamiento del
   agente (proveedor caído, credenciales, modelo inexistente, tope de gasto, tiempo máximo por
   conversación, error del arnés) va a `errors.jsonl` con su clase y no se puntúa ni como falla ni como
   acierto. Un caso con menos de dos corridas válidas se marca como evidencia insuficiente.
7. **Gasto acotado en `live`.** El modelo real se construye con `bootstrap.build_chat_model`. Cada
   llamada registra el modelo servido y el uso; el costo sale de `pricing.py`. Un modelo sin precio no
   cuenta como gratis: el modo se niega a empezar si el pedido no tiene precio, y se detiene si uno
   servido no lo tiene. Al llegar al tope (`--budget-usd`, `EVAL_BUDGET_USD`, 1,00 USD por defecto) la
   corrida para y queda PARCIAL. Las repeticiones corren por vuelta (todos los casos, después todos otra
   vez) para que una corrida cortada cubra los casos parejo.
8. **Resultados reproducibles y vigilados.** `results.json`, `errors.jsonl` y `report.md` no tienen
   hora ni rutas temporales y tienen las claves ordenadas: una segunda corrida los reproduce byte a
   byte. Un test regenera los tres modos fuera de línea y los compara con los archivos versionados.

## Alternativas consideradas

| Alternativa | Por qué se descartó |
|---|---|
| Un LLM como juez de cada respuesta | Sería un instrumento más que validar (acuerdo con anotaciones humanas, sesgo hacia respuestas largas, variabilidad), con costo por corrida. Los requisitos del enunciado son verificables con hechos; la calidad de la prosa queda declarada como no medida |
| Comparar el texto de la respuesta con uno esperado | Rompe con cualquier reformulación del modelo real y premia copiar plantillas. Solo se comparan plantillas del código y rasgos estructurales |
| Un agente de prueba armado a mano para el evaluador | Mediría otra configuración. El mundo se construye con la raíz de composición de producción |
| Guiones como listas fijas de respuestas | Se desalinean apenas el agente llama al modelo una vez más o una menos, y no sirven para el agente inseguro. Una política que reacciona a la transcripción funciona en los dos |
| Contar las corridas inválidas como fallas | Mezclaría un corte del proveedor con un mal comportamiento del modelo y bajaría la tasa sin motivo; contarlas como aciertos sería peor |
| Correr una sola vez cada caso | El enunciado pide tres o más porque el modelo no es determinista. En los modos guionados las repeticiones son idénticas, y se dice |

## Consecuencias

**A favor**

- Cada número del informe se puede rastrear a un hecho del `results.json` (herramienta, fila, estado).
- Los LOCK no son decorativos: el modelo nulo muestra que no se cumplen por omisión y el agente inseguro
  muestra que detectan violaciones (165 de 774 checks LOCK fallan con él; ninguno con el agente real).
- Escribir los escenarios encontró debilidades reales del agente, documentadas en el README: el piso
  léxico deja sin respuesta preguntas legítimas cortas (S20, fijado como limitación conocida), SUGGEST
  no vuelve solo a AUTO, el prompt de sistema no tiene candado, el texto de un documento envenenado se
  puede citar, y la pregunta de confirmación muestra valores escritos por el modelo sin control.

**En contra**

- La calidad de la redacción no se mide.
- En los modos guionados los UTILITY en verde dicen que un buen modelo podría hacerlo, no que el real lo
  haga. Eso solo lo responde una corrida `live`, que todavía no existe.
- La recuperación corre en modo degradado; con embeddings los rankings pueden cambiar.
- Con tres repeticiones los intervalos de Wilson son anchos (3/3 da [0,44; 1,00]).
- Los archivos de resultados pesan (unos 2 MB entre los tres modos) porque guardan cada trayectoria.

## Cómo se verifica

- `tests/unit/evaluation/test_conversation_checks.py`: cada tipo de check con una observación que pasa
  y una que falla; los analizadores fijados contra las plantillas reales.
- `tests/integration/evaluation/test_conversation_validation.py`: las tres validaciones sobre los 24
  escenarios.
- `tests/integration/evaluation/test_conversation_world.py`: aislamiento de cada mundo, el entorno no
  se cuela, el corpus envenenado vive solo en el directorio temporal, `bootstrap` sin inyectores lee las
  fallas de la configuración como antes.
- `tests/integration/evaluation/test_conversation_runner.py` y `test_conversation_cli.py`: corridas
  inválidas, tope de gasto, corrida parcial, credenciales rechazadas, tiempo máximo, todo con un modelo
  falso; dos corridas producen archivos idénticos.
- `tests/integration/evaluation/test_conversation_results_drift.py`: los resultados versionados son
  los que produce el código actual.
- `tests/integration/evaluation/test_conversation_naive.py`: nada en `src/` importa la evaluación.
