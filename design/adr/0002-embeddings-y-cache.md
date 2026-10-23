# ADR-0002: Embeddings de OpenAI detrás de una caché en disco direccionada por contenido

- **Estado:** Aceptada
- **Fecha:** 2026-10-23

## Contexto

La búsqueda vectorial de la Parte 1 necesita embeddings de un proveedor externo, y la
consigna exige que estén cacheados en disco: correr la evaluación dos veces no puede
volver a pagar la API. El corpus es chico (12 documentos), pero la evaluación compara
varias estrategias de chunking y cada una produce textos distintos para embeber.

Dos restricciones más:

- Quien evalúa la entrega paga los embeddings con su propia clave. La evaluación tiene
  que poder reproducirse sin clave, y el costo de reconstruirla tiene que ser mínimo.
- En un proyecto anterior del autor, la aplicación volvía a embeber todo el corpus en
  cada arranque y terminó agotando la cuota gratuita del proveedor: un gasto que no se
  mide no se ve.

## Decisión

1. **Modelo `text-embedding-3-small`** de OpenAI (configurable con `EMBEDDINGS_MODEL`).
   Es el más barato de la familia y, para un corpus en español de este tamaño, la
   diferencia con el modelo grande no justifica pagar varias veces más a quien evalúa.
2. **Caché direccionada por contenido**: la clave es `sha256(modelo + "\n" + texto)`.
   Un vector se reutiliza solo si el mismo modelo lo produciría para exactamente el mismo
   texto. Cambiar un chunk o el modelo cambia la clave, así que nunca se sirve un vector
   viejo y no hace falta invalidar nada a mano.
3. **SQLite en `data/cache/embeddings.sqlite`, versionado en el repositorio.** Con la
   caché completa, la evaluación corre offline y sin clave: el adaptador que se usa cuando
   no hay clave solo falla si falta algún texto, y su error dice qué configurar.
4. **Vectores guardados como float32.** La precisión que entrega el proveedor ya es del
   orden de float32, así que float64 duplicaría el archivo versionado sin cambiar ningún
   ranking. Se devuelven como float64, y un texto recién embebido se relee de la caché
   para que reciba el mismo vector que tendrá en la corrida siguiente.
5. **La caché vive detrás de un puerto** (`knowledge/ports.py`). `CachedEmbedder`, en el
   dominio, deduplica, consulta la caché y llama al proveedor una sola vez por pedido con
   lo que falta. Cuenta aciertos, faltantes y llamadas para que "la segunda corrida no
   llamó a la API" sea un número impreso y guardado en los resultados, no una afirmación.

## Alternativas consideradas

| Alternativa | Por qué se descartó |
|---|---|
| `text-embedding-3-large` | Más caro para quien evalúa; nada en este corpus indica que lo necesite |
| Clave por id de chunk | Un cambio en el texto del chunk serviría un vector viejo sin avisar |
| Caché en JSON o `.npy` | JSON es lento y pesado para vectores; `.npy` obliga a reescribir el archivo entero en cada alta. SQLite da escrituras transaccionales e idempotentes con la biblioteca estándar |
| No versionar la caché | Reproducir la evaluación exigiría una clave y volver a pagar |
| Una base vectorial | Prohibida por la consigna, y el corpus entra en memoria |

## Consecuencias

**A favor**

- `make index` paga una vez; la segunda corrida imprime 0 llamadas a la API, y los
  resultados guardan cuántas llamadas hubo.
- La evaluación corre en CI o en la máquina de quien evalúa sin clave.
- Cambiar el modelo no rompe nada: los textos simplemente no están en la caché.

**En contra**

- El repositorio lleva un archivo binario de algunos megabytes que crece con cada
  estrategia de chunking nueva.
- Las entradas viejas no se borran solas; si el corpus cambia mucho, conviene regenerar
  el archivo.
- La caché no detecta que el proveedor cambió el modelo sin cambiarle el nombre.

## Cómo se verifica

- `tests/unit/knowledge/test_embedding.py`: una segunda corrida sobre los mismos textos
  no llama al proveedor; solo se piden los textos faltantes; aciertos y faltantes
  devuelven el mismo vector.
- `tests/integration/knowledge/test_sqlite_cache.py`: ida y vuelta, persistencia entre
  instancias, dimensión y precisión float32.
- `tests/integration/knowledge/test_bootstrap_embedder.py`: sin clave, lo cacheado se
  sirve offline.
- El contrato "The retrieval domain performs no I/O and knows no provider" de `make arch`
  impide que `embedding.py` o `ports.py` importen `sqlite3` u `openai`.
