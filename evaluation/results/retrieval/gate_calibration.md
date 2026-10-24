# Calibración de la compuerta `no_data`

Generado por `make eval-retrieval` (`uv run python -m evaluation.retrieval calibrate`). No editar a mano: se regenera igual en cada corrida.

La compuerta decide cuándo `buscar_documentacion` dice que la documentación no tiene nada sobre la consulta. Un `no_data` falso es seguro pero inútil (la conversación pasa a SUGGEST y el cliente no recibe nada hasta que un operador aprueba); un `ok` falso solo le da al modelo pasajes poco relacionados, y el modelo tiene la instrucción de no inventar. Por eso la regla prefiere no disparar de más.

## Procedencia

- Modelo de embeddings: `text-embedding-3-small`
- Chunking: `document` (12 chunks), el que seleccionó la regla declarada de `report.md`
- sha256 del corpus, `questions.jsonl` y `calibration_queries.jsonl`: `e8708c40938f1e49c80238663ca180a94dbac8fc002ec1f06b03b78c26ea00f2`
- Consultas: 20 del kit, 30 cortas legítimas y 45 fuera de tema, 13 de ellas `hard`
- Llamadas a la API de embeddings durante esta corrida: 0

## Reglas declaradas antes de medir

1. Los conjuntos de consultas se escribieron antes de medir y no se modifican después de ver los puntajes: `kit` (las 20 preguntas de `questions.jsonl`, referenciadas), `short_legit` (frases cortas y naturales que el corpus responde) y `off_topic` (fuera del dominio; las marcadas `hard` comparten palabras genéricas con el dominio).
2. Señales: el mejor puntaje BM25 de la consulta sobre los chunks `document` y el mejor coseno entre el vector de la consulta y los de los chunks.
3. Compuerta híbrida: `no_data` si BM25 < τ_b **y** coseno < τ_v. Compuerta degradada (sin embeddings, solo existe BM25): `no_data` si BM25 < τ_b_degradado.
4. Selección de los umbrales: (a) toda consulta de `kit` y de `short_legit` sigue respondiendo; (b) entre las compuertas que cumplen (a), la que rechaza más consultas `off_topic`; (c) entre empates, el centro del intervalo factible. Un umbral solo importa por entre qué dos puntajes observados cae, así que cada eje se parte en celdas y el umbral es el punto medio de la celda elegida. En dos ejes, el centro es el punto medio del rango que ocupan las celdas empatadas en cada eje; si esa celda no está empatada, se toma la empatada más cercana (distancia L1 con cada eje normalizado por su rango).
5. Relajación: si (a) solo se cumple con una compuerta que no rechaza ninguna consulta `off_topic`, se relaja lo justo para conservar las 20 del `kit`, se aplican de nuevo (b) y (c) y se informan por nombre las `short_legit` que quedan rechazadas.
6. Tokenizador con stopwords en la búsqueda del agente: solo si (i) recall@3 y MRR de `hybrid+meta(demote)` sobre las 20 preguntas del kit no bajan respecto del tokenizador por defecto **y** (ii) sube el margen factible de BM25, definido como (L - U) / L con L el menor mejor-BM25 de las consultas legítimas (kit y short_legit) y U el mayor de las `off_topic`. Se normaliza por L porque quitar palabras funcionales cambia la escala de BM25; el margen absoluto se informa al lado. Si falla (i) o (ii), se mantiene el tokenizador por defecto.

## Los puntajes por grupo

Mejor puntaje de cada consulta sobre los chunks. Cada celda: mínimo / mediana / máximo.

| Grupo | Consultas | BM25 (por defecto) | BM25 (stopwords) | Coseno |
|---|---:|---|---|---|
| kit (preguntas de `questions.jsonl`) | 20 | 3.926 / 8.032 / 19.656 | 2.193 / 7.492 / 17.390 | 0.448 / 0.585 / 0.675 |
| short_legit (frases cortas legítimas) | 30 | 0.077 / 2.937 / 9.272 | 0.000 / 2.515 / 8.009 | 0.379 / 0.517 / 0.665 |
| off_topic (fuera del dominio) | 45 | 0.000 / 2.037 / 4.071 | 0.000 / 0.000 / 3.479 | 0.092 / 0.241 / 0.538 |
|   de ellas `hard` | 13 | 0.077 / 2.152 / 4.071 | 0.000 / 1.893 / 3.479 | 0.268 / 0.424 / 0.538 |
|   de ellas las demás | 32 | 0.000 / 2.012 / 3.470 | 0.000 / 0.000 / 2.636 | 0.092 / 0.206 / 0.395 |

### Solapamiento

