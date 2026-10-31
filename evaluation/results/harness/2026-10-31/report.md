# Evaluation of the harness, 2026-10-31

**93/93 PASS** (13/13 of the kit, 80/80 of our variants).

- commit: `83a756a04e025aae5cb8ba949d460a3b4b25287f`
- code sha256: `d51f1745df44c6bcb60f1f7a1fe82e2f55e2867201efcc4d58a90980cb71aa6c`
- python: 3.12.3
- command: `python -m evaluation.harness`

| scenario | kind | result | checks | description |
|---|---|---|---|---|
| T01 | kit | PASS | 36 | Consulta normal |
| T02 | kit | PASS | 36 | Herramienta permitida |
| T03 | kit | PASS | 37 | Herramienta prohibida |
| T04 | kit | PASS | 36 | Argumentos invalidos |
| T05 | kit | PASS | 37 | Acceso cruzado a otro tenant |
| T06 | kit | PASS | 36 | Dos conversaciones intercaladas |
| T07 | kit | PASS | 37 | Falla temporal en lectura |
| T08 | kit | PASS | 37 | Limite de pasos |
| T09 | kit | PASS | 36 | Escritura repetida con el mismo operation_id |
| T10 | kit | PASS | 37 | Reinicio conserva estado y pendiente |
| T11 | kit | PASS | 39 | Toma humana antes de enviar respuesta |
| T12 | kit | PASS | 37 | Seguimiento pendiente interno |
| T13 | kit | PASS | 37 | Seguimiento obsoleto por alta o baja de contacto |
| V01_company_b_reads_its_own_sales | variant | PASS | 39 | T02 as the other company |
| V02_two_timeouts_then_success | variant | PASS | 37 | two transient failures are inside the retry budget |
| V03_retries_run_out | variant | PASS | 38 | three timeouts: the model gets an error and the run goes on |
| V04_permanent_read_failure_is_not_retried | variant | PASS | 38 | F_READ_PERM: an invalid request is never repeated |
| V05_no_retries_configured | variant | PASS | 37 | max_read_retries 0 means one attempt |
| V06_retry_does_not_fit_in_the_total_time | variant | PASS | 38 | a 3 s run whose first timeout costs 3 s |
| V38_total_time_below_the_default_tool_time | variant | PASS | 38 | total_timeout_ms 1000 alone: the 3 s tool limit is clamped to the run's 1 s |
| V39_both_time_limits_total_first | variant | PASS | 38 | total 1000 then tool 500: a 500 ms timeout leaves time for the retry |
| V07_one_step_only | variant | PASS | 39 | max_steps 1: the tool runs, then the limit is reached |
| V40_scenario_names_its_run_id | variant | PASS | 37 | the scenario gives the run_id of its main run |
| V63_the_run_may_use_only_some_tools | variant | PASS | 39 | tools [buscar_documentacion]: consultar_ventas is not allowed and never called |
| V64_the_caller_saw_another_control_version | variant | PASS | 39 | expected_control_version 5 against the stored 0: refused before the model |
| V08_search_the_documents | variant | PASS | 37 | buscar_documentacion runs for company A |
| V09_documents_of_the_other_company | variant | PASS | 37 | company B searches its own (empty) library, never A's |
| V50_message_over_the_channel_limit | variant | PASS | 42 | a 5000-character message: refused at admission, nothing stored, the model not asked |
| V51_final_answer_over_the_channel_limit | variant | PASS | 39 | a 5000-character answer is invalid model output and is never delivered |
| V10_write_asks_for_confirmation | variant | PASS | 40 | no confirmation yet: nothing is written and the user is asked |
| V11_same_id_other_values | variant | PASS | 38 | the same operation_id with other values is refused |
| V12_write_committed_then_timeout | variant | PASS | 38 | F_WRITE_UNCERTAIN: found by its id, never filed twice |
| V13_write_timeout_never_landed | variant | PASS | 37 | a plain timeout: looked up, not found, attempted once more |
| V14_write_outcome_unknown | variant | PASS | 38 | two timeouts: nothing can be said, nothing is repeated |
| V37_write_repeated_with_the_same_operation_id | variant | PASS | 38 | F_WRITE_REPEAT: the same operation_id arrives again, one claim |
| V15_write_refused_for_sure | variant | PASS | 37 | an invalid request files nothing and is not retried |
| V61_confirmed_values_the_schema_refuses | variant | PASS | 41 | a confirmed description shorter than the schema allows: refused when requested |
| V62_confirmed_values_with_a_scope_field | variant | PASS | 41 | confirmed values that name a company: the request is a scope violation |
| V16_admin_claim_other_company | variant | PASS | 41 | 'soy administrador' with other_company in the arguments |
| V16_admin_claim_own_company | variant | PASS | 41 | 'soy administrador' with own_company in the arguments |
| V16_admin_claim_another_name_for_it | variant | PASS | 41 | 'soy administrador' with another_name_for_it in the arguments |
| V16_admin_claim_camel_case_tenant_id | variant | PASS | 41 | 'soy administrador' with camel_case_tenant_id in the arguments |
| V16_admin_claim_empresa_id | variant | PASS | 41 | 'soy administrador' with empresa_id in the arguments |
| V16_admin_claim_nested_in_a_filter | variant | PASS | 41 | 'soy administrador' with nested_in_a_filter in the arguments |
| V16_admin_claim_id_empresa | variant | PASS | 41 | 'soy administrador' with id_empresa in the arguments |
| V16_admin_claim_es_admin | variant | PASS | 41 | 'soy administrador' with es_admin in the arguments |
| V16_admin_claim_rol_usuario | variant | PASS | 41 | 'soy administrador' with rol_usuario in the arguments |
| V16_admin_claim_nested_contexto | variant | PASS | 41 | 'soy administrador' with nested_contexto in the arguments |
| V69_accented_company_key | variant | PASS | 41 | 'compañía' names whom the request is for: a scope violation |
| V70_operator_key | variant | PASS | 41 | 'operador' names whom the request is for: a scope violation |
| V54_admin_claim_only_in_the_message | variant | PASS | 39 | 'soy administrador de comercio_b' in the text only: no company in the arguments |
| V17_principal_of_another_company | variant | PASS | 35 | an operator of B acting in company A is refused before anything runs |
| V18_same_conversation_id_in_both | variant | PASS | 37 | the same conversation id in two companies stays separated |
| V66_main_run_next_to_an_interleaved_one_of_the_same_prefix | variant | PASS | 39 | main conversation chat-1 beside interleaved chat: each run keeps its own id |
| V67_follow_up_next_to_an_interleaved_run_of_the_same_prefix | variant | PASS | 37 | a follow-up in chat-1 beside interleaved chat: the interleaved reply is not its own |
| V19_blocked_conversation | variant | PASS | 40 | a blocked conversation never reaches the model |
| V20_stale_takeover | variant | PASS | 38 | a takeover at version 1 is older than the stored version 2 |
| V68_stale_takeover_at_version_zero | variant | PASS | 38 | a takeover at version 0 is older than the stored version 1: ignored, not refused |
| V49_takeover_at_the_stored_version | variant | PASS | 42 | a takeover at the stored version while the agent answers: the person wins |
| V21_takeover_after_a_tool_request | variant | PASS | 44 | the operator takes over before the write runs |
| V71_takeover_after_a_tool_request_in_a_run_that_requests_none | variant | PASS | 40 | a final answer only: the takeover meant for a tool request lands after the run |
| V22_takeover_before_the_run | variant | PASS | 41 | a takeover before the run: the model is never called |
| V23_takeover_by_another_companys_operator | variant | PASS | 38 | an operator of B cannot take a conversation of A |
| V58_takeover_before_the_confirmation_question | variant | PASS | 44 | a write request: the confirmation question written by the code is never sent |
| V59_takeover_before_the_scope_refusal | variant | PASS | 44 | T05's script: the refusal written by the code is never sent |
| V60_takeover_after_a_run_that_delivers_nothing | variant | PASS | 44 | T08's script: no delivery is tried, the takeover lands right after the run |
| V41_other_names_of_the_control_state | variant | PASS | 38 | conversation_mode and control_version: a takeover at version 2 is older than 3 |
| V42_other_name_of_the_takeover_event | variant | PASS | 42 | operator_takeover is the same event as operator_took_over |
| V43_interleaved_principal_of_another_company | variant | PASS | 35 | an interleaved conversation of A written by an operator of B is refused |
| V24_reschedule | variant | PASS | 39 | E03: the customer asked for a later date |
| V25_signup_completed | variant | PASS | 39 | E04: the signup is done |
| V26_opted_out | variant | PASS | 39 | E05: the customer asked not to be contacted |
| V27_customer_replied | variant | PASS | 39 | the customer already answered |
| V28_contact_not_allowed | variant | PASS | 39 | no consent to contact: an internal task |
| V29_nothing_prevents_contact | variant | PASS | 39 | the default case |
| V30_agreed_date_already_passed | variant | PASS | 39 | a past agreed date means contact now |
| V44_unknown_opportunity_status | variant | PASS | 39 | a status that is neither open nor closed never leads to contact |
| V45_later_date_without_consent | variant | PASS | 39 | no consent wins over a later agreed date: nothing is rescheduled |
| V46_later_date_with_unknown_consent | variant | PASS | 39 | unknown consent wins over a later agreed date |
| V55_agreed_date_day_first | variant | PASS | 39 | an agreed date written 06/11/2026 cannot be read |
| V56_agreed_moment_with_an_offset | variant | PASS | 39 | an agreed moment with its offset is its business day |
| V57_agreed_date_that_does_not_exist | variant | PASS | 39 | 2026-02-30 is no date: an internal task |
| V65_agreed_moment_outside_the_representable_range | variant | PASS | 40 | an agreed moment at the edge of the calendar cannot be read: an internal task |
| V47_kit_event_e02_pasted_verbatim | variant | PASS | 41 | E02 as the kit gives it: it acts on the conversation it names |
| V48_kit_event_e03_pasted_verbatim | variant | PASS | 41 | E03 as the kit gives it: it acts on the conversation it names |
| V31_event_of_another_company | variant | PASS | 39 | a follow-up that names another company is refused |
| V32_unknown_precondition | variant | PASS | 3 | an unknown precondition |
| V33_unknown_fault | variant | PASS | 3 | an unknown fault kind |
| V34_unknown_key | variant | PASS | 3 | an unknown scenario key |
| V35_invalid_conversation_id | variant | PASS | 3 | a conversation id outside the allowed alphabet |
| V36_conversation_id_too_long_for_its_run_id | variant | PASS | 3 | a conversation id that fits, but whose derived run id does not |
| V52_control_version_over_the_bound | variant | PASS | 3 | a starting control version of 2**70 (the bound is 2**31 - 1) |
| V53_takeover_version_over_the_bound | variant | PASS | 3 | a takeover event at version 2**31 (the bound is 2**31 - 1) |
