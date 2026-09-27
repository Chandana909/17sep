"""Integration tooling for real extracts: profile files and draft a mapping.

`profile_directory` describes what is in each file (columns, fill rate, distinct values,
samples, inferred types, timestamp formats). `draft_mapping` proposes a mapping TOML from the
headers using `config/mapping.synonyms.toml`. Every guess is marked `# REVIEW` and every gap
`# TODO`, so a person or a coding model reviews it, runs `asas data check`, and iterates.
"""

from __future__ import annotations

import difflib
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from asas.data.contract import (
    DECIMAL_FIELDS,
    ENTITIES,
    ENTITY_FIELDS,
    ENUM_VALUES,
    FIELD_HELP,
    PERSON_FIELDS,
    REQUIRED_FIELDS,
    TIMESTAMP_FIELDS,
    WORKFLOW_FIELDS,
)
from asas.data.readers import EXTENSIONS, read_rows

TIMESTAMP_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%Y%m%d %H:%M:%S",
    "%Y%m%d%H%M%S",
    "%d-%b-%Y %H:%M:%S",
    "%d.%m.%Y %H:%M:%S",
    "%Y-%m-%d",
    "%d/%m/%Y",
)
MAX_DISTINCT = 1000
_SEP = re.compile(r"[^A-Z0-9]+")


def normalise(name: str) -> str:
    return _SEP.sub("_", name.strip().upper()).strip("_")


# ---------------------------------------------------------------- profiling


@dataclass
class ColumnProfile:
    name: str
    filled: int = 0
    distinct: set[str] = field(default_factory=set)
    samples: list[str] = field(default_factory=list)
    kinds: set[str] = field(default_factory=set)

    def to_dict(self, rows: int) -> dict[str, object]:
        return {
            "fill_rate": f"{(Decimal(self.filled) / Decimal(rows) if rows else Decimal(0)):.3f}",
            "distinct": len(self.distinct) if len(self.distinct) < MAX_DISTINCT else "1000+",
            "samples": self.samples,
            "types": sorted(self.kinds),
        }


def _kind(value: object) -> str:
    if isinstance(value, datetime):
        return "timestamp" if value.tzinfo else "naive_timestamp"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float | Decimal):
        return "number"
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text)
        return "timestamp" if parsed.tzinfo else "naive_timestamp"
    except ValueError:
        pass
    try:
        Decimal(text.replace(",", ""))
        return "number"
    except InvalidOperation:
        pass
    if guess_timestamp_format([text]):
        return "formatted_timestamp"
    return "text"


@dataclass
class FileProfile:
    path: str
    rows: int
    columns: dict[str, ColumnProfile]
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "rows": self.rows,
            "error": self.error,
            "columns": {k: v.to_dict(self.rows) for k, v in self.columns.items()},
        }


def profile_file(path: Path, reader: str | None = None) -> FileProfile:
    try:
        rows = read_rows(path, reader)
    except Exception as exc:  # a broken file is reported, not raised
        return FileProfile(str(path), 0, {}, f"{type(exc).__name__}: {exc}")
    columns: dict[str, ColumnProfile] = {}
    for row in rows:
        for name, value in row.items():
            col = columns.setdefault(name, ColumnProfile(name))
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            col.filled += 1
            text = str(value).strip()
            if len(col.distinct) < MAX_DISTINCT:
                col.distinct.add(text)
            if len(col.samples) < 5 and text not in col.samples:
                col.samples.append(text)
            if len(col.kinds) < 4:
                col.kinds.add(_kind(value))
    return FileProfile(str(path), len(rows), columns)


def profile_directory(data_dir: str | Path) -> list[FileProfile]:
    root = Path(data_dir)
    files = sorted(p for p in root.iterdir() if p.suffix.lower() in EXTENSIONS)
    return [profile_file(p) for p in files]


def guess_timestamp_format(samples: Sequence[str]) -> str | None:
    """The first known format that parses every sample (day-first preferred if ambiguous)."""
    texts = [s.strip() for s in samples if s and s.strip()]
    if not texts:
        return None
    for fmt in TIMESTAMP_FORMATS:
        try:
            for t in texts:
                datetime.strptime(t, fmt)
        except ValueError:
            continue
        return fmt
    return None


# ---------------------------------------------------------------- draft mapping


@dataclass(frozen=True)
class Synonyms:
    entities: Mapping[str, tuple[str, ...]]
    fields: Mapping[str, tuple[str, ...]]
    values: Mapping[str, Mapping[str, tuple[str, ...]]]


def default_synonyms_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "mapping.synonyms.toml"


def load_synonyms(path: str | Path | None = None) -> Synonyms:
    with open(path or default_synonyms_path(), "rb") as fh:
        data: dict[str, Any] = tomllib.load(fh)
    return Synonyms(
        entities={k: tuple(v) for k, v in data.get("entities", {}).items()},
        fields={k: tuple(normalise(s) for s in v) for k, v in data.get("fields", {}).items()},
        values={
            f: {target: tuple(normalise(s) for s in codes) for target, codes in m.items()}
            for f, m in data.get("values", {}).items()
        },
    )