Distancia entre la consulta legítima (kit o short_legit) más débil y la fuera de tema más fuerte; negativa si se solapan. El margen relativo es esa distancia sobre el puntaje legítimo más débil.

| Señal | Legítima más débil | Fuera de tema más fuerte | Margen | Margen relativo | Fuera de tema que la igualan o superan | Legítimas por debajo de la más fuerte |
|---|---|---|---:|---:|---:|---:|
| BM25 (por defecto) | 0.077 (L05) | 4.071 (N34) | -3.993 | -51.596 | 43 | 23 |
| BM25 (stopwords) | 0.000 (L05) | 3.479 (N34) | -3.479 | indefinido | 45 | 21 |
| Coseno | 0.379 (L24) | 0.538 (N42) | -0.159 | -0.421 | 12 | 23 |

## Umbrales elegidos

Cada umbral se da con el intervalo de umbrales equivalentes del que sale (cualquier valor dentro rechaza exactamente las mismas consultas medidas).

| Tokenizador | Compuerta | τ_b (BM25) | τ_v (coseno) | Fuera de tema rechazadas | Celdas empatadas | Relajada |
|---|---|---|---|---:|---:|---|
| tokenizador por defecto (seleccionado) | degradada | 0.039 (0.000, 0.077] | — | 2 | 1 | no |
| tokenizador por defecto (seleccionado) | híbrida | 11.824 (10.553, 13.095] | 0.366 (0.3539, 0.3787] | 33 | 35 | no |
| tokenizador con stopwords | degradada | 2.183 (2.173, 2.193] | — | 38 | 1 | sí |
| tokenizador con stopwords | híbrida | 11.238 (9.806, 12.670] | 0.366 (0.3539, 0.3787] | 33 | 30 | no |

**Seleccionado para el agente: tokenizador por defecto.** Compuerta degradada: `no_data` si BM25 < 0.039. Compuerta híbrida: `no_data` si BM25 < 11.824 y coseno < 0.366.

### Qué significan estos umbrales

- **Híbrida.** Una compuerta de solo coseno con τ_v = 0.366 rechazaría 33 de 45 consultas fuera de tema y 0 de 50 legítimas; la compuerta híbrida rechaza 33 fuera de tema. BM25 solo rescata una consulta de coseno bajo si pasa de 11.824: en estas consultas rescata 0. Dicho claro: con estos datos el coseno hace el trabajo y el umbral de BM25 queda en el medio de una meseta ancha de valores equivalentes.
- **Degradada.** El umbral es 0.039: por debajo solo quedan las consultas que no comparten ninguna palabra con el corpus (por ejemplo `hola`). BM25 no tiene stopwords, así que casi cualquier frase en español comparte `el`, `de` o `la` con los documentos y puntúa por encima. La regla pide que ninguna consulta legítima quede rechazada, y las legítimas cortas puntúan casi cero: la compuerta degradada es, por la regla, casi vacía, y el modo sin claves deja pasar casi todo (falla tolerable).
- **Alternativa que no se usa: exigir solo el kit en modo degradado.** El umbral sería 3.9, rechazaría 44 de 45 fuera de tema y dejaría sin respuesta 21 consultas legítimas: L01 «¿Cómo transfiero una entrada?» (BM25 1.82, coseno 0.589); L02 «Quiero hacer un reclamo.» (BM25 2.67, coseno 0.538); L03 «No me liquidaron un evento, quiero hacer un reclamo.» (BM25 3.13, coseno 0.517); L04 «no me llegó la entrada» (BM25 0.99, coseno 0.394); L05 «cuánto cobran de comisión» (BM25 0.08, coseno 0.433); L07 «hasta cuándo puedo devolver» (BM25 3.78, coseno 0.500); L08 «puedo transferir mi entrada» (BM25 2.98, coseno 0.665); L11 «me cobraron dos veces» (BM25 1.12, coseno 0.387); L12 «cuántos días tarda la liquidación» (BM25 3.57, coseno 0.584); L13 «qué pasa si cancelan el evento» (BM25 2.32, coseno 0.540); L16 «cómo me doy de alta como organizador» (BM25 2.90, coseno 0.545); L17 «puedo cambiar la fecha de mi evento» (BM25 2.52, coseno 0.485); L18 «qué pasa si el lector escanea sin internet» (BM25 2.15, coseno 0.524); L19 «quién puede publicar un evento» (BM25 2.55, coseno 0.553); L20 «cuánto cobraban de comisión en agosto» (BM25 2.19, coseno 0.412); L21 «se puede ampliar la capacidad del lugar» (BM25 3.86, coseno 0.408); L22 «cuánto tarda una devolución» (BM25 3.87, coseno 0.581); L23 «qué tipos de reclamo hay» (BM25 2.72, coseno 0.663); L24 «qué es un lote» (BM25 2.72, coseno 0.379); L26 «cuándo me pagan lo que vendí» (BM25 2.87, coseno 0.398); L30 «quiero devolver una entrada» (BM25 1.34, coseno 0.423).

