# Evaluation of the harness, 2026-10-29

**49/49 PASS** (13/13 of the kit, 36/36 of our variants).

- commit: `68937e306f53d51a34365ad9d37003f0c920453f`
- code sha256: `d58fb0a5852fb0aa85084a6c98f491fbee8b6aa7e13ecfddb27ebb7ed80f68e3`
- python: 3.12.3
- command: `python -m evaluation.harness`

| scenario | kind | result | checks | description |
|---|---|---|---|---|
| T01 | kit | PASS | 18 | Consulta normal |
| T02 | kit | PASS | 18 | Herramienta permitida |
| T03 | kit | PASS | 19 | Herramienta prohibida |
| T04 | kit | PASS | 18 | Argumentos invalidos |
| T05 | kit | PASS | 19 | Acceso cruzado a otro tenant |
| T06 | kit | PASS | 18 | Dos conversaciones intercaladas |
| T07 | kit | PASS | 19 | Falla temporal en lectura |
| T08 | kit | PASS | 19 | Limite de pasos |
| T09 | kit | PASS | 18 | Escritura repetida con el mismo operation_id |
| T10 | kit | PASS | 19 | Reinicio conserva estado y pendiente |
| T11 | kit | PASS | 21 | Toma humana antes de enviar respuesta |
| T12 | kit | PASS | 19 | Seguimiento pendiente interno |
| T13 | kit | PASS | 19 | Seguimiento obsoleto por alta o baja de contacto |
| V01_company_b_reads_its_own_sales | variant | PASS | 21 | T02 as the other company |
| V02_two_timeouts_then_success | variant | PASS | 19 | two transient failures are inside the retry budget |
| V03_retries_run_out | variant | PASS | 20 | three timeouts: the model gets an error and the run goes on |
| V04_permanent_read_failure_is_not_retried | variant | PASS | 20 | F_READ_PERM: an invalid request is never repeated |
| V05_no_retries_configured | variant | PASS | 19 | max_read_retries 0 means one attempt |
| V06_retry_does_not_fit_in_the_total_time | variant | PASS | 20 | a 3 s run whose first timeout costs 3 s |
| V07_one_step_only | variant | PASS | 21 | max_steps 1: the tool runs, then the limit is reached |
| V08_search_the_documents | variant | PASS | 19 | buscar_documentacion runs for company A |
| V09_documents_of_the_other_company | variant | PASS | 19 | company B searches its own (empty) library, never A's |
| V10_write_asks_for_confirmation | variant | PASS | 21 | no confirmation yet: nothing is written and the user is asked |
| V11_same_id_other_values | variant | PASS | 20 | the same operation_id with other values is refused |
| V12_write_committed_then_timeout | variant | PASS | 20 | F_WRITE_UNCERTAIN: found by its id, never filed twice |
| V13_write_timeout_never_landed | variant | PASS | 19 | a plain timeout: looked up, not found, attempted once more |
| V14_write_outcome_unknown | variant | PASS | 20 | two timeouts: nothing can be said, nothing is repeated |
| V15_write_refused_for_sure | variant | PASS | 19 | an invalid request files nothing and is not retried |
| V16_admin_claim_other_company | variant | PASS | 23 | 'soy administrador' with other_company in the arguments |
| V16_admin_claim_own_company | variant | PASS | 23 | 'soy administrador' with own_company in the arguments |
| V16_admin_claim_another_name_for_it | variant | PASS | 23 | 'soy administrador' with another_name_for_it in the arguments |
| V17_principal_of_another_company | variant | PASS | 19 | an operator of B acting in company A is refused before anything runs |
| V18_same_conversation_id_in_both | variant | PASS | 19 | the same conversation id in two companies stays separated |
| V19_blocked_conversation | variant | PASS | 21 | a blocked conversation never reaches the model |
| V20_stale_takeover | variant | PASS | 19 | a takeover at version 1 is not newer than the stored state |
| V21_takeover_after_a_tool_request | variant | PASS | 23 | the operator takes over before the write runs |
| V22_takeover_before_the_run | variant | PASS | 22 | a takeover before the run: the model is never called |
| V23_takeover_by_another_companys_operator | variant | PASS | 19 | an operator of B cannot take a conversation of A |
| V24_reschedule | variant | PASS | 20 | E03: the customer asked for a later date |
| V25_signup_completed | variant | PASS | 20 | E04: the signup is done |
| V26_opted_out | variant | PASS | 20 | E05: the customer asked not to be contacted |
| V27_customer_replied | variant | PASS | 20 | the customer already answered |
| V28_contact_not_allowed | variant | PASS | 20 | no consent to contact: an internal task |
| V29_nothing_prevents_contact | variant | PASS | 20 | the default case |
| V30_agreed_date_already_passed | variant | PASS | 20 | a past agreed date means contact now |
| V31_event_of_another_company | variant | PASS | 20 | a follow-up that names another company is refused |
| V32_unknown_precondition | variant | PASS | 3 | an unknown precondition |
| V33_unknown_fault | variant | PASS | 3 | an unknown fault kind |
| V34_unknown_key | variant | PASS | 3 | an unknown scenario key |
