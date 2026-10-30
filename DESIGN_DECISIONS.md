# Decisiones de diseño

Dos partes: las decisiones que dan forma al harness, y cómo se adaptaría a un complejo de Zoco
Canchas (el caso de diseño de la sección 8, sin implementación).

## El harness

| Decisión | Por qué | Alternativa descartada |
|---|---|---|
| Mismo repositorio, paquete nuevo `src/cortex/` | La consigna pide reutilizar el proyecto; el historial muestra la evolución y los tests de la primera entrega siguen cuidando lo reutilizado | Repo nuevo: más chico, pero la reutilización se ve menos y cada arreglo exige publicar otra versión |
| La empresa sale de un contexto autenticado y ningún repositorio recibe su id | El acceso cruzado queda imposible por construcción, no por disciplina | Filtrar por empresa en cada consulta "con cuidado" |
| Verificación que deniega por defecto; el alcance se revisa antes que el esquema | Un intento de elegir otra empresa termina la ejecución con un motivo específico | Validar solo el esquema: rechaza el campo, pero no distingue el ataque de un error |
| Escrituras: confirmación exacta, registro por (empresa, `operation_id`) y clave única en el sistema externo | Dos defensas independientes contra el duplicado; el registro además permite conciliar | Reintentar confiando en que el sistema externo sea idempotente (lo que hacía la primera entrega) |
| Un timeout de escritura es incierto: se busca por id y, si no se aplicó, se intenta una vez más | No se puede saber si llegó; solo un rechazo seguro prueba que no se aplicó | Tratarlo como transitorio y reintentar |
| Reclamos en un archivo por empresa, sin transacción compartida con el harness | Así es un sistema externo de verdad: la incertidumbre es real y se prueba | Una sola base: el "resultado incierto" sería artificial |
| La versión de control se compara al llamar al modelo, al ejecutar y al entregar (en la misma transacción que el envío) | Una toma de control puede llegar en cualquier momento | Mirarla solo al empezar |
| La toma de control es un hecho que informa Zoco Chat, con versión y compare-and-set; con la misma versión gana la persona | La persona ya tomó la conversación. La máquina de la primera entrega exige horario abierto para AUTO → HUMAN (habría que forzarlo con `True`), rechaza una segunda toma desde HUMAN y no tiene versiones; de ella se reutilizan los estados y `capabilities_for` | Usar su `transition` |
| Cinco motivos de fin y un detalle en una sola tabla: BLOCKED es seguridad, NEEDS_HUMAN es "le toca a una persona" | Un criterio que se puede explicar y probar | Un motivo por caso, mezclando los dos sentidos |
| El seguimiento lo decide una tabla pura; si no se sabe si hay consentimiento o el estado de la oportunidad, no se contacta | La decisión comercial no depende del modelo y respeta el permiso del cliente | Pedirle la decisión al modelo |
| Trazas con lista blanca y enmascarado al escribir, guardadas mientras ocurren | Nada sin filtrar llega a ningún destino, y un fallo deja su historia | Filtrar al exportar |
| La evaluación no lee `expected`, usa reloj fijo y un verificador que lee los archivos | Un PASS tiene que salir del estado real, no de lo que el adaptador dice | Comparar solo la salida JSON |

## Caso: Zoco Canchas, Chat, PDV y Pagos

**Situación.** Queda un turno libre hoy. El agente puede ofrecerlo con hasta 10 % de descuento,
pero solo después de que el dueño apruebe esa oferta.

**Datos a consultar** (lecturas, por empresa): el turno en Canchas con su versión; tarifa de lista y
costo variable de la franja; consumo promedio en el PDV de quienes reservan; consentimiento,
bajas e historial de cada cliente en Chat; y la política del dueño (descuento máximo, piso de
margen, contactos por día).

**Flujo.**
1. Canchas emite `turno_liberado`. El código arma una oferta con valores exactos: cancha, hora,
   precio, descuento, vencimiento y clientes elegibles.
2. **Aprobación del dueño antes de ofrecer.** La oferta es una escritura pendiente que solo puede
   confirmar un principal con permiso `ofertas:aprobar` (el agente no lo tiene). La aprobación
   queda atada a la huella de esos valores y a la versión del turno: si cambia el precio o el
   turno, hay que volver a aprobar.
3. El agente la envía por Chat solo a clientes con consentimiento y si nadie tomó la conversación.
4. Si el cliente acepta, se pide un **bloqueo con vencimiento** en Canchas (`LIBRE → BLOQUEADO`
   solo si la versión no cambió). Sin bloqueo no hay cobro.
5. Se genera el cobro en Pagos con la referencia de la operación como clave de idempotencia.

**Eventos que evitan la sobreventa.** Canchas es la única fuente de verdad del turno y todos los
canales (Chat, PDV, web) pasan por el mismo bloqueo con versión. Eventos: `turno_bloqueado`,
`turno_reservado` (cancela ofertas y bloqueos ajenos), `bloqueo_vencido` (libera el turno),
`pago_confirmado`, `pago_fallido` y `pago_reintegrado`. El agente nunca decide la disponibilidad
desde su memoria: la consulta al bloquear.

**Cálculos que quedan en código**, nunca en el modelo:
- `precio_final = redondeo(precio_lista × (1 − d))`, con `d ≤ min(10 %, máximo del dueño)` y en
  `Decimal`.
- Que `precio_final + consumo_pdv_esperado − costo_variable` no baje del piso de margen. Si baja,
  se reduce el descuento o no se ofrece.
- Elegibilidad del cliente, vencimientos y cupos de contacto.
- El monto a cobrar, idéntico al aprobado.

El modelo solo redacta el texto; los valores los inserta el código.

**Confirmación del cobro.**
- Solo la confirma el aviso firmado de Pagos (`pago_confirmado`), con monto, moneda y referencia
  iguales a los aprobados; recién ahí el turno queda reservado. Un "ya pagué" no confirma nada.
- Si el aviso se pierde, se concilia consultando el pago por su referencia, como hoy con los reclamos.
- Un pago que llega con el bloqueo vencido y el turno vendido genera un reintegro y una tarea
  para una persona.

**Cómo medir reservas adicionales y margen.**
- Con el correr de las semanas, una parte de los turnos libres elegibles, elegida al azar, no
  recibe ofertas: es el grupo de control.
- **Reservas adicionales:** diferencia de ocupación entre los turnos con oferta y los de control.
- **Margen incremental:** `precio_final − costo_variable` más el consumo en el PDV atribuido a esa
  reserva, menos lo que habrían pagado a precio de lista los clientes que igual iban a reservar
  (la canibalización, que el grupo de control también mide).
- **Embudo desde las trazas:** aprobadas → enviadas → aceptadas → bloqueadas → pagadas →
  asistidas, más las bajas y los reclamos que causan los contactos.
