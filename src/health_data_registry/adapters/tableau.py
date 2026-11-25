"""Tableau Server and Tableau Public dashboards: each worksheet's aggregate table, read from bootstrapSession.

A guest view of a Tableau dashboard gets its data in one answer, ``bootstrapSession``: a few chunks, each
``<length>;<json>``. The first lists the dashboard and its worksheets; the second carries the data as a
dictionary of typed value lists (``dataSegments``) and, for each worksheet, which entries of those lists make
up each column (``paneColumnsData``). This module reads those structures and nothing else:

- one table per worksheet, one record per mark, keyed by the worksheet's own field captions, every value as
  the dashboard publishes it. A field that also has display text (an alias) is given twice: ``<caption>`` (the
  value) and ``<caption> (alias)`` (the text shown).
- the dashboard's own suppression is kept as published: an aggregated count shown as "<6" (or "<11", ">=5"
  ...) is never turned into a number. Its value and its display text are both left empty, whatever the value
  field holds, and ``<caption> flag`` holds the marker exactly as shown. A contract that reads such a field must
  also read its flag; a page that would drop the marker is refused.
- a count the chart carries as a number while the dashboard shows it suppressed on the same mark (a tooltip
  reading "<16") is read as suppressed too, where the catalog entry declares which field carries its marker
  (``suppressed_by``).
- each worksheet's column list is hashed (``layout_hash``) and must equal the hash in the catalog entry, so a
  redesigned dashboard stops the load instead of shifting a column.
- a worksheet whose grain is one person is refused (adapters/row_guard.py).
- workbook files (.twb, .twbx) are never downloaded. ``<view>.csv`` is not used: on the Detroit Health
  Department's server a dashboard view's .csv answered 404 (checked 2026-11-24); bootstrapSession works everywhere.

The fetcher hands back the captured bootstrapSession for a view URL (crawl.py opens the view in the browser
fetcher and captures the answer whose path contains ``/bootstrapSession/``).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from health_data_registry.adapters import Fetcher, Page
from health_data_registry.adapters.row_guard import check_grain
from health_data_registry.registry import CatalogEntry

SUPPRESSED: Final = re.compile(r"^\s*(<|>|<=|>=|≤|≥)\s*\d+(\.\d+)?\s*$")
# Tableau names an aggregated field after its aggregation: "SUM(Cases)", "AGG(Count Suppressed)", "CNT(Id)".
# Only those carry counts, so only they are read for suppression markers; a dimension label such as the age
# band "< 20" is a label, never a suppressed count.
AGGREGATED: Final = re.compile(r"^[A-Z]+\(")
# The aggregations that make a number of a group (not a date part such as YEAR(...) or MDY(...)): the fields whose
# flag the drift check lists as served before any marker has appeared.
COUNTING: Final = re.compile(r"^(SUM|AGG|CNT|CNTD|COUNT|COUNTD|AVG|MEDIAN|MIN|MAX)\(")
ALIAS_SUFFIX: Final = " (alias)"
FLAG_SUFFIX: Final = " flag"
WORKSHEET_FIELD: Final = "worksheet"
VIEW_FIELD: Final = "view"


class TableauLayoutError(ValueError):
    """The dashboard's answer is not what the catalog entry describes (a redesign, or not a bootstrapSession)."""


@dataclass(frozen=True)
class Column:
    caption: str
    data_type: str
    # (pane index, column index within that pane) for every pane the column appears in
    places: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class Worksheet:
    name: str
    columns: tuple[Column, ...]
    records: tuple[dict[str, Any], ...]

    @property
    def fields(self) -> list[str]:
        return sorted({k for r in self.records for k in r})

    @property
    def layout_hash(self) -> str:
        return layout_hash(self.columns)

    @property
    def captions(self) -> list[str]:
        return [c.caption for c in self.columns]

    def by_caption(self) -> list[dict[str, Any]]:
        """Each record keyed by caption only (a value, else its display text), for the row-level guard."""
        out = []
        for r in self.records:
            rec = {k.removesuffix(ALIAS_SUFFIX): v for k, v in r.items() if k.endswith(ALIAS_SUFFIX)}
            rec.update({k: v for k, v in r.items() if not k.endswith((ALIAS_SUFFIX, FLAG_SUFFIX))})
            out.append(rec)
        return out