## Matrices de confusión

Cuántas consultas de cada grupo responde la compuerta (no dice `no_data`) y cuántas rechaza. Lo bueno es responder todas las legítimas y rechazar las fuera de tema.

### Tokenizador por defecto (seleccionado)

| Grupo | Degradada: responde | Degradada: rechaza | Híbrida: responde | Híbrida: rechaza |
|---|---:|---:|---:|---:|
| kit (20) | 20 | 0 | 20 | 0 |
| short_legit (30) | 30 | 0 | 30 | 0 |
| off_topic (45) | 43 | 2 | 12 | 33 |
|   de ellas `hard` (13) | 13 | 0 | 10 | 3 |

### Tokenizador con stopwords (no seleccionado)

| Grupo | Degradada: responde | Degradada: rechaza | Híbrida: responde | Híbrida: rechaza |
|---|---:|---:|---:|---:|
| kit (20) | 20 | 0 | 20 | 0 |
| short_legit (30) | 17 | 13 | 30 | 0 |
| off_topic (45) | 7 | 38 | 12 | 33 |
|   de ellas `hard` (13) | 5 | 8 | 10 | 3 |

## Qué consultas quedan mal

### Compuerta degradada

- Legítimas rechazadas: ninguna.
- Fuera de tema que pasan (43 de 45): N01 «¿Cuál es la capital de Australia?» (BM25 1.99, coseno 0.108); N02 «¿Quién ganó el último mundial de fútbol?» (BM25 0.15, coseno 0.092); N03 «Pasame una receta de milanesas» (BM25 0.67, coseno 0.174); N04 «¿Va a llover mañana?» (BM25 0.19, coseno 0.219); N05 «Contame un chiste» (BM25 0.78, coseno 0.223); N06 «¿Cuántos habitantes tiene Brasil?» (BM25 2.33, coseno 0.120); N07 «¿Cómo se arma un currículum?» (BM25 2.48, coseno 0.327); N08 «¿Quién pintó la Mona Lisa?» (BM25 0.22, coseno 0.092); N09 «Recomendame una serie para ver esta noche» (BM25 3.47, coseno 0.195); N10 «¿Cuánto es 15 por 12?» (BM25 3.12, coseno 0.170); N12 «¿Cómo estás?» (BM25 2.04, coseno 0.281); N13 «buenos días» (BM25 1.69, coseno 0.230); N14 «¿Qué hora es en Tokio?» (BM25 2.24, coseno 0.185); N15 «Explicame qué es la inflación» (BM25 2.19, coseno 0.248); N16 «¿Cómo se instala Python en Windows?» (BM25 1.93, coseno 0.124); N17 «¿Cuál es la mejor marca de zapatillas?» (BM25 1.99, coseno 0.229); N18 «¿Qué es la fotosíntesis?» (BM25 2.19, coseno 0.153); N19 «¿Cómo hago para bajar de peso?» (BM25 2.05, coseno 0.159); N20 «Escribime un poema sobre el mar» (BM25 2.75, coseno 0.125); N21 «¿Cuánto está el dólar hoy?» (BM25 2.42, coseno 0.241); N22 «¿Cómo funciona Netflix?» (BM25 1.70, coseno 0.342); N23 «¿Qué plan de Spotify me conviene?» (BM25 2.64, coseno 0.395); N24 «¿Cómo se cambia una rueda pinchada?» (BM25 1.88, coseno 0.220); N25 «¿Quién es el presidente de Estados Unidos?» (BM25 1.89, coseno 0.140); N26 «Traducime good morning al francés» (BM25 0.58, coseno 0.121); N27 «¿Cómo cargo la SUBE?» (BM25 2.11, coseno 0.387); N28 «¿Qué significa la palabra efímero?» (BM25 0.98, coseno 0.246); N29 «¿Cuándo es el cumpleaños de Messi?» (BM25 3.18, coseno 0.217); N30 «¿Cómo puedo aprender a tocar la guitarra?» (BM25 2.04, coseno 0.106); N32 «¿Cuál es la distancia de la Tierra a la Luna?» (BM25 2.10, coseno 0.116); N33 «¿Cuánto sale una entrada del cine?» (BM25 1.44, coseno 0.405); N34 «¿Cómo hago una transferencia bancaria?» (BM25 4.07, coseno 0.481); N35 «reclamo de mi seguro del auto» (BM25 2.15, coseno 0.444); N36 «¿Cómo valido mi tarjeta de crédito?» (BM25 2.36, coseno 0.428); N37 «¿Cuánto cobra Mercado Libre de comisión?» (BM25 0.08, coseno 0.424); N38 «No me llegó el paquete que compré por internet» (BM25 1.62, coseno 0.268); N39 «¿Cuántos días tarda en salir un pasaporte?» (BM25 3.42, coseno 0.354); N40 «¿Hasta cuándo puedo pagar el monotributo?» (BM25 3.85, coseno 0.418); N41 «Quiero cancelar la suscripción del gimnasio» (BM25 0.43, coseno 0.323); N42 «¿Qué pasa si cancelan mi vuelo?» (BM25 1.50, coseno 0.538); N43 «¿Cuándo me liquidan el sueldo?» (BM25 1.76, coseno 0.432); N44 «¿Cómo me doy de alta en el monotributo?» (BM25 2.44, coseno 0.422); N45 «¿Puedo transferir mi línea de celular a otra person…» (BM25 2.44, coseno 0.448).

