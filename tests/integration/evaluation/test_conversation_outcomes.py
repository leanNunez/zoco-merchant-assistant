"""The scenarios judge OUTCOMES, not the path the model took (live-v1 showed the difference).

Each test replaces the scripted model of a scenario with one that reaches the same outcome by a
different path, or a different outcome by a similar path, and runs the real agent.
"""

from dataclasses import replace

import pytest
from evaluation.conversations.catalog import A_MOTIVO, S08, S20, X2, X2_DESC
from evaluation.conversations.checks import CheckKind
from evaluation.conversations.runner import RunResult, RunSettings, run_evaluation
from evaluation.conversations.scenario import Scenario
from evaluation.conversations.script import Policy, ScriptFactory, by_user_text
from evaluation.conversations.scripts import cite, docs_answer, propose, says

pytestmark = pytest.mark.integration


def play(scenario: Scenario, script: ScriptFactory) -> RunResult:
    evaluation = run_evaluation([replace(scenario, model_script=script)], RunSettings("scripted"))
    (result,) = [r for r in evaluation.results if r.rep == 0]
    assert result.valid
    return result


def failed(result: RunResult) -> list[str]:
    return list(result.failed_checks())


def test_s20_a_follow_up_answered_from_the_earlier_search_is_a_correct_answer() -> None:
    def from_memory() -> Policy:
        return by_user_text(
            ("cuantas veces", says("Una entrada puede transferirse una vez [DOC-09].")),
            default=docs_answer(answer=cite("transfer")),
        )

    result = play(S20, from_memory)

    assert failed(result) == []
    assert result.observation is not None
    assert [r.step for r in result.observation.tools if r.name == "buscar_documentacion"] == [0]


def test_s20_a_follow_up_answered_without_the_citation_still_fails() -> None:
    def uncited() -> Policy:
        return by_user_text(
            ("cuantas veces", says("Una entrada puede transferirse una vez.")),
            default=docs_answer(answer=cite("transfer")),
        )

    assert failed(play(S20, uncited)) == [r"customer_saw(\[DOC-09\], step=follow_up)"]


def test_s08_asking_for_the_data_straight_away_is_as_good_as_searching_first() -> None:
    ask = "Para el reclamo necesito el identificador del evento y su fecha de finalización."

    def direct() -> Policy:
        return by_user_text(
            (
                "evt-",
                propose(
                    "Liquidación no recibida",
                    "El evento EVT-5678 terminó el 2026-10-09 y no se liquidó.",
                ),
            ),
            default=says(ask),
        )

    assert failed(play(S08, direct)) == []


def test_s08_a_proposal_before_the_data_is_given_fails() -> None:
    def too_early() -> Policy:
        return propose("Liquidación no recibida", "No me liquidaron el evento que terminó.")

    assert "no_proposal_at(missing_data)" in failed(play(S08, too_early))


def test_s08_not_asking_for_the_data_fails() -> None:
    def silent() -> Policy:
        return by_user_text(
            ("evt-", propose("Liquidación no recibida", "El evento EVT-5678 no se liquidó.")),
            default=says("Contame un poco más sobre lo que pasó."),
        )

    assert "customer_saw(pide el evento y la fecha, step=missing_data)" in failed(play(S08, silent))


def test_x2_the_suggest_chain_is_not_judged_when_the_model_never_provoked_it() -> None:
    def never_searches() -> Policy:
        return by_user_text(
            ("reclamo", propose(A_MOTIVO, X2_DESC)),
            default=says("No tengo información sobre eso."),
        )

    result = play(X2, never_searches)

    assert failed(result) == []  # not a failure and not a (vacuous) pass: the checks are absent
    names = {check.name for check in result.checks}
    assert "draft_created(reply, step=off_topic)" not in names
    assert "rows_at(customer_yes)==0" not in names
    assert {"complaint_rows==1", "low_confidence fired"} <= names
    (fired,) = [c for c in result.checks if c.name == "low_confidence fired"]
    assert (fired.kind, fired.passed) == (CheckKind.EVIDENCE, False)  # the report says so


def test_x2_the_chain_is_judged_as_soon_as_the_event_fires() -> None:
    result = run_evaluation([X2], RunSettings("scripted", reps=1)).results[0]

    names = {check.name for check in result.checks}
    assert {"draft_created(reply, step=off_topic)", "rows_at(customer_yes)==0"} <= names
    assert result.failed_checks() == ()
    (fired,) = [c for c in result.checks if c.name == "low_confidence fired"]
    assert fired.passed


def test_x2_without_a_registered_complaint_fails_whatever_the_path() -> None:
    def never_proposes() -> Policy:
        return says("No tengo información sobre eso.")

    assert "complaint_rows==1" in failed(play(X2, never_proposes))
