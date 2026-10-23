"""Documents of the corpus and the strict parser of their on-disk format.

The format is a small, fixed subset of Markdown with a front matter block. It is parsed by
hand and strictly (no YAML) because the metadata decides which document may answer a
question: a misspelled key or a swapped date must stop the load, not silently weaken the
validity policy. The text of a document is data: nothing in it is ever interpreted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from merchant_assistant.knowledge.errors import DocumentFormatError

_FRONT_MATTER_FENCE = "---"
_HEADING = re.compile(r"# (.+)")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

# The keys are the corpus' own (Spanish); the dataclass fields are English.
_KEYS = ("id", "titulo", "tipo", "vigencia_desde", "vigencia_hasta")


@dataclass(frozen=True, slots=True)
class Section:
    heading: str
    body: str

    @property
    def text(self) -> str:
        # A preamble has no heading and a heading may have no body: joining only what is
        # present keeps stray newlines out of the text that gets tokenized and embedded.
        return "\n".join(part for part in (self.heading, self.body) if part)


@dataclass(frozen=True, slots=True)
class Document:
    id: str
    title: str
    kind: str
    valid_from: date
    valid_until: date
    sections: tuple[Section, ...]

    @property
    def text(self) -> str:
        """Sections flattened without `#` markers, so the marker is not indexed as content."""
        return "\n\n".join(section.text for section in self.sections)


def parse_document(raw: str) -> Document:
    # `split("\n")` on purpose: `str.splitlines` also breaks on U+2028, form feeds and other
    # separators that may legitimately appear inside a paragraph.
    lines = [line.rstrip("\r") for line in raw.split("\n")]
    fields, body_start = _parse_front_matter(lines)
    valid_from = _parse_date(fields["vigencia_desde"], "vigencia_desde")
    valid_until = _parse_date(fields["vigencia_hasta"], "vigencia_hasta")
    if valid_from > valid_until:
        raise DocumentFormatError(
            f"vigencia_desde {valid_from} is after vigencia_hasta {valid_until}"
        )
    sections = _parse_sections(lines, body_start)
    if not sections:
        raise DocumentFormatError("the document has no content after the front matter")
    return Document(
        id=fields["id"],
        title=fields["titulo"],
        kind=fields["tipo"],
        valid_from=valid_from,
        valid_until=valid_until,
        sections=sections,
    )


def _parse_front_matter(lines: list[str]) -> tuple[dict[str, str], int]:
    """Return the raw values by key and the index of the first line after the block."""
    if not lines or lines[0].rstrip() != _FRONT_MATTER_FENCE:
        raise DocumentFormatError("line 1: the document must start with a '---' front matter")
    fields: dict[str, str] = {}
    for number, line in enumerate(lines[1:], start=2):
        if line.rstrip() == _FRONT_MATTER_FENCE:
            missing = [key for key in _KEYS if key not in fields]
            if missing:
                raise DocumentFormatError(f"line {number}: missing front matter keys {missing}")
            return fields, number
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        key = key.strip()
        if not separator:
            raise DocumentFormatError(f"line {number}: expected 'key: value'")
        if key not in _KEYS:
            raise DocumentFormatError(f"line {number}: unknown front matter key '{key}'")
        if key in fields:
            raise DocumentFormatError(f"line {number}: duplicate front matter key '{key}'")
        value = _unquote(value.strip())
        if not value:
            raise DocumentFormatError(f"line {number}: empty value for '{key}'")
        fields[key] = value
    raise DocumentFormatError("the front matter is never closed with '---'")


def _unquote(value: str) -> str:
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1].strip()
    return value


def _parse_date(value: str, key: str) -> date:
    # `date.fromisoformat` alone accepts "20260101" and week dates on 3.11+; the corpus
    # writes YYYY-MM-DD and anything else is more likely a typo than an intention.
    if not _ISO_DATE.fullmatch(value):
        raise DocumentFormatError(f"{key}: '{value}' is not a YYYY-MM-DD date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise DocumentFormatError(f"{key}: '{value}' is not a real calendar date") from None


def _parse_sections(lines: list[str], start: int) -> tuple[Section, ...]:
    sections: list[Section] = []
    heading = ""
    body: list[str] = []

    def close() -> None:
        text = "\n".join(_trim_blank_lines(body))
        # A heading with an empty body is a legitimate section; a blank preamble is not.
        if heading or text:
            sections.append(Section(heading=heading, body=text))

    for line in lines[start:]:
        match = _HEADING.fullmatch(line.rstrip())
        if match is None:
            body.append(line)
            continue
        close()
        heading, body = match.group(1).strip(), []
    close()
    return tuple(sections)


def _trim_blank_lines(lines: list[str]) -> list[str]:
    first = 0
    last = len(lines)
    while first < last and not lines[first].strip():
        first += 1
    while last > first and not lines[last - 1].strip():
        last -= 1
    return lines[first:last]
