import pytest

from merchant_assistant.knowledge.text import Tokenizer, normalize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("revisión", "revision"),
        ("REVISIÓN", "revision"),
        ("año", "ano"),
        ("Ñandú", "nandu"),
        ("Pingüino", "pinguino"),
        ("ZT-PRO-2", "zt-pro-2"),
        ("Straße", "strasse"),
        ("ｆｕｌｌ", "full"),  # noqa: RUF001 - fullwidth letters are the point
        ("", ""),
    ],
)
def test_normalization_folds_accents_and_case(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


def test_an_accented_word_written_precomposed_or_decomposed_normalizes_the_same() -> None:
    assert normalize("revisión") == normalize("revisión") == "revision"


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("ZT-PRO-2", ["zt-pro-2", "zt", "pro", "2"]),
        ("$10.000", ["10.000", "10", "000"]),
        ("6,0%", ["6,0", "6", "0"]),
        ("REC-000123", ["rec-000123", "rec", "000123"]),
        ("tarifas.", ["tarifas"]),
        ("¿Cuánto demora?", ["cuanto", "demora"]),
        ("Revisión del año", ["revision", "del", "ano"]),
        ("", []),
        ("  ...  ", []),
    ],
)
def test_the_tokenizer_emits_the_compound_first_and_then_its_parts(
    text: str, tokens: list[str]
) -> None:
    assert Tokenizer()(text) == tokens


def test_punctuation_at_the_edges_of_a_word_never_joins_it() -> None:
    assert Tokenizer()("(tarifas), -pro- y 3.") == ["tarifas", "pro", "y", "3"]


def test_a_separator_only_joins_when_a_letter_or_digit_follows_it() -> None:
    assert Tokenizer()("fin.Inicio") == ["fin.inicio", "fin", "inicio"]
    assert Tokenizer()("fin. Inicio") == ["fin", "inicio"]


def test_repeated_tokens_are_kept_because_term_frequency_needs_them() -> None:
    assert Tokenizer()("pago pago Pago") == ["pago", "pago", "pago"]


def test_common_words_are_not_removed() -> None:
    assert Tokenizer()("el de la") == ["el", "de", "la"]


def test_the_query_word_pro_matches_a_token_of_the_plan_code() -> None:
    assert "pro" in Tokenizer()("ZT-PRO-2")
    assert "pro" in Tokenizer()("plan PRO")


def test_a_word_with_apostrophes_or_symbols_is_split_around_them() -> None:
    assert Tokenizer()("a&b c_d") == ["a", "b", "c", "d"]


@pytest.mark.parametrize(
    ("word", "stem"),
    [
        ("entradas", "entrada"),  # plural in -s
        ("tarifas", "tarifa"),
        ("dias", "dia"),
        ("paises", "pais"),  # longer than 4 and ending in -es: drops "es"
        ("reclamos", "reclamo"),
        ("clases", "clas"),  # the rule is crude on purpose: "clase" itself is left alone
        ("clase", "clase"),
        ("acceso", "acceso"),  # no plural ending
        ("pass", "pass"),  # "ss" is not a plural
        ("mes", "mes"),  # too short to touch
        ("mas", "mas"),
        ("es", "es"),
        ("tres", "tre"),  # len 4 does not reach the "es" rule, but ends in a lone "s"
    ],
)
def test_the_light_stemmer_folds_plurals_of_purely_alphabetic_tokens(word: str, stem: str) -> None:
    assert Tokenizer(stem=True)(word) == [stem]


def test_the_stemmer_never_touches_codes_numbers_or_compounds() -> None:
    tokenizer = Tokenizer(stem=True)

    assert tokenizer("ZT-START-1") == ["zt-start-1", "zt", "start", "1"]
    assert tokenizer("REC-000123") == ["rec-000123", "rec", "000123"]
    assert tokenizer("10.000 400s 3,0") == ["10.000", "10", "000", "400s", "3,0", "3", "0"]


def test_the_stemmer_does_stem_the_alphabetic_parts_of_a_compound() -> None:
    assert Tokenizer(stem=True)("pre-ventas") == ["pre-ventas", "pre", "venta"]


def test_without_stemming_the_words_are_left_alone() -> None:
    assert Tokenizer(stem=False)("entradas tarifas paises") == ["entradas", "tarifas", "paises"]
    assert Tokenizer()("entradas") == ["entradas"]


def test_stemming_is_off_by_default() -> None:
    assert Tokenizer().stem is False
