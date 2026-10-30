# Evaluation of the harness, 2026-10-30

**72/72 PASS** (13/13 of the kit, 59/59 of our variants).

- commit: `6bcdebdd3fce8da803a1b9d55f636e81c0010b9d`
- code sha256: `6f75906629e7d55716deebcad40e0245660db9694e9c29f57c38add5157613d4`
- python: 3.12.3
- command: `python -m evaluation.harness`

| scenario | kind | result | checks | description |
|---|---|---|---|---|
| T01 | kit | PASS | 33 | Consulta normal |
| T02 | kit | PASS | 33 | Herramienta permitida |
| T03 | kit | PASS | 34 | Herramienta prohibida |
| T04 | kit | PASS | 33 | Argumentos invalidos |
| T05 | kit | PASS | 34 | Acceso cruzado a otro tenant |
| T06 | kit | PASS | 33 | Dos conversaciones intercaladas |
| T07 | kit | PASS | 34 | Falla temporal en lectura |
| T08 | kit | PASS | 34 | Limite de pasos |
| T09 | kit | PASS | 33 | Escritura repetida con el mismo operation_id |
| T10 | kit | PASS | 34 | Reinicio conserva estado y pendiente |
| T11 | kit | PASS | 36 | Toma humana antes de enviar respuesta |
| T12 | kit | PASS | 34 | Seguimiento pendiente interno |
| T13 | kit | PASS | 34 | Seguimiento obsoleto por alta o baja de contacto |
| V01_company_b_reads_its_own_sales | variant | PASS | 36 | T02 as the other company |
| V02_two_timeouts_then_success | variant | PASS | 34 | two transient failures are inside the retry budget |
| V03_retries_run_out | variant | PASS | 35 | three timeouts: the model gets an error and the run goes on |
| V04_permanent_read_failure_is_not_retried | variant | PASS | 35 | F_READ_PERM: an invalid request is never repeated |
| V05_no_retries_configured | variant | PASS | 34 | max_read_retries 0 means one attempt |
| V06_retry_does_not_fit_in_the_total_time | variant | PASS | 35 | a 3 s run whose first timeout costs 3 s |
| V38_total_time_below_the_default_tool_time | variant | PASS | 35 | total_timeout_ms 1000 alone: the 3 s tool limit is clamped to the run's 1 s |
| V39_both_time_limits_total_first | variant | PASS | 35 | total 1000 then tool 500: a 500 ms timeout leaves time for the retry |
| V07_one_step_only | variant | PASS | 36 | max_steps 1: the tool runs, then the limit is reached |
| V40_scenario_names_its_run_id | variant | PASS | 34 | the scenario gives the run_id of its main run |
| V08_search_the_documents | variant | PASS | 34 | buscar_documentacion runs for company A |
| V09_documents_of_the_other_company | variant | PASS | 34 | company B searches its own (empty) library, never A's |
| V50_message_over_the_channel_limit | variant | PASS | 39 | a 5000-character message: refused at admission, nothing stored, the model not asked |
| V51_final_answer_over_the_channel_limit | variant | PASS | 36 | a 5000-character answer is invalid model output and is never delivered |
| V10_write_asks_for_confirmation | variant | PASS | 37 | no confirmation yet: nothing is written and the user is asked |
| V11_same_id_other_values | variant | PASS | 35 | the same operation_id with other values is refused |
| V12_write_committed_then_timeout | variant | PASS | 35 | F_WRITE_UNCERTAIN: found by its id, never filed twice |
| V13_write_timeout_never_landed | variant | PASS | 34 | a plain timeout: looked up, not found, attempted once more |
| V14_write_outcome_unknown | variant | PASS | 35 | two timeouts: nothing can be said, nothing is repeated |
| V37_write_repeated_with_the_same_operation_id | variant | PASS | 35 | F_WRITE_REPEAT: the same operation_id arrives again, one claim |
| V15_write_refused_for_sure | variant | PASS | 34 | an invalid request files nothing and is not retried |
| V16_admin_claim_other_company | variant | PASS | 38 | 'soy administrador' with other_company in the arguments |
| V16_admin_claim_own_company | variant | PASS | 38 | 'soy administrador' with own_company in the arguments |
| V16_admin_claim_another_name_for_it | variant | PASS | 38 | 'soy administrador' with another_name_for_it in the arguments |
| V16_admin_claim_camel_case_tenant_id | variant | PASS | 38 | 'soy administrador' with camel_case_tenant_id in the arguments |
| V16_admin_claim_empresa_id | variant | PASS | 38 | 'soy administrador' with empresa_id in the arguments |
| V16_admin_claim_nested_in_a_filter | variant | PASS | 38 | 'soy administrador' with nested_in_a_filter in the arguments |
| V54_admin_claim_only_in_the_message | variant | PASS | 36 | 'soy administrador de comercio_b' in the text only: no company in the arguments |
| V17_principal_of_another_company | variant | PASS | 34 | an operator of B acting in company A is refused before anything runs |
| V18_same_conversation_id_in_both | variant | PASS | 34 | the same conversation id in two companies stays separated |
| V19_blocked_conversation | variant | PASS | 37 | a blocked conversation never reaches the model |
| V20_stale_takeover | variant | PASS | 35 | a takeover at version 1 is older than the stored version 2 |
| V49_takeover_at_the_stored_version | variant | PASS | 39 | a takeover at the stored version while the agent answers: the person wins |
| V21_takeover_after_a_tool_request | variant | PASS | 41 | the operator takes over before the write runs |
| V22_takeover_before_the_run | variant | PASS | 38 | a takeover before the run: the model is never called |
| V23_takeover_by_another_companys_operator | variant | PASS | 35 | an operator of B cannot take a conversation of A |
| V41_other_names_of_the_control_state | variant | PASS | 35 | conversation_mode and control_version: a takeover at version 2 is older than 3 |
| V42_other_name_of_the_takeover_event | variant | PASS | 39 | operator_takeover is the same event as operator_took_over |
| V43_interleaved_principal_of_another_company | variant | PASS | 34 | an interleaved conversation of A written by an operator of B is refused |
| V24_reschedule | variant | PASS | 36 | E03: the customer asked for a later date |
| V25_signup_completed | variant | PASS | 36 | E04: the signup is done |
| V26_opted_out | variant | PASS | 36 | E05: the customer asked not to be contacted |
| V27_customer_replied | variant | PASS | 36 | the customer already answered |
| V28_contact_not_allowed | variant | PASS | 36 | no consent to contact: an internal task |
| V29_nothing_prevents_contact | variant | PASS | 36 | the default case |
| V30_agreed_date_already_passed | variant | PASS | 36 | a past agreed date means contact now |
| V44_unknown_opportunity_status | variant | PASS | 36 | a status that is neither open nor closed never leads to contact |
| V45_later_date_without_consent | variant | PASS | 36 | no consent wins over a later agreed date: nothing is rescheduled |
| V46_later_date_with_unknown_consent | variant | PASS | 36 | unknown consent wins over a later agreed date |
| V47_kit_event_e02_pasted_verbatim | variant | PASS | 38 | E02 as the kit gives it: it acts on the conversation it names |
| V48_kit_event_e03_pasted_verbatim | variant | PASS | 38 | E03 as the kit gives it: it acts on the conversation it names |
| V31_event_of_another_company | variant | PASS | 36 | a follow-up that names another company is refused |
| V32_unknown_precondition | variant | PASS | 3 | an unknown precondition |
| V33_unknown_fault | variant | PASS | 3 | an unknown fault kind |
| V34_unknown_key | variant | PASS | 3 | an unknown scenario key |
| V35_invalid_conversation_id | variant | PASS | 3 | a conversation id outside the allowed alphabet |
| V36_conversation_id_too_long_for_its_run_id | variant | PASS | 3 | a conversation id that fits, but whose derived run id does not |
| V52_control_version_over_the_bound | variant | PASS | 3 | a starting control version of 2**70 (the bound is 2**31 - 1) |
| V53_takeover_version_over_the_bound | variant | PASS | 3 | a takeover event at version 2**31 (the bound is 2**31 - 1) |