### Compuerta híbrida

- Legítimas rechazadas: ninguna.
- Fuera de tema que pasan (12 de 45): N23 «¿Qué plan de Spotify me conviene?» (BM25 2.64, coseno 0.395); N27 «¿Cómo cargo la SUBE?» (BM25 2.11, coseno 0.387); N33 «¿Cuánto sale una entrada del cine?» (BM25 1.44, coseno 0.405); N34 «¿Cómo hago una transferencia bancaria?» (BM25 4.07, coseno 0.481); N35 «reclamo de mi seguro del auto» (BM25 2.15, coseno 0.444); N36 «¿Cómo valido mi tarjeta de crédito?» (BM25 2.36, coseno 0.428); N37 «¿Cuánto cobra Mercado Libre de comisión?» (BM25 0.08, coseno 0.424); N40 «¿Hasta cuándo puedo pagar el monotributo?» (BM25 3.85, coseno 0.418); N42 «¿Qué pasa si cancelan mi vuelo?» (BM25 1.50, coseno 0.538); N43 «¿Cuándo me liquidan el sueldo?» (BM25 1.76, coseno 0.432); N44 «¿Cómo me doy de alta en el monotributo?» (BM25 2.44, coseno 0.422); N45 «¿Puedo transferir mi línea de celular a otra person…» (BM25 2.44, coseno 0.448).

## El piso anterior

El piso anterior (BM25 ≥ 5.25 sobre chunking `section`, calibrado con las 20 preguntas del kit y 16 fuera de tema) sobre estas mismas consultas:

- Consultas legítimas que rechaza: 17 de 50 (L01 «¿Cómo transfiero una entrada?», L02 «Quiero hacer un reclamo.», L03 «No me liquidaron un evento, quiero hacer un reclamo.», L04 «no me llegó la entrada», L05 «cuánto cobran de comisión», L07 «hasta cuándo puedo devolver», L11 «me cobraron dos veces», L13 «qué pasa si cancelan el evento», L16 «cómo me doy de alta como organizador», L17 «puedo cambiar la fecha de mi evento», L18 «qué pasa si el lector escanea sin internet», L20 «cuánto cobraban de comisión en agosto», L22 «cuánto tarda una devolución», L23 «qué tipos de reclamo hay», L24 «qué es un lote», L27 «qué significa que el evento esté pausado», L30 «quiero devolver una entrada»).
- Consultas fuera de tema que deja pasar: 2 de 45.

## Tokenizador con stopwords: decisión

| Condición | Por defecto | Con stopwords | Se cumple |
|---|---:|---:|---|
| (i) recall@3 de `hybrid+meta(demote)` sobre el kit | 0.975 | 0.975 | sí (no baja) |
| (i) MRR de `hybrid+meta(demote)` sobre el kit | 1.000 | 1.000 | sí (no baja) |
| (ii) margen relativo de BM25 | -51.596 | indefinido (el puntaje legítimo más débil es 0) | no (sube) |
| margen absoluto de BM25 (informativo) | -3.993 | -3.479 | — |

Resultado: **no se adopta: se mantiene el tokenizador por defecto**.

## Por consulta

Con el tokenizador por defecto. **Responde** / **Rechaza** es lo que hace cada compuerta; ✗ marca una decisión equivocada (legítima rechazada, fuera de tema que pasa).

