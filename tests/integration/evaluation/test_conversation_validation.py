"""The evaluator validated against itself, on every scenario of the catalog.

1. The scripted models with the REAL agent pass every check, except the checks a scenario
   declares as a known limitation of the agent, which must fail exactly as declared.
2. The null model (answers a fixed sentence, never calls a tool) passes every LOCK check and
   fails at least one UTILITY check per scenario: "no answer" is never scored as compliance.
3. The unsafe reference agent fails at least one LOCK check in every attack and every state
   crossing: the locks detect a violation when there is one.
"""

import pytest
from evaluation.conversations.catalog import BASELINE, SCENARIOS
from evaluation.conversations.checks import CheckKind
from evaluation.conversations.runner import Evaluation, RunResult, RunSettings, run_evaluation
from evaluation.conversations.scenario import Category, Say, Scenario, in_split

pytestmark = pytest.mark.integration

ADVERSARIAL = frozenset({Category.POLICY_ATTACK, Category.STATE_CROSSING})
IDS = [scenario.id for scenario in SCENARIOS]


@pytest.fixture(scope="module")
def runs() -> dict[str, Evaluation]:
    return {
        mode: run_evaluation(SCENARIOS, RunSettings(mode, reps=1))
        for mode in ("scripted", "null", "naive")
    }


def only(evaluation: Evaluation, scenario: Scenario) -> RunResult:
    (result,) = evaluation.results_of(scenario.id)
    assert result.valid, (scenario.id, result.failure_class, result.failure_detail)
    return result


def failing(result: RunResult, kind: CheckKind) -> list[str]:
    return [c.name for c in result.checks if c.kind is kind and not c.passed]


def test_the_catalog_covers_what_part_4_asks_for() -> None:
    assert len(SCENARIOS) >= 15
    assert len(set(IDS)) == len(IDS)
    by_category = {
        category: [s for s in SCENARIOS if s.category is category] for category in Category
    }
    assert all(by_category.values())
    assert len(by_category[Category.STATE_CROSSING]) >= 3
    assert all(len(s.user_messages) >= 2 for s in SCENARIOS)
    poisoned = [s for s in SCENARIOS if s.world.corpus_patch is not None]
    assert {s.category for s in poisoned} == {Category.POLICY_ATTACK}  # rule 7, indirect
    assert [s.split for s in poisoned] == ["dev", "heldout"]
    assert all(isinstance(step, Say) or step.tag for s in SCENARIOS for step in s.steps)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
def test_1_the_scripted_models_pass_every_check_but_the_declared_limitations(
    runs: dict[str, Evaluation], scenario: Scenario
) -> None:
    result = only(runs["scripted"], scenario)
    assert result.failed_checks() == scenario.expected_failures
    assert failing(result, CheckKind.EVIDENCE) == []  # every attack really happened


@pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
def test_2_the_null_model_breaks_no_lock_and_is_never_useful(
    runs: dict[str, Evaluation], scenario: Scenario
) -> None:
    result = only(runs["null"], scenario)
    assert failing(result, CheckKind.LOCK) == []
    assert failing(result, CheckKind.UTILITY) != []


@pytest.mark.parametrize(
    "scenario", [s for s in SCENARIOS if s.category in ADVERSARIAL], ids=lambda s: s.id
)
def test_3_the_unsafe_agent_is_caught_by_the_locks(
    runs: dict[str, Evaluation], scenario: Scenario
) -> None:
    assert failing(only(runs["naive"], scenario), CheckKind.LOCK) != []


def test_a_short_legitimate_question_is_answered_in_auto_and_is_a_regression_scenario(
    runs: dict[str, Evaluation],
) -> None:
    # S20 was a known limitation: the old lexical floor sent "¿Cómo transfiero una entrada?" to
    # SUGGEST. The calibrated gate answers it, so it now passes every check, in AUTO.
    (s20,) = [s for s in SCENARIOS if s.id == "S20"]
    result = only(runs["scripted"], s20)

    assert (s20.known_limitation, s20.expected_failures) == (None, ())
    assert result.failed_checks() == ()
    assert result.observation is not None
    assert result.observation.final_state == "AUTO"


def test_no_scenario_is_left_as_a_known_limitation() -> None:
    assert [s.id for s in SCENARIOS if s.known_limitation] == []


def test_the_catalog_has_a_dev_split_and_a_held_out_one_that_copies_nothing() -> None:
    dev, held = in_split(SCENARIOS, "dev"), in_split(SCENARIOS, "heldout")

    assert [s.id for s in held] == [f"H0{n}" for n in range(1, 9)]
    assert len(dev) == 26
    assert len(dev) + len(held) == len(SCENARIOS) == len(in_split(SCENARIOS, "all"))
    assert not {s.id for s in dev} & {s.id for s in held}
    said = {step.text for s in dev for step in s.steps if isinstance(step, Say)}
    held_said = {step.text for s in held for step in s.steps if isinstance(step, Say)}
    # New wordings, never a copy of a dev message (a bare "sí" or "no" is not a wording).
    assert not {text for text in said & held_said if len(text) > 6}
    assert not {s.title for s in dev} & {s.title for s in held}


def test_every_held_out_scenario_is_a_new_wording_of_a_dev_situation_with_the_same_discipline() -> (
    None
):
    held = in_split(SCENARIOS, "heldout")

    assert {s.category for s in held} == set(Category)
    for scenario in held:
        assert scenario.split == "heldout"
        assert scenario.known_limitation is None
        assert {c.name for c in BASELINE} <= {c.name for c in scenario.checks}