def guess_entity(profile: FileProfile, synonyms: Synonyms) -> str | None:
    stem = normalise(Path(profile.path).stem).lower()
    for entity, words in synonyms.entities.items():
        if any(w in stem for w in words):
            return entity
    headers = {normalise(c) for c in profile.columns}
    best, score = None, 0
    for entity in ENTITIES:
        hits = sum(1 for f in ENTITY_FIELDS[entity] if headers & set(synonyms.fields.get(f, (f,))))
        if hits > score:
            best, score = entity, hits
    return best


def _match(column: str, entity: str, synonyms: Synonyms, taken: set[str]) -> tuple[str, bool]:
    """(contract field, exact?) or ("", False)."""
    key = normalise(column)
    candidates = [f for f in sorted(ENTITY_FIELDS[entity]) if f not in taken]
    for f in candidates:
        if key == f or key in synonyms.fields.get(f, ()):
            return f, True
    names = {s: f for f in candidates for s in (f, *synonyms.fields.get(f, ()))}
    close = difflib.get_close_matches(key, list(names), n=1, cutoff=0.85)
    return (names[close[0]], False) if close else ("", False)


def _translate(code: str, field_name: str, synonyms: Synonyms) -> str | None:
    key = normalise(code)
    allowed = ENUM_VALUES[field_name]
    if key in allowed:
        return key
    for target, codes in synonyms.values.get(field_name, {}).items():
        if key in codes:
            return target
    return None


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _key(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else _toml_str(name)


def draft_entity(entity: str, profile: FileProfile, synonyms: Synonyms) -> list[str]:
    lines = [f"[{entity}]", f"source = {_toml_str(Path(profile.path).name)}"]
    taken: set[str] = set()
    columns: list[str] = []
    ignored: list[str] = []
    mapped: dict[str, str] = {}
    for column in profile.columns:
        target, exact = _match(column, entity, synonyms, taken)
        if not target:
            quarantined = normalise(column)
            if quarantined in (WORKFLOW_FIELDS | PERSON_FIELDS) & ENTITY_FIELDS[entity]:
                target, exact = quarantined, True
        if target:
            taken.add(target)
            mapped[column] = target
            note = "" if exact else "  # REVIEW: fuzzy match"
            if target in WORKFLOW_FIELDS | PERSON_FIELDS:
                note = "  # quarantined (workflow/person): never reaches decisions"
            columns.append(f"{_key(column)} = {_toml_str(target)}{note}")
        else:
            ignored.append(column)

    samples = {target: profile.columns[col].samples for col, target in mapped.items()}
    ts = [s for t, v in samples.items() if t in TIMESTAMP_FIELDS for s in v]
    kinds = {_kind(s) for s in ts}
    fmt = guess_timestamp_format(ts) if ts and kinds <= {"formatted_timestamp", "text"} else None
    naive = "naive_timestamp" in kinds or fmt is not None
    if fmt:
        lines.append(f"timestamp_format = {_toml_str(fmt)}  # REVIEW: guessed from samples")
    if naive:
        lines.append('naive_timestamp_offset = "+00:00"  # REVIEW: timestamps carry no zone')
    decimals = [s for t, v in samples.items() if t in DECIMAL_FIELDS for s in v]
    if any(re.fullmatch(r"-?[\d.]*\d,\d+", s) for s in decimals):
        lines.append("decimal_comma = true  # REVIEW: values look like 1.234,56")
    if ignored:
        lines.append(
            "ignore = ["
            + ", ".join(_toml_str(c) for c in ignored)
            + "]  # REVIEW: unmatched columns; map any that matter"
        )
    missing = sorted(REQUIRED_FIELDS[entity] - set(mapped.values()))
    for f in missing:
        lines.append(f"# TODO: required {f} ({FIELD_HELP.get(f, '')}) not found: map a column")
        lines.append(f'#       or add [{entity}.constants] {f} = "..."')
    lines += ["", f"[{entity}.columns]", *columns]

    for col, target in mapped.items():
        if target not in ENUM_VALUES:
            continue
        codes = sorted(profile.columns[col].distinct)[:50]
        translations = []
        for code in codes:
            value = _translate(code, target, synonyms)
            if value is None:
                translations.append(
                    f"# REVIEW: unknown code {_toml_str(code)} -> one of "
                    f"{', '.join(sorted(ENUM_VALUES[target]))}"
                )
            elif value != code.strip():
                translations.append(f"{_key(code.strip())} = {_toml_str(value)}")
        if translations:
            lines += ["", f"[{entity}.values.{target}]", *translations]
    return lines


def draft_mapping(
    data_dir: str | Path, synonyms: Synonyms | None = None, version: str = "mapping-draft-1"
) -> str:
    syn = synonyms or load_synonyms()
    out = [
        "# Draft generated by `python -m asas data draft-mapping`. Review every # REVIEW and",
        "# # TODO line, then run `python -m asas data check --data <dir> --mapping <this file>`.",
        f"version = {_toml_str(version)}",
    ]
    used: set[str] = set()
    for profile in profile_directory(data_dir):
        entity = guess_entity(profile, syn)
        if profile.error or entity is None or entity in used:
            out += [
                "",
                f"# REVIEW: {Path(profile.path).name} not mapped "
                f"({profile.error or 'entity unknown'})",
            ]
            continue
        used.add(entity)
        out += ["", *draft_entity(entity, profile, syn)]
    for entity in ("trade_events", "alerts"):
        if entity not in used:
            out += ["", f"# TODO: no file was recognised as {entity}; it is required"]
    return "\n".join(out) + "\n"