| Id | Grupo | Consulta | BM25 | BM25 (stopwords) | Coseno | Doc BM25 | Doc coseno | Degradada | Híbrida |
|---|---|---|---:|---:|---:|---|---|---|---|
| Q01 | kit | ¿Qué rol puede publicar eventos una vez aprobado? | 5.789 | 4.608 | 0.5899 | DOC-02 | DOC-03 | Responde | Responde |
| Q02 | kit | ¿Cuánto demora la revisión de alta con documentació… | 9.038 | 8.870 | 0.5248 | DOC-02 | DOC-02 | Responde | Responde |
| Q03 | kit | ¿Qué pasa si los lotes superan la capacidad del rec… | 8.094 | 7.624 | 0.4476 | DOC-03 | DOC-04 | Responde | Responde |
| Q04 | kit | ¿Cuántos minutos permanece reservado un carrito sin… | 9.123 | 8.636 | 0.5439 | DOC-04 | DOC-04 | Responde | Responde |
| Q05 | kit | ¿Cuál es el cargo por una entrada de $10.000 en ZT-… | 19.656 | 17.390 | 0.6748 | DOC-05 | DOC-05 | Responde | Responde |
| Q06 | kit | ¿En cuántos días hábiles se liquida normalmente un… | 7.094 | 6.243 | 0.4894 | DOC-06 | DOC-12 | Responde | Responde |
| Q07 | kit | ¿Cuál es el mínimo de entradas por mes del plan ZT-… | 14.490 | 13.199 | 0.6750 | DOC-07 | DOC-07 | Responde | Responde |
| Q08 | kit | ¿Cuentan las entradas gratuitas para el mínimo mens… | 15.956 | 14.303 | 0.6004 | DOC-07 | DOC-07 | Responde | Responde |
| Q09 | kit | ¿Se devuelve el cargo de servicio si el organizador… | 7.486 | 7.359 | 0.6477 | DOC-08 | DOC-08 | Responde | Responde |
| Q10 | kit | ¿Hasta cuándo puede pedirse devolución por reprogra… | 10.292 | 8.359 | 0.5485 | DOC-08 | DOC-08 | Responde | Responde |
| Q11 | kit | ¿Cuántas veces puede transferirse una entrada? | 4.316 | 3.734 | 0.6538 | DOC-09 | DOC-09 | Responde | Responde |
| Q12 | kit | ¿Qué pasa con el QR original después de una transfe… | 6.489 | 4.987 | 0.5798 | DOC-09 | DOC-09 | Responde | Responde |
| Q13 | kit | ¿Qué estados devuelve la validación de un QR? | 3.926 | 2.193 | 0.6156 | DOC-11 | DOC-10 | Responde | Responde |
| Q14 | kit | ¿Cuál es el riesgo de escanear offline en dos dispo… | 6.702 | 6.474 | 0.5699 | DOC-10 | DOC-10 | Responde | Responde |
| Q15 | kit | ¿Puede abrirse un reclamo sin que el usuario confir… | 5.963 | 4.777 | 0.5613 | DOC-06 | DOC-11 | Responde | Responde |
| Q16 | kit | ¿Qué significa que crear_reclamo devuelva REC-00012… | 10.553 | 9.806 | 0.6276 | DOC-11 | DOC-11 | Responde | Responde |
| Q17 | kit | ¿Cuál era el mínimo mensual de ZT-PRO-2 en julio de… | 13.095 | 12.670 | 0.6180 | DOC-07 | DOC-12 | Responde | Responde |
| Q18 | kit | ¿Cuánto cobraba ZT-START-1 en agosto de 2026? | 14.633 | 14.198 | 0.5695 | DOC-12 | DOC-12 | Responde | Responde |
| Q19 | kit | ¿Qué documento contiene las tarifas aplicables en o… | 7.970 | 6.543 | 0.5356 | DOC-05 | DOC-12 | Responde | Responde |
| Q20 | kit | ¿Puede un organizador en estado EN_REVISION publica… | 7.070 | 4.813 | 0.6352 | DOC-02 | DOC-03 | Responde | Responde |
| L01 | short_legit | ¿Cómo transfiero una entrada? | 1.818 | 0.740 | 0.5885 | DOC-12 | DOC-09 | Responde | Responde |
| L02 | short_legit | Quiero hacer un reclamo. | 2.674 | 1.893 | 0.5377 | DOC-11 | DOC-11 | Responde | Responde |
| L03 | short_legit | No me liquidaron un evento, quiero hacer un reclamo. | 3.128 | 2.548 | 0.5171 | DOC-06 | DOC-06 | Responde | Responde |
| L04 | short_legit | no me llegó la entrada | 0.991 | 0.805 | 0.3937 | DOC-09 | DOC-04 | Responde | Responde |
| L05 | short_legit | cuánto cobran de comisión | 0.077 | 0.000 | 0.4328 | DOC-08 | DOC-05 | Responde | Responde |
| L06 | short_legit | qué es ZT-PRO-2 | 9.272 | 7.829 | 0.4334 | DOC-05 | DOC-05 | Responde | Responde |
| L07 | short_legit | hasta cuándo puedo devolver | 3.775 | 2.125 | 0.4997 | DOC-08 | DOC-08 | Responde | Responde |
| L08 | short_legit | puedo transferir mi entrada | 2.979 | 3.000 | 0.6655 | DOC-09 | DOC-09 | Responde | Responde |
| L09 | short_legit | cómo valido un QR | 4.943 | 4.194 | 0.6075 | DOC-10 | DOC-10 | Responde | Responde |
| L10 | short_legit | quiero registrar un reclamo por un pago duplicado | 6.106 | 4.900 | 0.5279 | DOC-11 | DOC-11 | Responde | Responde |
| L11 | short_legit | me cobraron dos veces | 1.123 | 1.124 | 0.3869 | DOC-10 | DOC-05 | Responde | Responde |
| L12 | short_legit | cuántos días tarda la liquidación | 3.572 | 3.412 | 0.5836 | DOC-02 | DOC-06 | Responde | Responde |
| L13 | short_legit | qué pasa si cancelan el evento | 2.323 | 1.983 | 0.5396 | DOC-06 | DOC-08 | Responde | Responde |
| L14 | short_legit | cuánto dura la reserva del carrito | 8.389 | 8.009 | 0.5081 | DOC-04 | DOC-04 | Responde | Responde |
| L15 | short_legit | cuántas entradas necesito vender con el plan pro | 4.914 | 3.726 | 0.5524 | DOC-07 | DOC-07 | Responde | Responde |
| L16 | short_legit | cómo me doy de alta como organizador | 2.895 | 2.871 | 0.5450 | DOC-02 | DOC-02 | Responde | Responde |
| L17 | short_legit | puedo cambiar la fecha de mi evento | 2.516 | 2.288 | 0.4848 | DOC-02 | DOC-03 | Responde | Responde |
| L18 | short_legit | qué pasa si el lector escanea sin internet | 2.146 | 1.311 | 0.5238 | DOC-09 | DOC-10 | Responde | Responde |
| L19 | short_legit | quién puede publicar un evento | 2.545 | 2.483 | 0.5527 | DOC-01 | DOC-03 | Responde | Responde |
| L20 | short_legit | cuánto cobraban de comisión en agosto | 2.191 | 2.013 | 0.4118 | DOC-12 | DOC-05 | Responde | Responde |
| L21 | short_legit | se puede ampliar la capacidad del lugar | 3.855 | 3.410 | 0.4076 | DOC-03 | DOC-03 | Responde | Responde |
| L22 | short_legit | cuánto tarda una devolución | 3.874 | 3.496 | 0.5809 | DOC-08 | DOC-08 | Responde | Responde |
| L23 | short_legit | qué tipos de reclamo hay | 2.722 | 1.893 | 0.6632 | DOC-11 | DOC-11 | Responde | Responde |
| L24 | short_legit | qué es un lote | 2.723 | 1.706 | 0.3787 | DOC-05 | DOC-04 | Responde | Responde |
| L25 | short_legit | las entradas gratis cuentan para el mínimo | 8.115 | 6.834 | 0.5167 | DOC-07 | DOC-07 | Responde | Responde |
| L26 | short_legit | cuándo me pagan lo que vendí | 2.875 | 0.000 | 0.3984 | DOC-05 | DOC-06 | Responde | Responde |
| L27 | short_legit | qué significa que el evento esté pausado | 3.999 | 3.940 | 0.4257 | DOC-03 | DOC-03 | Responde | Responde |
| L28 | short_legit | puedo volver a transferir una entrada que ya transf… | 6.206 | 4.730 | 0.6346 | DOC-09 | DOC-09 | Responde | Responde |
| L29 | short_legit | el escáner dice ya utilizada | 6.389 | 6.293 | 0.4856 | DOC-10 | DOC-10 | Responde | Responde |
| L30 | short_legit | quiero devolver una entrada | 1.337 | 0.740 | 0.4234 | DOC-09 | DOC-08 | Responde | Responde |
| N01 | off_topic | ¿Cuál es la capital de Australia? | 1.988 | 0.000 | 0.1080 | DOC-01 | DOC-02 | Responde ✗ | Rechaza |
| N02 | off_topic | ¿Quién ganó el último mundial de fútbol? | 0.150 | 0.000 | 0.0919 | DOC-08 | DOC-09 | Responde ✗ | Rechaza |
| N03 | off_topic | Pasame una receta de milanesas | 0.673 | 0.000 | 0.1745 | DOC-09 | DOC-09 | Responde ✗ | Rechaza |
| N04 | off_topic | ¿Va a llover mañana? | 0.194 | 0.000 | 0.2189 | DOC-03 | DOC-08 | Responde ✗ | Rechaza |
| N05 | off_topic | Contame un chiste | 0.782 | 0.000 | 0.2230 | DOC-11 | DOC-11 | Responde ✗ | Rechaza |
| N06 | off_topic | ¿Cuántos habitantes tiene Brasil? | 2.330 | 2.320 | 0.1201 | DOC-04 | DOC-02 | Responde ✗ | Rechaza |
| N07 | off_topic | ¿Cómo se arma un currículum? | 2.475 | 0.000 | 0.3265 | DOC-06 | DOC-02 | Responde ✗ | Rechaza |
| N08 | off_topic | ¿Quién pintó la Mona Lisa? | 0.219 | 0.000 | 0.0923 | DOC-03 | DOC-09 | Responde ✗ | Rechaza |
| N09 | off_topic | Recomendame una serie para ver esta noche | 3.470 | 2.636 | 0.1955 | DOC-02 | DOC-11 | Responde ✗ | Rechaza |
| N10 | off_topic | ¿Cuánto es 15 por 12? | 3.122 | 1.358 | 0.1703 | DOC-05 | DOC-06 | Responde ✗ | Rechaza |
| N11 | off_topic | hola | 0.000 | 0.000 | 0.2591 | — | DOC-02 | Rechaza | Rechaza |
| N12 | off_topic | ¿Cómo estás? | 2.037 | 0.000 | 0.2812 | DOC-05 | DOC-02 | Responde ✗ | Rechaza |
| N13 | off_topic | buenos días | 1.688 | 1.718 | 0.2304 | DOC-08 | DOC-02 | Responde ✗ | Rechaza |
| N14 | off_topic | ¿Qué hora es en Tokio? | 2.244 | 2.173 | 0.1851 | DOC-03 | DOC-12 | Responde ✗ | Rechaza |
| N15 | off_topic | Explicame qué es la inflación | 2.191 | 0.000 | 0.2479 | DOC-05 | DOC-08 | Responde ✗ | Rechaza |
| N16 | off_topic | ¿Cómo se instala Python en Windows? | 1.933 | 0.000 | 0.1240 | DOC-06 | DOC-02 | Responde ✗ | Rechaza |
| N17 | off_topic | ¿Cuál es la mejor marca de zapatillas? | 1.988 | 0.000 | 0.2294 | DOC-01 | DOC-01 | Responde ✗ | Rechaza |
| N18 | off_topic | ¿Qué es la fotosíntesis? | 2.191 | 0.000 | 0.1532 | DOC-05 | DOC-01 | Responde ✗ | Rechaza |
| N19 | off_topic | ¿Cómo hago para bajar de peso? | 2.053 | 0.000 | 0.1586 | DOC-12 | DOC-02 | Responde ✗ | Rechaza |
| N20 | off_topic | Escribime un poema sobre el mar | 2.755 | 0.000 | 0.1248 | DOC-05 | DOC-11 | Responde ✗ | Rechaza |
| N21 | off_topic | ¿Cuánto está el dólar hoy? | 2.416 | 0.000 | 0.2406 | DOC-02 | DOC-12 | Responde ✗ | Rechaza |
| N22 | off_topic | ¿Cómo funciona Netflix? | 1.705 | 0.000 | 0.3420 | DOC-06 | DOC-01 | Responde ✗ | Rechaza |
| N23 | off_topic | ¿Qué plan de Spotify me conviene? | 2.644 | 1.742 | 0.3945 | DOC-05 | DOC-07 | Responde ✗ | Responde ✗ |
| N24 | off_topic | ¿Cómo se cambia una rueda pinchada? | 1.877 | 0.000 | 0.2205 | DOC-06 | DOC-09 | Responde ✗ | Rechaza |
| N25 | off_topic | ¿Quién es el presidente de Estados Unidos? | 1.893 | 1.747 | 0.1400 | DOC-01 | DOC-03 | Responde ✗ | Rechaza |
| N26 | off_topic | Traducime good morning al francés | 0.578 | 0.000 | 0.1214 | DOC-07 | DOC-02 | Responde ✗ | Rechaza |
| N27 | off_topic | ¿Cómo cargo la SUBE? | 2.115 | 2.007 | 0.3874 | DOC-05 | DOC-05 | Responde ✗ | Responde ✗ |
| N28 | off_topic | ¿Qué significa la palabra efímero? | 0.978 | 0.000 | 0.2460 | DOC-02 | DOC-04 | Responde ✗ | Rechaza |
| N29 | off_topic | ¿Cuándo es el cumpleaños de Messi? | 3.181 | 0.000 | 0.2169 | DOC-08 | DOC-06 | Responde ✗ | Rechaza |
| N30 | off_topic | ¿Cómo puedo aprender a tocar la guitarra? | 2.042 | 0.000 | 0.1062 | DOC-06 | DOC-02 | Responde ✗ | Rechaza |
| N31 | off_topic | gracias | 0.000 | 0.000 | 0.2587 | — | DOC-02 | Rechaza | Rechaza |
| N32 | off_topic | ¿Cuál es la distancia de la Tierra a la Luna? | 2.105 | 0.000 | 0.1162 | DOC-01 | DOC-05 | Responde ✗ | Rechaza |
| N33 | off_topic (hard) | ¿Cuánto sale una entrada del cine? | 1.444 | 0.740 | 0.4054 | DOC-04 | DOC-04 | Responde ✗ | Responde ✗ |
| N34 | off_topic (hard) | ¿Cómo hago una transferencia bancaria? | 4.071 | 3.479 | 0.4809 | DOC-09 | DOC-09 | Responde ✗ | Responde ✗ |
| N35 | off_topic (hard) | reclamo de mi seguro del auto | 2.152 | 1.893 | 0.4438 | DOC-11 | DOC-11 | Responde ✗ | Responde ✗ |
| N36 | off_topic (hard) | ¿Cómo valido mi tarjeta de crédito? | 2.357 | 2.277 | 0.4281 | DOC-10 | DOC-02 | Responde ✗ | Responde ✗ |
| N37 | off_topic (hard) | ¿Cuánto cobra Mercado Libre de comisión? | 0.077 | 0.000 | 0.4238 | DOC-08 | DOC-05 | Responde ✗ | Responde ✗ |
| N38 | off_topic (hard) | No me llegó el paquete que compré por internet | 1.620 | 0.073 | 0.2680 | DOC-05 | DOC-04 | Responde ✗ | Rechaza |
| N39 | off_topic (hard) | ¿Cuántos días tarda en salir un pasaporte? | 3.416 | 3.412 | 0.3539 | DOC-02 | DOC-08 | Responde ✗ | Rechaza |
| N40 | off_topic (hard) | ¿Hasta cuándo puedo pagar el monotributo? | 3.848 | 2.125 | 0.4184 | DOC-08 | DOC-12 | Responde ✗ | Responde ✗ |
| N41 | off_topic (hard) | Quiero cancelar la suscripción del gimnasio | 0.427 | 0.000 | 0.3231 | DOC-03 | DOC-08 | Responde ✗ | Rechaza |
| N42 | off_topic (hard) | ¿Qué pasa si cancelan mi vuelo? | 1.499 | 1.109 | 0.5381 | DOC-09 | DOC-08 | Responde ✗ | Responde ✗ |
| N43 | off_topic (hard) | ¿Cuándo me liquidan el sueldo? | 1.760 | 0.000 | 0.4322 | DOC-08 | DOC-06 | Responde ✗ | Responde ✗ |
| N44 | off_topic (hard) | ¿Cómo me doy de alta en el monotributo? | 2.436 | 2.288 | 0.4223 | DOC-02 | DOC-02 | Responde ✗ | Responde ✗ |
| N45 | off_topic (hard) | ¿Puedo transferir mi línea de celular a otra person… | 2.443 | 2.266 | 0.4476 | DOC-09 | DOC-09 | Responde ✗ | Responde ✗ |

## Límites

- Los tres conjuntos son chicos (20, 30 y 45 consultas) y los escribió quien escribió el sistema: no son una muestra de lo que van a preguntar personas reales, y los umbrales se eligieron sobre estas mismas consultas, así que sobreestiman lo que pasaría con otras.
- Una consulta legítima distinta de las medidas puede caer debajo de la compuerta (falla segura: pasa a SUGGEST) y una fuera de tema puede pasarla (falla tolerable: el modelo recibe pasajes poco relacionados y tiene la instrucción de no inventar). El diseño prefiere la segunda.
- El corpus son 12 documentos con pocos cientos de palabras: los puntajes BM25 dependen del tamaño del corpus (el IDF) y los cosenos de `text-embedding-3-small`. Cambiar el corpus o el modelo exige volver a calibrar (`make eval-retrieval`).
- Las consultas fuera de tema que comparten palabras con el dominio (`hard`) son el caso difícil de verdad: ningún umbral sobre estas señales separa 'habla de entradas' de 'habla de las entradas de ZocoTickets'. La defensa para esos casos es el prompt, no la compuerta.
