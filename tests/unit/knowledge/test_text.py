import pytest

from merchant_assistant.knowledge.text import SPANISH_STOPWORDS, Tokenizer, normalize


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


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("ZT-PRO-2", ["zt-pro-2"]),
        ("$10.000", ["10.000"]),
        ("plan PRO", ["plan", "pro"]),  # a standalone word is not a compound: untouched
        ("REC-000123 y ZT-START-1", ["rec-000123", "y", "zt-start-1"]),
        ("", []),
    ],
)
def test_without_parts_only_the_compound_is_emitted(text: str, tokens: list[str]) -> None:
    assert Tokenizer(emit_parts=False)(text) == tokens


def test_the_query_word_pro_no_longer_matches_a_compound_when_parts_are_off() -> None:
    assert "pro" not in Tokenizer(emit_parts=False)("ZT-PRO-2")
    assert "zt-pro-2" in Tokenizer(emit_parts=False)("el plan ZT-PRO-2")


def test_parts_are_emitted_by_default() -> None:
    assert Tokenizer().emit_parts is True
    assert Tokenizer()("ZT-PRO-2") == ["zt-pro-2", "zt", "pro", "2"]


def test_stopwords_are_off_by_default() -> None:
    assert Tokenizer().stopwords is False


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        (
            "¿Qué estados devuelve la validación de un QR?",
            ["estados", "devuelve", "validacion", "qr"],
        ),
        ("¿Cuánto cobran de comisión?", ["cobran", "comision"]),
        ("¿Cómo transfiero una entrada?", ["transfiero", "entrada"]),
        ("Quiero hacer un reclamo.", ["quiero", "hacer", "reclamo"]),
        ("No me liquidaron un evento", ["no", "liquidaron", "evento"]),
        ("¿Dónde está mi entrada?", ["entrada"]),
        ("hay un cobro de más", ["cobro", "mas"]),
        ("el de la", []),
    ],
)
def test_stopwords_drop_articles_prepositions_pronouns_and_interrogatives(
    text: str, tokens: list[str]
) -> None:
    assert Tokenizer(stopwords=True)(text) == tokens


@pytest.mark.parametrize(
    "word",
    ["no", "sin", "mas", "menos", "solo", "si", "pero", "hasta", "desde", "antes", "despues",
     "durante", "entre", "ni", "tampoco", "nunca", "todo", "cada"],
)  # fmt: skip
def test_words_that_can_carry_the_meaning_of_a_policy_survive(word: str) -> None:
    assert Tokenizer(stopwords=True)(word) == [word]
    assert word not in SPANISH_STOPWORDS


def test_numbers_and_codes_survive_the_stopword_list() -> None:
    assert Tokenizer(stopwords=True)("el 10 de ZT-PRO-2 y 10.000") == [
        "10",
        "zt-pro-2",
        "zt",
        "pro",
        "2",
        "10.000",
        "10",
        "000",
    ]


def test_the_parts_of_a_compound_are_never_dropped_as_stopwords() -> None:
    assert Tokenizer(stopwords=True)("fin-de-mes") == ["fin-de-mes", "fin", "de", "mes"]
    assert Tokenizer(stopwords=True)("ZT-PRO-2") == Tokenizer()("ZT-PRO-2")


def test_the_stopword_list_is_compared_on_normalized_tokens() -> None:
    assert Tokenizer(stopwords=True)("QUÉ Cuál CÓMO") == []
    assert all(word == normalize(word) for word in SPANISH_STOPWORDS)


def test_stopwords_and_stemming_combine_without_stemming_a_dropped_word() -> None:
    assert Tokenizer(stopwords=True, stem=True)("las entradas de los eventos") == [
        "entrada",
        "evento",
    ]
