"""The documentation cannot drift from the code and from the saved results.

In earlier projects of the author the README described a system that no longer existed. These
tests read the documents and compare what they say with the sources of truth: the Makefile, the
repository tree, `results.json` and `metrics.json` of the saved runs, the scenario catalog, the
settings class and the gate constants. They need no network and no key, and they never read a
`.env` file. When one fails, fix the document (or regenerate the results), not the test.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from evaluation.conversations.catalog import SCENARIOS
from evaluation.conversations.report import CATEGORY_NAMES

from merchant_assistant import bootstrap
from merchant_assistant.shared.config import Settings

ROOT = Path(__file__).resolve().parents[2]
# The first delivery's documentation. It moved from README.md when the harness got its own.
README = ROOT / "PRUEBA_1.md"
HARNESS_README = ROOT / "README.md"
REVISION = ROOT / "REVISION_CONSIGNA.md"
DESIGN = ROOT / "DESIGN_DECISIONS.md"
SECURITY = ROOT / "SEGURIDAD.md"
SPEC_REVIEW = ROOT / "SPEC_REVIEW.md"
ADRS = tuple(sorted((ROOT / "design" / "adr").glob("*.md")))
DOCUMENTS = (README, HARNESS_README, REVISION, DESIGN, SECURITY, SPEC_REVIEW, *ADRS)
CONVERSATIONS = ROOT / "evaluation" / "results" / "conversations"
RETRIEVAL = ROOT / "evaluation" / "results" / "retrieval"

# Real-model runs of the final prompt: the dev split and the held-out split.
FINAL_LIVE = ("live", "live-heldout")
# Every directory of real-model runs, the iterations and the reverted attempt included.
ALL_LIVE = (
    "live-v1",
    "live-v2",
    "live-v3",
    "live-v4",
    "live",
    "live-heldout",
    "live-v6-attempt",
    "live-heldout-v6-attempt",
)
MODES = ("scripted", "null", "naive", *FINAL_LIVE)

# Settings fields that the `.env` template intentionally leaves out. They are the layout of the
# repository (the committed data, the corpus, the runtime database); changing them breaks the
# committed files and the saved results, so they are not offered as knobs.
OMITTED_FROM_TEMPLATE = {
    "EMBEDDINGS_CACHE_PATH": "the versioned embeddings cache",
    "SALES_PATH": "the committed sales file",
    "COMPLAINTS_DB_PATH": "the runtime claims database",
    "DOCS_DIR": "the corpus",
}

# Paths the documents may name although the file is not in the tree.
# `.env.example` is created by hand from the README template: it has its own test below.
CREATED_BY_HAND = {".env.example"}
# Backticked tokens that look like a path and are not one.
NOT_PATHS = {"hybrid/complete", "degraded/incomplete_cache", "degraded/no_key", "ok/no_data"}


# --------------------------------------------------------------------------- reading helpers


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def without_fences(text: str) -> str:
    """The text with its fenced code blocks removed."""
    return re.sub(r"^```.*?^```[^\n]*$", "", text, flags=re.DOTALL | re.MULTILINE)


def table_rows(text: str) -> list[list[str]]:
    """Every row of every Markdown table, as stripped cells (the separator rows are dropped)."""
    rows = []
    for line in without_fences(text).splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        rows.append(cells)
    return rows


def section(text: str, heading: str) -> str:
    """The body under `heading` (a full heading line), up to the next heading of its level."""
    lines = text.splitlines()
    start = lines.index(heading)
    level = len(heading) - len(heading.lstrip("#"))
    body = []
    fenced = False
    for line in lines[start + 1 :]:
        if line.startswith("```"):
            fenced = not fenced
        match = re.match(r"^(#+) ", line)
        if match and not fenced and len(match.group(1)) <= level:
            break
        body.append(line)
    return "\n".join(body)


def fmt(number: float, digits: int = 3) -> str:
    return f"{number:.{digits}f}"


Expected = str | float | tuple[float, float]


def row_matches(row: list[str], expected: list[Expected]) -> bool:
    """Whether a table row has the expected cells. Numbers match to the third decimal: the saved
    JSON rounds to six decimals and the report rounds the exact value, so a value like 0.8475
    is written 0.847 in the report."""
    if len(row) != len(expected):
        return False
    for cell, want in zip(row, expected, strict=True):
        if isinstance(want, str):
            if cell != want:
                return False
        else:
            wanted = [want] if isinstance(want, float) else list(want)
            found = [float(x) for x in re.findall(r"\d+\.\d+", cell)]
            if len(found) != len(wanted) or any(
                abs(a - b) > 0.00051 for a, b in zip(found, wanted, strict=True)
            ):
                return False
    return True


def has_row(rows: list[list[str]], expected: list[Expected]) -> bool:
    return any(row_matches(row, expected) for row in rows)


def comma(number: float, digits: int = 2) -> str:
    return f"{number:.{digits}f}".replace(".", ",")


# --------------------------------------------------------------------------- the repository tree


def _tree() -> set[str]:
    """Every file and directory of the repository, relative and with `/`, hidden ones left out."""
    found: set[str] = set()
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = [
            d
            for d in dirs
            if (not d.startswith(".") or d == ".github") and d not in {"__pycache__", "var"}
        ]
        base = Path(folder).relative_to(ROOT)
        for name in (*dirs, *files):
            found.add((base / name).as_posix())
    return found


def _suffixes(paths: set[str]) -> set[str]:
    """Every tail of every path: the documents write `agent/domain/signals.py` for a file that
    lives under `src/merchant_assistant/`."""
    tails: set[str] = set()
    for path in paths:
        parts = path.split("/")
        tails.update("/".join(parts[i:]) for i in range(len(parts)))
    return tails


TREE = _tree()
TAILS = _suffixes(TREE)


def _gitignored(path: str) -> bool:
    patterns = [
        line.strip().rstrip("/")
        for line in read(ROOT / ".gitignore").splitlines()
        if line.strip() and not line.startswith(("#", "!"))
    ]
    segments = path.split("/")
    return any(
        fnmatch.fnmatch(path, pattern) or any(fnmatch.fnmatch(s, pattern) for s in segments)
        for pattern in patterns
    )


_PATH_SHAPE = re.compile(r"[A-Za-z0-9_.\-/]+")
_EXTENSIONS = (".py", ".md", ".json", ".jsonl", ".toml", ".yml", ".sqlite", ".db", ".lock")


def _looks_like_path(token: str) -> bool:
    if token in NOT_PATHS or not _PATH_SHAPE.fullmatch(token) or token.startswith("/"):
        return False
    if re.fullmatch(r"[0-9/]+", token):
        return False  # a date such as 03/04
    if token.endswith(_EXTENSIONS):
        return not token.startswith(".") or token.startswith(".github")
    # Without an extension it has to be a lowercase directory path (`design/adr/`), so that
    # `AUTO/SUGGEST` or `y/o` are not taken for files.
    return "/" in token.strip("/") and token == token.lower()


def named_paths(path: Path) -> Iterator[str]:
    """The repository paths a document writes in backticks (outside fenced blocks)."""
    for span in re.findall(r"`([^`\n]+)`", without_fences(read(path))):
        token = span.split("::")[0]
        if _looks_like_path(token):
            yield token


def _exists(token: str) -> bool:
    return token.strip("/") in TAILS


# --------------------------------------------------------------------------- (a) make targets


def _makefile_targets() -> dict[str, bool]:
    """Target name -> whether it has a help line (`target: ... ## text`)."""
    targets = {}
    for line in read(ROOT / "Makefile").splitlines():
        match = re.match(r"^([a-z][a-z-]*):(.*)$", line)
        if match:
            targets[match.group(1)] = "##" in match.group(2)
    return targets


def _mentioned_make_targets() -> set[str]:
    """Every `make <target>` that either README mentions."""
    text = read(README) + read(HARNESS_README)
    return set(re.findall(r"\bmake ([a-z][a-z-]*)", text))


def test_every_make_target_the_readme_mentions_exists() -> None:
    mentioned = _mentioned_make_targets()
    missing = sorted(mentioned - set(_makefile_targets()))

    assert not missing, f"the README mentions `make` targets that the Makefile lacks: {missing}"


def test_every_documented_make_target_is_in_the_readme() -> None:
    mentioned = _mentioned_make_targets()
    documented = {name for name, has_help in _makefile_targets().items() if has_help}

    assert not documented - mentioned, (
        f"targets of the Makefile that the README never mentions: {sorted(documented - mentioned)}"
    )


# --------------------------------------------------------------------------- (b) paths and links


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda p: p.name)
def test_every_path_in_backticks_exists(document: Path) -> None:
    missing = sorted(
        {
            token
            for token in named_paths(document)
            if token not in CREATED_BY_HAND and not _gitignored(token) and not _exists(token)
        }
    )

    assert not missing, f"{document.name} names paths that are not in the repository: {missing}"


_FUNCTION_REFERENCE = re.compile(r"([A-Za-z0-9_./\-]+\.py)::(\w+)(\*?)")


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda p: p.name)
def test_every_test_function_the_document_cites_exists(document: Path) -> None:
    missing = []
    for file_name, name, star in _FUNCTION_REFERENCE.findall(read(document)):
        candidates = [ROOT / tail for tail in TREE if tail.endswith(f"/{file_name}")]
        candidates += [ROOT / file_name] if (ROOT / file_name).exists() else []
        pattern = rf"def {re.escape(name)}\w*\(" if star else rf"def {re.escape(name)}\("
        if not any(re.search(pattern, read(path)) for path in candidates if path.is_file()):
            missing.append(f"{file_name}::{name}{star}")

    assert not missing, f"{document.name} cites tests that do not exist: {sorted(set(missing))}"


def _slugs(markdown: str) -> set[str]:
    """The anchors GitHub gives to the headings of a document."""
    seen: dict[str, int] = {}
    anchors = set()
    for line in without_fences(markdown).splitlines():
        match = re.match(r"^#{1,6}\s+(.*)$", line)
        if not match:
            continue
        slug = re.sub(r"[^\w\- ]", "", match.group(1).replace("`", "").lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda p: p.name)
def test_every_relative_link_and_anchor_resolves(document: Path) -> None:
    broken = []
    for target in re.findall(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)", without_fences(read(document))):
        if re.match(r"^[a-z]+:", target):
            continue  # a URL
        file_part, _, anchor = target.partition("#")
        destination = (document.parent / file_part).resolve() if file_part else document
        missing_anchor = (
            bool(anchor)
            and destination.suffix == ".md"
            and destination.exists()
            and anchor not in _slugs(read(destination))
        )
        if not destination.exists() or missing_anchor:
            broken.append(target)

    assert not broken, f"{document.name} has links that do not resolve: {broken}"


def test_the_readme_does_not_repeat_what_was_replaced() -> None:
    stale = {
        "MIN_BM25_SCORE": "the single BM25 floor became the two GATE_* thresholds (ADR-0007)",
        "test_agent_retrieval_floor": "replaced by test_agent_no_data_gate.py",
        "nunca se corrió contra la API real": "the live runs exist",
        "todavía no existe": "the live runs exist",
    }
    for document in (README, SPEC_REVIEW):
        text = read(document)
        found = {phrase: why for phrase, why in stale.items() if phrase in text}

        assert not found, f"{document.name} still says things that stopped being true: {found}"


# --------------------------------------------------------------------------- (c) conversations


@dataclass(frozen=True)
class Counts:
    runs_ok: int
    runs_valid: int
    lock_ok: int
    lock_total: int
    utility_ok: int
    utility_total: int
    wilson: tuple[float, float]
    spent: float

    @property
    def runs(self) -> str:
        return f"{self.runs_ok}/{self.runs_valid} ({round(100 * self.runs_ok / self.runs_valid)}%)"

    @property
    def interval(self) -> str:
        return f"[{comma(self.wilson[0])}; {comma(self.wilson[1])}]"

    @property
    def lock(self) -> str:
        return f"{self.lock_ok}/{self.lock_total}"

    @property
    def utility(self) -> str:
        return f"{self.utility_ok}/{self.utility_total}"


def counts(mode: str) -> Counts:
    """The numbers of a saved run, computed from its runs and not from its own summary."""
    data = json.loads(read(CONVERSATIONS / mode / "results.json"))
    valid = [run for run in data["runs"] if run["valid"]]
    checks = [(c["kind"], c["passed"]) for run in valid for c in run["checks"]]
    lock = [passed for kind, passed in checks if kind == "lock"]
    utility = [passed for kind, passed in checks if kind == "utility"]
    low, high = data["summary"]["overall"]["compliance"]["wilson95"]
    return Counts(
        runs_ok=sum(1 for run in valid if run["passed"]),
        runs_valid=len(valid),
        lock_ok=sum(lock),
        lock_total=len(lock),
        utility_ok=sum(utility),
        utility_total=len(utility),
        wilson=(low, high),
        spent=float(data["provenance"].get("spent_usd") or 0),
    )


def headline() -> str:
    text = read(README)
    return section(text, "## Resultados en una pantalla")


@pytest.mark.parametrize("mode", MODES)
def test_the_results_table_matches_the_saved_runs(mode: str) -> None:
    expected = counts(mode)
    cost = f"USD {comma(expected.spent, 3)}" if mode in FINAL_LIVE else "—"
    wanted = [f"`{mode}`", expected.runs, expected.interval, expected.lock, expected.utility, cost]

    rows = [row for row in table_rows(headline()) if row and row[0] == f"`{mode}`"]

    assert wanted in rows, f"README row for {mode} should be {wanted}, found {rows}"


def test_the_headline_sums_of_the_real_runs_match_the_saved_runs() -> None:
    final = [counts(mode) for mode in FINAL_LIVE]
    conversations = sum(c.runs_valid for c in final)
    lock = f"{sum(c.lock_ok for c in final)}/{sum(c.lock_total for c in final)}"
    spent_final = comma(sum(c.spent for c in final), 3)
    spent_all = comma(sum(counts(mode).spent for mode in ALL_LIVE), 2)
    text = headline()

    for needle in (f"{conversations} conversaciones", lock, f"USD {spent_final}"):
        assert needle in text, f"the headline should say {needle!r}"
    assert f"USD {spent_all}" in read(README), (
        f"the README should say the eight real runs cost USD {spent_all}"
    )


@pytest.mark.parametrize("mode", ALL_LIVE)
def test_the_history_of_real_runs_matches_the_saved_runs(mode: str) -> None:
    data = json.loads(read(CONVERSATIONS / mode / "results.json"))
    expected = counts(mode)
    scenarios = len({run["scenario"] for run in data["runs"]})
    prompt = data["provenance"]["prompt_sha256"][:16]
    cost = f"USD {comma(expected.spent, 3)}"
    wanted = [
        f"`{mode}`",
        f"`{prompt}`",
        str(scenarios),
        expected.runs,
        expected.lock,
        expected.utility,
        cost,
    ]

    assert wanted in table_rows(read(README)), f"the history table should have the row {wanted}"


def test_every_live_run_kept_all_its_locks() -> None:
    # The sentence "locks held in every real-model run" is only true if the data says so.
    for mode in ALL_LIVE:
        saved = counts(mode)
        assert saved.lock_ok == saved.lock_total, f"{mode} has a failed LOCK check"


# --------------------------------------------------------------------------- (f) the catalog


def test_the_scenario_counts_match_the_catalog() -> None:
    total = len(SCENARIOS)
    dev = sum(1 for s in SCENARIOS if s.split == "dev")
    held = sum(1 for s in SCENARIOS if s.split == "heldout")
    sentence = f"{total} escenarios ({dev} `dev` y {held} `heldout`)"

    assert sentence in read(README), f"the README should say {sentence!r}"


def test_the_scenarios_per_category_match_the_catalog() -> None:
    rows = {row[0]: row[1:] for row in table_rows(read(README)) if len(row) == 4}
    for category, name in CATEGORY_NAMES.items():
        dev = sum(1 for s in SCENARIOS if s.category == category and s.split == "dev")
        held = sum(1 for s in SCENARIOS if s.category == category and s.split == "heldout")
        wanted = [str(dev), str(held), str(dev + held)]

        assert rows.get(name) == wanted, f"{name}: the README says {rows.get(name)}, not {wanted}"


# --------------------------------------------------------------------------- (d) retrieval


def _rows(key: str) -> list[dict[str, Any]]:
    loaded: dict[str, Any] = json.loads(read(RETRIEVAL / "metrics.json"))
    rows: list[dict[str, Any]] = loaded[key]
    return rows


def test_the_main_retrieval_table_matches_metrics_json() -> None:
    rows = table_rows(read(README))
    for entry in _rows("main"):
        wanted: list[Expected] = [
            entry["strategy"],
            entry["recall_at_3"],
            tuple(entry["recall_at_3_ci95"]),
            entry["hit_at_3"],
            entry["mrr"],
            tuple(entry["mrr_ci95"]),
        ]

        assert has_row(rows, wanted), f"the main table should have the row {wanted}"


def test_the_headline_retrieval_table_matches_metrics_json() -> None:
    rows = table_rows(headline())
    for entry in _rows("main"):
        wanted: list[Expected] = [entry["strategy"], entry["recall_at_3"], entry["mrr"]]

        assert has_row(rows, wanted), f"the headline table should have the row {wanted}"


def test_the_chunking_grid_matches_metrics_json() -> None:
    by_chunking: dict[str, dict[str, dict[str, Any]]] = {}
    for entry in _rows("grid"):
        by_chunking.setdefault(entry["chunking"], {})[entry["strategy"]] = entry
    rows = table_rows(read(README))
    for label, strategies in by_chunking.items():
        wanted: list[Expected] = [
            f"`{label}`",
            str(strategies["vector"]["chunks"]),
            strategies["vector"]["mrr"],
            strategies["bm25"]["mrr"],
            strategies["hybrid"]["mrr"],
            strategies["hybrid"]["recall_at_3"],
        ]

        assert has_row(rows, wanted), f"the chunking table should have the row {wanted}"


def test_the_paired_differences_match_metrics_json() -> None:
    text = read(README)
    for entry in _rows("paired_differences"):
        mean = f"{entry['mean_difference']:+.3f}"
        low, high = entry["ci95"]
        interval = f"[{fmt(low)}, {fmt(high)}]"

        assert f"{mean} {interval}" in text, f"the README should say {mean} {interval}"


def _tie_break() -> list[dict[str, Any]]:
    loaded: dict[str, Any] = json.loads(read(RETRIEVAL / "metrics.json"))
    strategies: list[dict[str, Any]] = loaded["tie_break"]["strategies"]
    return strategies


def _questions_text(found: list[str]) -> str:
    return ", ".join(found) or "—"


def test_the_tie_break_table_matches_metrics_json() -> None:
    rows = table_rows(read(README))
    for entry in _tie_break():
        wanted: list[Expected] = [
            entry["strategy"],
            entry["default"]["mrr"],
            entry["reversed"]["mrr"],
            entry["expected"]["mrr"],
            entry["worst_case_mrr"],
            entry["best_case_mrr"],
        ]

        assert has_row(rows, wanted), f"the tie-break table should have the row {wanted}"


def test_the_tie_break_questions_match_metrics_json() -> None:
    rows = table_rows(read(README))
    for entry in _tie_break():
        wanted: list[Expected] = [
            entry["strategy"],
            _questions_text(entry["first_two_order_depends"]),
            _questions_text(entry["outcome_depends"]),
        ]

        assert has_row(rows, wanted), f"the tie-break questions table should have the row {wanted}"


def test_the_tie_break_prose_matches_metrics_json() -> None:
    text = read(README)
    metrics = json.loads(read(RETRIEVAL / "metrics.json"))
    hybrid, policy = (_tie_break()[0], _tie_break()[1])
    assert (hybrid["strategy"], policy["strategy"]) == ("hybrid", "hybrid+meta(demote)")
    tied = hybrid["questions_with_exact_tie"]
    bm25 = next(e["mrr"] for e in metrics["main"] if e["strategy"] == "bm25")
    values = (hybrid["default"]["mrr"], hybrid["reversed"]["mrr"], hybrid["expected"]["mrr"])
    gains = [policy[k]["mrr"] - hybrid[k]["mrr"] for k in ("default", "reversed", "expected")]
    needles = [
        f"En {len(tied)} de {len(metrics['per_question'])} preguntas "
        f"({', '.join(tied[:-1])} y {tied[-1]})",
        *(f"{value - bm25:+.3f} " for value in values),
        *(f"{gain:+.3f} " for gain in gains),
        f"{fmt(hybrid['reversed']['mrr'])} (invertido) a {fmt(hybrid['default']['mrr'])}",
        f"con {fmt(hybrid['expected']['mrr'])} esperado",
        f"de {fmt(hybrid['worst_case_mrr'])} a {fmt(hybrid['best_case_mrr'])}",
        f"va de {fmt(policy['worst_case_mrr'])} a {fmt(policy['best_case_mrr'])}",
    ]

    missing = [needle for needle in needles if needle not in text]
    assert not missing, f"the README should say {missing} about the RRF tie-break"
    assert hybrid["worst_case_mrr"] < bm25, "the README says the worst order is below BM25"
    assert hybrid["default"]["mrr"] == max(values), "the README says the default is the best"
    assert min(values) == hybrid["reversed"]["mrr"], "the README says the reversed is the lowest"


def test_the_headline_quotes_the_hybrid_mrr_as_a_range() -> None:
    hybrid = _tie_break()[0]
    text = headline()

    for needle in (
        f"({fmt(hybrid['default']['mrr'])})",
        f"baja a {fmt(hybrid['reversed']['mrr'])}",
        f"esperado es {fmt(hybrid['expected']['mrr'])}",
    ):
        assert needle in text, f"the headline should say {needle!r} about the tie-break"


def summary() -> str:
    return section(read(README), "## Resumen")


def _checks_that_passed_without_being_exercised() -> int:
    """LOCK checks of the final real runs that measured nothing: no action existed to drop."""
    total = 0
    for mode in FINAL_LIVE:
        data = json.loads(read(CONVERSATIONS / mode / "results.json"))
        for run in data["runs"]:
            for check in run["checks"]:
                if check["name"] == "dropped_action_reaches_inbox" and check["detail"].startswith(
                    "0 dropped, 0 inbox"
                ):
                    total += 1
    return total


def test_the_summary_quotes_the_saved_results() -> None:
    live, held = counts("live"), counts("live-heldout")
    hybrid = _tie_break()[0]
    main_hybrid = next(e for e in _rows("main") if e["strategy"] == "hybrid")
    lock = f"{live.lock_ok + held.lock_ok}/{live.lock_total + held.lock_total}"
    unexercised = _checks_that_passed_without_being_exercised()
    text = summary()

    needles = [
        f"hybrid recall@3 {fmt(main_hybrid['recall_at_3'])}",
        f"MRR {fmt(hybrid['reversed']['mrr'])}\u2013{fmt(hybrid['default']['mrr'])} "
        f"según el desempate de RRF ({fmt(hybrid['expected']['mrr'])} esperado)",
        f"`live` {live.runs_ok}/{live.runs_valid} ({round(100 * live.runs_ok / live.runs_valid)}%",
        f"`live-heldout` {held.runs_ok}/{held.runs_valid} "
        f"({round(100 * held.runs_ok / held.runs_valid)}%",
        f"LOCK {lock} en las {live.runs_valid + held.runs_valid} conversaciones",
        f"{unexercised} de esos checks pasan sin ejercerse",
        '"0 dropped, 0 inbox note(s)"',
    ]

    missing = [needle for needle in needles if needle not in text]
    assert not missing, f"the Resumen should say {missing}"
    assert unexercised > 0, "the Resumen warns about checks that passed without being exercised"


def test_the_readme_counts_its_partial_rows() -> None:
    rows = table_rows(section(read(README), "## Matriz de cumplimiento de la consigna"))
    states = [row[1] for row in rows if len(row) == 4 and row[1] in {"✅", "🟨", "⬜"}]
    sentence = f"con {states.count('🟨')} de las {len(states)} filas de la [matriz de cumplimiento]"

    assert sentence in read(README), f"the Estado should say {sentence!r}"


# --------------------------------------------------------------------------- the gate


def test_the_gate_thresholds_in_the_readme_are_the_ones_in_the_code() -> None:
    text = read(README)
    thresholds = (
        bootstrap.GATE_BM25_HYBRID,
        bootstrap.GATE_COSINE_HYBRID,
        bootstrap.GATE_BM25_DEGRADED,
    )
    for value in thresholds:
        assert str(value) in text, f"the README should quote the gate threshold {value}"


def test_the_gate_confusion_counts_match_the_calibration() -> None:
    data = json.loads(read(RETRIEVAL / "gate_calibration.json"))["tokenizers"]["default"]
    hybrid, degraded = data["hybrid_confusion"], data["degraded_confusion"]
    off_topic = hybrid["off_topic_total"]
    wanted = [
        f"{hybrid['off_topic_rejected']} de {off_topic}",
        f"{off_topic - hybrid['off_topic_rejected']} de {off_topic}",
        f"{hybrid['hard_total'] - hybrid['hard_rejected']} de {hybrid['hard_total']}",
        f"{degraded['off_topic_rejected']} de {degraded['off_topic_total']}",
    ]
    text = read(README)

    for needle in wanted:
        assert needle in text, f"the README should say {needle!r} about the gate"


# --------------------------------------------------------------------------- (e) configuration


def template_lines() -> list[str]:
    """The lines of the first `dotenv` block of the Configuración section: the template."""
    body = section(read(README), "### Configuración")
    block = re.search(r"^```dotenv\n(.*?)^```", body, flags=re.DOTALL | re.MULTILINE)
    assert block, "the Configuración section should contain a ```dotenv block"
    return block.group(1).splitlines()


_ASSIGNMENT = re.compile(r"^(#\s?)?([A-Z][A-Z0-9_]*)=(.*)$")


def template_variables() -> dict[str, tuple[bool, str]]:
    """Variable -> (is commented out, the value written before any trailing comment)."""
    found = {}
    for line in template_lines():
        match = _ASSIGNMENT.match(line)
        if match:
            value = match.group(3).split("  #")[0].strip()
            found[match.group(2)] = (match.group(1) is not None, value)
    return found


def test_the_template_covers_exactly_the_settings_the_code_reads() -> None:
    fields = {name.upper() for name in Settings.model_fields}
    covered = set(template_variables())

    expected = fields - set(OMITTED_FROM_TEMPLATE)

    assert covered == expected, (
        f"in the template and not in Settings: {sorted(covered - fields)}; "
        f"in Settings and not in the template: {sorted(expected - covered)}"
    )


def test_the_readme_says_which_variables_the_template_leaves_out() -> None:
    prose = read(README).replace("\n".join(template_lines()), "")
    for name in OMITTED_FROM_TEMPLATE:
        assert name in prose, f"the README should explain why the template omits {name}"


def test_the_defaults_in_the_template_are_the_defaults_of_the_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    for variable, (commented, value) in template_variables().items():
        field = variable.lower()
        default = Settings.model_fields[field].default
        parsed = getattr(Settings(_env_file=None, **{field: value}), field)  # type: ignore[arg-type]

        if commented:
            assert parsed == default, (
                f"{variable}: the template says {value!r}, the code {default!r}"
            )
        else:
            assert value == "", f"{variable} is active in the template, so it should be empty"
            assert default is None, f"{variable} is empty in the template but has a default"


def test_the_template_asks_for_no_secret() -> None:
    secrets = {"OPENAI_API_KEY", "LLM_API_KEY", "OPERATOR_PIN", "SUPERVISOR_PIN"}
    variables = template_variables()

    assert all(variables[name] == (False, "") for name in secrets), "secrets must stay empty"


def test_env_example_is_the_readme_template() -> None:
    path = ROOT / ".env.example"
    if not path.exists():
        pytest.skip("create .env.example from the README template (see the Configuración section)")

    assert read(path).strip() == "\n".join(template_lines()).strip()