def layout_hash(columns: Sequence[Column]) -> str:
    """sha256 of the worksheet's (caption, data type) pairs in published order: changes when the layout does."""
    text = json.dumps([[c.caption, c.data_type] for c in columns], separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_chunks(body: bytes | str) -> list[dict[str, Any]]:
    """The JSON chunks of a bootstrapSession answer (``<length>;<json><length>;<json>...``)."""
    text = body.decode("utf-8") if isinstance(body, bytes) else body
    chunks: list[dict[str, Any]] = []
    i = 0
    while i < len(text):
        m = re.match(r"\s*(\d+);", text[i:])
        if not m:
            raise TableauLayoutError("the answer is not a Tableau bootstrapSession (no <length>; prefix)")
        start = i + m.end()
        end = start + int(m.group(1))
        if end > len(text):
            raise TableauLayoutError("the bootstrapSession answer was cut short")
        try:
            chunk = json.loads(text[start:end])
        except json.JSONDecodeError as e:
            raise TableauLayoutError("a bootstrapSession chunk is not valid JSON") from e
        if not isinstance(chunk, dict):
            raise TableauLayoutError("a bootstrapSession chunk is not a JSON object")
        chunks.append(chunk)
        i = end
    if not chunks:
        raise TableauLayoutError("the bootstrapSession answer is empty")
    return chunks


def _get(d: Any, *path: str) -> Any:
    for p in path:
        if not isinstance(d, Mapping) or p not in d:
            return None
        d = d[p]
    return d


def dashboard_worksheets(chunks: Sequence[Mapping[str, Any]]) -> list[str]:
    """The worksheet names shown on the dashboard (its zones), in a stable order."""
    zones = _get(
        chunks[0], "worldUpdate", "applicationPresModel", "workbookPresModel", "dashboardPresModel", "zones"
    )
    if not isinstance(zones, Mapping):
        return []
    return sorted(
        {str(z["worksheet"]) for z in zones.values() if isinstance(z, Mapping) and z.get("worksheet")}
    )


def _data_dictionary(chunks: Sequence[Mapping[str, Any]]) -> dict[str, list[Any]]:
    """Every data segment's typed value lists, concatenated per data type in segment order."""
    full: dict[str, list[Any]] = {}
    for chunk in chunks:
        for root in (
            _get(chunk, "secondaryInfo", "presModelMap"),
            _get(chunk, "worldUpdate", "applicationPresModel", "presModelMap"),
        ):
            segs = _get(
                root, "dataDictionary", "presModelHolder", "genDataDictionaryPresModel", "dataSegments"
            )
            if not isinstance(segs, Mapping):
                continue
            for key in sorted(segs, key=lambda k: int(k)):
                for col in segs[key].get("dataColumns") or []:
                    full.setdefault(str(col["dataType"]), []).extend(col.get("dataValues") or [])
    return full


def _viz_models(chunks: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    for chunk in chunks:
        for root in (
            _get(chunk, "secondaryInfo", "presModelMap"),
            _get(chunk, "worldUpdate", "applicationPresModel", "presModelMap"),
        ):
            pm = _get(root, "vizData", "presModelHolder", "genPresModelMapPresModel", "presModelMap")
            if not isinstance(pm, Mapping):
                continue
            for name, v in pm.items():
                cd = _get(v, "presModelHolder", "genVizDataPresModel", "paneColumnsData")
                if isinstance(cd, Mapping):
                    out[str(name)] = cd
    return out


def _lookup(full: Mapping[str, list[Any]], data_type: str, index: int, *, alias: bool) -> Any:
    """One value from the dictionary. Alias indices below zero point into the string list (Tableau's encoding)."""
    if alias and index < 0:
        values, index = full.get("cstring", []), -index - 1
    else:
        values = full.get(data_type, [])
    if not 0 <= index < len(values):
        raise TableauLayoutError(f"a {data_type} index ({index}) points outside the data dictionary")
    return values[index]


def _cell(value: Any) -> Any:
    return None if value is None else value


def read_worksheets(body: bytes | str) -> dict[str, Worksheet]:
    """Every worksheet with data in a bootstrapSession answer, as published (suppression split into flags)."""
    chunks = split_chunks(body)
    full = _data_dictionary(chunks)
    out: dict[str, Worksheet] = {}
    for name, cd in _viz_models(chunks).items():
        columns: list[Column] = []
        for c in cd.get("vizDataColumns") or []:
            caption = c.get("fieldCaption")
            if not caption:
                continue  # Tableau's own tuple ids, not a published field
            places = tuple(
                zip((int(p) for p in c["paneIndices"]), (int(x) for x in c["columnIndices"]), strict=True)
            )
            name_ = str(caption)
            seen = sum(1 for x in columns if x.caption == name_ or x.caption.startswith(name_ + " #"))
            # Two fields can share a caption (the same measure as a count and as a percent): number the repeats
            # in published order so neither overwrites the other.
            columns.append(
                Column(name_ if not seen else f"{name_} #{seen + 1}", str(c.get("dataType")), places)
            )
        panes = cd.get("paneColumnsList") or []
        records: list[dict[str, Any]] = []
        for p, pane in enumerate(panes):
            viz = pane.get("vizPaneColumns") or []
            here = [(col, idx) for col in columns for (pi, idx) in col.places if pi == p]
            lists: dict[str, list[Any]] = {}
            for col, idx in here:
                if idx >= len(viz):
                    raise TableauLayoutError(f"{name}: column {col.caption} points past its pane")
                vals = viz[idx].get("valueIndices") or []
                aliases = viz[idx].get("aliasIndices") or []
                if vals:
                    lists[col.caption] = [_lookup(full, col.data_type, i, alias=False) for i in vals]
                if aliases:
                    lists[col.caption + ALIAS_SUFFIX] = [
                        _lookup(full, col.data_type, i, alias=True) for i in aliases
                    ]
            if not lists:
                continue
            lengths = {len(v) for v in lists.values()}
            if len(lengths) != 1:
                raise TableauLayoutError(f"{name}: the columns of one pane have different lengths; not read")
            n = lengths.pop()
            for i in range(n):
                records.append(_flag_suppressed({k: _cell(v[i]) for k, v in lists.items()}))
        if records:
            out[str(name)] = Worksheet(str(name), tuple(columns), tuple(records))
    return out


def _flag_suppressed(record: dict[str, Any]) -> dict[str, Any]:
    """A cell the dashboard shows as "<6" stays "<6", in a flag field; its own field is left empty, never a number."""
    out = dict(record)
    for k, v in record.items():
        if isinstance(v, str) and AGGREGATED.match(k) and SUPPRESSED.match(v):
            base = k.removesuffix(ALIAS_SUFFIX)
            out[base + FLAG_SUFFIX] = v.strip()
            # Both the value and its display text are emptied, whatever the value field holds: a number behind a
            # "<6" is the count the publisher chose to hide (hard rule 4).
            for field in (base, base + ALIAS_SUFFIX):
                if field in out:
                    out[field] = None
    return out


def apply_suppressed_by(record: Mapping[str, Any], suppressed_by: Mapping[str, str]) -> dict[str, Any]:
    """Empty a count wherever the dashboard shows its declared marker field suppressed ("<16") on the same mark.

    The count's own field and its alias are emptied and ``<count> flag`` holds the marker as shown, so the number
    behind a suppressed bar is never read.
    """
    out = dict(record)
    for count, marker_field in suppressed_by.items():
        marker = record.get(marker_field + FLAG_SUFFIX)
        if not marker or (count not in record and count + ALIAS_SUFFIX not in record):
            continue  # nothing shown suppressed, or this mark (another layer of the chart) has no such count
        for k in (count, count + ALIAS_SUFFIX):
            if k in out:
                out[k] = None
        out[count + FLAG_SUFFIX] = marker
    return out


def _check_flags_kept(dataset: str, where: str, record: Mapping[str, Any], wanted: set[str]) -> None:
    """Refuse a record whose suppression marker the contract would drop while it reads the field it flags.

    A count read without its flag would come back empty with no reason, or (if the contract read the other half
    of a value/alias pair) as the number behind the marker; so the page is refused instead, in plain words.
    """
    for k, v in record.items():
        if not k.endswith(FLAG_SUFFIX) or v is None or k in wanted:
            continue
        base = k.removesuffix(FLAG_SUFFIX)
        if base in wanted or base + ALIAS_SUFFIX in wanted:
            raise TableauLayoutError(
                f"{dataset}: {where} now shows {base} as suppressed ({v}), and the contract has no flag column "
                f"for it ({k}); the contract needs review before this dashboard is read"
            )


def view_url(entry: CatalogEntry, view: str) -> str:
    base = entry.source.api_url.rstrip("/")
    return f"{base}/{view}?:embed=y&:showVizHome=no"


def pages(
    entry: CatalogEntry, fetcher: Fetcher, *, max_rows: int | None = None, fields: list[str] | None = None
) -> Iterator[Page]:
    """One page per catalogued view: the declared worksheets' records, each tagged with view and worksheet."""
    spec = entry.source.tableau
    if spec is None:  # pragma: no cover - the catalog model requires it for this portal
        raise TableauLayoutError(f"{entry.id}: no tableau views are catalogued")
    left = max_rows
    wanted = set(fields) if fields else None
    for view in spec.views:
        raw = fetcher(view_url(entry, view.view))
        sheets = read_worksheets(raw.content)
        recs: list[dict[str, Any]] = []
        layout: dict[str, str] = {}
        for ws_name, expected in view.worksheets.items():
            ws = sheets.get(ws_name)
            if ws is None:
                raise TableauLayoutError(
                    f"{entry.id}: the dashboard view {view.view} no longer has the worksheet {ws_name!r}; "
                    "the catalog entry needs review"
                )
            if ws.layout_hash != expected:
                raise TableauLayoutError(
                    f"{entry.id}: the worksheet {ws_name!r} in view {view.view} was redesigned (its columns are "
                    f"now {', '.join(c.caption for c in ws.columns)}); the contract needs review before it is read"
                )
            check_grain(f"{entry.id}: {view.view}/{ws_name}", ws.captions, ws.by_caption())
            layout[f"{view.view}/{ws_name}"] = ws.layout_hash
            for r in ws.records:
                rec = {
                    VIEW_FIELD: view.view,
                    WORKSHEET_FIELD: ws_name,
                    **apply_suppressed_by(r, view.suppressed_by),
                }
                if wanted is not None:
                    _check_flags_kept(entry.id, f"{view.view}/{ws_name}", rec, wanted)
                    rec = {k: v for k, v in rec.items() if k in wanted}
                recs.append(rec)
        if left is not None:
            recs, left = recs[: max(0, left)], max(0, left - len(recs))
        yield Page(fetch=raw, records=recs, layout=layout)
        if left == 0:
            break  # the row limit is used up: no more views are opened


def publisher_fields(entry: CatalogEntry, fetcher: Fetcher) -> set[str]:
    """Every field the catalogued worksheets serve today, for the contract drift check.

    An aggregated field's ``<caption> flag`` is served too: the adapter writes it whenever the dashboard shows that
    field suppressed, so a contract may read it before any marker has appeared.
    """
    out = {VIEW_FIELD, WORKSHEET_FIELD}
    spec = entry.source.tableau
    for view in spec.views if spec else []:
        sheets = read_worksheets(fetcher(view_url(entry, view.view)).content)
        for ws_name in view.worksheets:
            if ws_name in sheets:
                out |= set(sheets[ws_name].fields)
                out |= {c.caption + FLAG_SUFFIX for c in sheets[ws_name].columns if COUNTING.match(c.caption)}
        out |= {count + FLAG_SUFFIX for count in view.suppressed_by}
    return out
