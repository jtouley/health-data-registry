"""export-public: publish the redistributable sources to the open health data hub, and list the rest.

Ported from coffee-data-tracker's src/coffeedata/public.py (MIT, same owner): one commit per export with a
manifest.json beside the licence text, holding the release id and run date, the contract and data licence, and
each file's sha256 and row count; the commit is tagged release-<run date> so a study can pin the week it used.

Destinations: hf://datasets/<org>/<name> (staged in build/hub, then one commit), or a local directory.

Layout of a release (the same in a directory and on the Hub):

    README.md                      the hub card: every source, hosted or listed, one config per hosted table
    manifest.json                  the release: id, run date, product, every source and every file's sha256
    <source>/README.md             the dataset card, in plain clinical words, with its YAML header
    <source>/manifest.json         this source's part of the release manifest
    <source>/LICENSE.md            the publisher's terms, as recorded in licenses/ (hosted sources only)
    <source>/data/<source>.parquet the snapshot's table, byte for byte (hosted sources only)
    publish-checklist.md           the manual check of sampled numbers (never uploaded)

Only sources whose catalog licence.redistribution is true get data files. The rest are listed, not hosted: a
card saying where the data live, their terms and citation, and that the MCP server and
`health-data-registry fetch <source>` get them from the publisher at run time.

Every export runs the pre-publish checks first and writes nothing to the Hub if one fails.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import yaml

from health_data_registry.product import Port, load_ports
from health_data_registry.registry import Dataset, Registry
from health_data_registry.store import Manifest, latest_snapshot

HF_PREFIX = "hf://datasets/"
STAGING = Path("build") / "hub"
LICENCE_FILE = "LICENSE.md"
CHECKLIST = "publish-checklist.md"
SAMPLE_ROWS = 5
_RUN_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The Hub tag for each infectious-disease area (catalog id_areas), one collection per area.
AREA_TAGS = {
    "hiv": "hiv",
    "stis": "sexually-transmitted-infections",
    "hepatitis": "hepatitis",
    "tb": "tuberculosis",
    "hais_amr": "healthcare-associated-infections-and-amr",
    "respiratory": "respiratory-infections",
    "vaccine_preventable": "vaccine-preventable-diseases",
    "enteric": "enteric-infections",
    "vector_borne": "vector-borne-diseases",
}


class PublishCheckFailed(ValueError):
    """A pre-publish check failed; the message lists every problem in plain words."""


@dataclass
class Release:
    release_id: str
    run_date: str
    dest: str
    local: Path
    hosted: list[str] = field(default_factory=list)
    listed: list[str] = field(default_factory=list)
    tag: str | None = None

    def summary(self) -> list[str]:
        out = [f"release {self.release_id} (run {self.run_date}) written to {self.dest}"]
        out += [f"hosted {s}" for s in self.hosted]
        out += [f"listed {s} (no data files; fetched from the publisher at run time)" for s in self.listed]
        out.append(f"manual check before announcing: {self.local / CHECKLIST}")
        if self.tag:
            out.append(f"tagged {self.tag}")
        return out


# ------------------------------------------------------------------------------------------------ helpers


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hf_repo_id(url: str) -> str:
    """`hf://datasets/<owner>/<name>[@rev][/path]` -> `<owner>/<name>` (as in coffee-data-tracker)."""
    repo = "/".join(url.removeprefix(HF_PREFIX).split("@")[0].strip("/").split("/")[:2])
    if not url.startswith(HF_PREFIX) or repo.count("/") != 1 or not all(repo.split("/")):
        raise ValueError(f"{url!r} is not hf://datasets/<org>/<name>")
    return repo


def release_tag(run_date: str) -> str:
    return f"release-{run_date}"


def release_id(run_date: str, snapshot_ids: Iterable[str]) -> str:
    """The run date plus a short hash of the snapshots in it: the same inputs always give the same id."""
    digest = hashlib.sha256("\n".join(sorted(snapshot_ids)).encode()).hexdigest()[:12]
    return f"{run_date}-{digest}"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def citation(ds: Dataset, manifest: Manifest | None) -> str:
    """The publisher's citation, with the access date filled in from the snapshot."""
    when = manifest.retrieved_at[:10] if manifest else "the date you downloaded it"
    return ds.entry.citation.replace("<retrieved_at>", when)


def tags(ds: Dataset) -> list[str]:
    """Card tags: ID area, condition, geography and publisher."""
    e = ds.entry
    out = ["infectious-disease", *(AREA_TAGS[a] for a in e.id_areas)]
    out += [slug(s.label) for s in ds.semantic.series]
    out += [slug(c.cause) for c in e.gbd.causes]
    out += [f"geography-{slug(g)}" for g in e.geography]
    if e.country:
        out.append(f"country-{slug(e.country)}")
    out.append(f"publisher-{slug(e.agency)}")
    return list(dict.fromkeys(t for t in out if t))


def _front_matter(header: dict[str, Any]) -> str:
    return "---\n" + yaml.safe_dump(header, sort_keys=False, allow_unicode=True, width=100) + "---\n"


def read_front_matter(card: str) -> dict[str, Any]:
    if not card.startswith("---\n"):
        return {}
    end = card.index("\n---\n", 4)
    return dict(yaml.safe_load(card[4:end]) or {})


def data_path(dataset_id: str) -> str:
    return f"{dataset_id}/data/{dataset_id}.parquet"


def _years(ds: Dataset) -> str:
    y = ds.entry.years
    return f"{y.start} to {y.end}" if y.end else f"{y.start} onward"


# ------------------------------------------------------------------------------------------------ cards


def _case_definitions(ds: Dataset) -> list[str]:
    out = []
    for s in ds.semantic.series:
        status = f"; case status: {s.case_status}" if s.case_status else ""
        definition = s.case_definition or "not yet recorded in the contract; see the publisher's codebook"
        out.append(f"- **{s.label}** ({s.measure}): {definition}{status}")
    if not out:
        out.append(f"- Not recorded in the contract yet; see the publisher's codebook: {ds.entry.codebook_url}")
    return out


def _suppression(ds: Dataset) -> list[str]:
    lines = [
        "Cells the publisher suppressed or marked missing are empty (null) here, exactly as published. They are "
        "never filled in, back-calculated or set to zero, so an empty cell is not a zero."
    ]
    coded = [c for c in ds.contract.columns if c.missing_codes]
    if coded:
        lines.append("")
        lines += [
            f"- `{c.name}`: the publisher's codes {', '.join(repr(m) for m in c.missing_codes)} read as empty"
            for c in coded
        ]
    flags = [c for c in ds.contract.columns if c.flag_for]
    if flags:
        lines.append("")
        lines += [
            f"- `{c.name}` keeps the publisher's flag for `{c.flag_for}` (why a value is missing)" for c in flags
        ]
    return lines


def _caveats(ds: Dataset) -> list[str]:
    out = [f"- {c}" for c in ds.entry.caveats]
    if ds.entry.status != "reviewed":
        out.append("- This dataset's contract is a draft: no maintainer has reviewed it against the codebook yet.")
    out += [f"- Not yet confirmed: {q}" for q in ds.contract.open_questions]
    return out


def hosted_card(
    ds: Dataset, manifest: Manifest, *, release: str, run_date: str, rows: int, sha: str, repo: str | None
) -> str:
    e = ds.entry
    path = data_path(ds.id)
    header = {
        "pretty_name": e.title,
        "license": "other",
        "license_name": e.licence.id,
        "license_link": e.licence.terms_url,
        "tags": tags(ds),
        "configs": [{"config_name": ds.id, "data_files": [{"split": "train", "path": path}]}],
    }
    where = f"hf://datasets/{repo}/{path}" if repo else path
    cols = ["| Column | What it means | Unit |", "|---|---|---|"]
    cols += [f"| `{c.name}` | {c.description} | {c.unit or ''} |" for c in ds.contract.columns]
    body = [
        f"# {e.title}",
        "",
        f"{e.publisher}. Hosted copy of the publisher's table, release `{release}` (run {run_date}), fetched "
        f"{manifest.retrieved_at[:10]} from [{e.source.landing_url}]({e.source.landing_url}).",
        "",
        "## What one row is",
        "",
        e.grain,
        "",
        "## Case definition",
        "",
        *_case_definitions(ds),
        "",
        "## Years and geography",
        "",
        f"Years {_years(ds)}. Geography: {', '.join(e.geography)}" + (f" ({e.country})." if e.country else "."),
        "",
        "## Suppression and missing values",
        "",
        *_suppression(ds),
        "",
        "## Caveats",
        "",
        *_caveats(ds),
        "",
        "## Columns",
        "",
        *cols,
        "",
        "## Licence and how to cite",
        "",
        f"Licence: {e.licence.id} ([terms]({e.licence.terms_url})); the text is in `{LICENCE_FILE}` beside the data.",
        "",
        f"Cite as: {citation(ds, manifest)}",
        "",
        "## Files",
        "",
        "| File | Rows | sha256 |",
        "|---|---|---|",
        f"| `{path}` | {rows} | `{sha}` |",
        "",
        "## Use it",
        "",
        "```sql",
        "-- DuckDB, no download step",
        f"select * from '{where}' limit 10;",
        "```",
        "",
        f"Pin this release with `@{release_tag(run_date)}` after the dataset id. Contract `{ds.contract.id}` "
        f"version {ds.contract.version}; the codebook is at {e.codebook_url}.",
        "",
    ]
    return _front_matter(header) + "\n" + "\n".join(body)


def listed_card(ds: Dataset, *, release: str, run_date: str) -> str:
    e = ds.entry
    header = {
        "pretty_name": f"{e.title} (listed, not hosted)",
        "license": "other",
        "license_name": e.licence.id,
        "license_link": e.licence.terms_url,
        "tags": [*tags(ds), "listed-not-hosted"],
    }
    body = [
        f"# {e.title} (listed, not hosted)",
        "",
        f"{e.publisher}. This dataset is listed in release `{release}` (run {run_date}) but its data are not "
        "hosted here: the publisher's terms do not allow redistribution.",
        "",
        "## Where the data live",
        "",
        f"- Publisher's page: {e.source.landing_url}",
        f"- Data service: {e.source.api_url}",
        f"- Codebook: {e.codebook_url}",
        "",
        "## Terms",
        "",
        f"{e.licence.id}: {e.licence.terms_url}. Follow these terms whenever you use or share the numbers.",
        "",
        "## How to get the data",
        "",
        "The health-data-registry MCP server fetches it from the publisher at run time, the first time you ask "
        f"about it. From a terminal, `health-data-registry fetch {ds.id}` gets it from the publisher into your own "
        "local copy. Nothing is downloaded from this hub.",
        "",
        "## What one row is",
        "",
        e.grain,
        "",
        "## Case definition",
        "",
        *_case_definitions(ds),
        "",
        "## Years and geography",
        "",
        f"Years {_years(ds)}. Geography: {', '.join(e.geography)}.",
        "",
        "## Caveats",
        "",
        *_caveats(ds),
        "",
        "## How to cite",
        "",
        f"Cite as: {citation(ds, None)}",
        "",
    ]
    return _front_matter(header) + "\n" + "\n".join(body)


def hub_card(registry: Registry, release: dict[str, Any], repo: str | None) -> str:
    hosted = [registry.get(s) for s, v in release["sources"].items() if v["hosted"]]
    listed = [registry.get(s) for s, v in release["sources"].items() if not v["hosted"]]
    all_tags: list[str] = []
    for ds in [*hosted, *listed]:
        all_tags += tags(ds)
    header = {
        "pretty_name": "Open health data hub",
        "license": "other",
        "license_name": "per-source",
        "license_link": "https://github.com/jtouley/health-data-registry/blob/main/licenses/README.md",
        "tags": list(dict.fromkeys(all_tags)),
        "configs": [
            {"config_name": ds.id, "data_files": [{"split": "train", "path": data_path(ds.id)}]} for ds in hosted
        ],
    }
    rows = ["| Source | Publisher | Years | Here |", "|---|---|---|---|"]
    rows += [f"| [{d.entry.title}]({d.id}/README.md) | {d.entry.agency} | {_years(d)} | hosted |" for d in hosted]
    rows += [
        f"| [{d.entry.title}]({d.id}/README.md) | {d.entry.agency} | {_years(d)} | listed, not hosted |"
        for d in listed
    ]
    body = [
        "# Open health data hub",
        "",
        "Public, population-level infectious disease data from CDC, WHO and other publishers, each table with its "
        "publisher's citation, terms and a plain-words card. Built by "
        "[health-data-registry](https://github.com/jtouley/health-data-registry).",
        "",
        f"Release `{release['release_id']}` (run {release['run_date']}). Pin it with "
        f"`@{release_tag(release['run_date'])}`. `manifest.json` lists every file's sha256 and row count.",
        "",
        *rows,
        "",
        "Listed sources have a card but no data files: their terms do not allow redistribution, so the MCP "
        "server and `health-data-registry fetch <source>` get them from the publisher at run time.",
        "",
        "Empty cells are suppressed or missing as the publisher published them; they are never zeros.",
        "",
    ]
    return _front_matter(header) + "\n" + "\n".join(body)


# ------------------------------------------------------------------------------------------------ export


def _licence_text(registry: Registry, licence_id: str) -> Path | None:
    p = registry.root / "licenses" / f"{licence_id}.md"
    return p if p.is_file() else None


def _source_manifest(
    ds: Dataset, *, release: str, run_date: str, port: Port | None, snap: Manifest | None, hosted: bool
) -> dict[str, Any]:
    return {
        "release_id": release,
        "run_date": run_date,
        "source": ds.id,
        "hosted": hosted,
        "contract": {"id": ds.contract.id, "version": ds.contract.version, "status": ds.contract.status},
        "data_product": {"id": port.product_id, "port": port.name, "version": port.version} if port else None,
        "licence": {
            "id": ds.entry.licence.id,
            "terms_url": ds.entry.licence.terms_url,
            "redistribution": ds.entry.licence.redistribution,
        },
        "citation": citation(ds, snap),
        "snapshot": asdict(snap) if snap and hosted else None,
        "files": {},
    }


def _parquet_rows(path: Path) -> int:
    con = duckdb.connect(":memory:")
    try:
        row = con.execute("SELECT count(*) FROM read_parquet(?)", [str(path)]).fetchone()
    finally:
        con.close()
    return int(row[0]) if row else 0


def stage(registry: Registry, lake: Path, out: Path, *, run_date: str, repo: str | None) -> dict[str, Any]:
    """Write the release into `out` (cards, licence texts, data files, manifests). Returns the release manifest."""
    ports = load_ports(registry.root)
    snaps: dict[str, tuple[Path, Manifest]] = {}
    problems: list[str] = []
    for ds in registry:
        if not ds.entry.licence.redistribution:
            continue
        found = latest_snapshot(lake, ds.id)
        if found is None:
            problems.append(
                f"{ds.id}: there is no snapshot in the lake to publish; run `health-data-registry fetch {ds.id}`"
            )
        else:
            snaps[ds.id] = found
    if problems:
        raise PublishCheckFailed("; ".join(problems))
    rid = release_id(run_date, [m.snapshot_id for _, m in snaps.values()])
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    release: dict[str, Any] = {
        "release_id": rid,
        "run_date": run_date,
        "tag": release_tag(run_date),
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "data_product": None,
        "sources": {},
        "files": {},
    }
    for ds in registry:
        port = ports.get(ds.contract.id)
        if port and release["data_product"] is None:
            release["data_product"] = {"id": port.product_id, "version": port.product_version}
        folder = out / ds.id
        folder.mkdir()
        hosted = ds.entry.licence.redistribution
        snap = snaps[ds.id][1] if hosted else None
        sm = _source_manifest(ds, release=rid, run_date=run_date, port=port, snap=snap, hosted=hosted)
        if hosted and snap is not None:
            target = out / data_path(ds.id)
            target.parent.mkdir()
            shutil.copyfile(snaps[ds.id][0] / "data.parquet", target)
            rows, sha = _parquet_rows(target), sha256_file(target)
            sm["files"][f"data/{ds.id}.parquet"] = {"sha256": sha, "rows": rows}
            lic = _licence_text(registry, ds.entry.licence.id)
            if lic is not None:
                shutil.copyfile(lic, folder / LICENCE_FILE)
                sm["files"][LICENCE_FILE] = {"sha256": sha256_file(folder / LICENCE_FILE), "rows": None}
            card = hosted_card(ds, snap, release=rid, run_date=run_date, rows=rows, sha=sha, repo=repo)
        else:
            card = listed_card(ds, release=rid, run_date=run_date)
        (folder / "README.md").write_text(card, encoding="utf-8")
        sm["files"]["README.md"] = {"sha256": sha256_file(folder / "README.md"), "rows": None}
        (folder / "manifest.json").write_text(json.dumps(sm, indent=1) + "\n", encoding="utf-8")
        release["sources"][ds.id] = sm
        for name, meta in sm["files"].items():
            release["files"][f"{ds.id}/{name}"] = meta
    (out / "README.md").write_text(hub_card(registry, release, repo), encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps(release, indent=1) + "\n", encoding="utf-8")
    return release


# ------------------------------------------------------------------------------------------------ checks


def suppression_problems(snapshot_db: Path, table: str, exported: Path) -> list[str]:
    """Is the exported table exactly the snapshot? A suppressed or missing cell filled in is named by column."""
    con = duckdb.connect(":memory:")
    try:
        con.execute("ATTACH ? AS snap (READ_ONLY)", [str(snapshot_db)])
        q = '"' + table.replace('"', '""') + '"'
        con.execute(f"CREATE VIEW a AS SELECT * FROM snap.{q}")  # noqa: S608 - quoted catalog identifier
        con.execute(f"CREATE VIEW b AS SELECT * FROM read_parquet({_lit(str(exported))})")  # noqa: S608 - quoted path
        cols_a = [r[0] for r in con.execute("DESCRIBE a").fetchall()]
        cols_b = [r[0] for r in con.execute("DESCRIBE b").fetchall()]
        if cols_a != cols_b:
            return [f"{table}: the published columns differ from the snapshot's ({cols_b} vs {cols_a})"]
        out = []
        for c in cols_a:
            qc = '"' + c.replace('"', '""') + '"'
            na, nb = (
                int((con.execute(f"SELECT count(*) - count({qc}) FROM {v}").fetchone() or (0,))[0])  # noqa: S608 - quoted column names
                for v in ("a", "b")
            )
            if nb < na:
                out.append(
                    f"{table}.{c}: {na - nb} suppressed or missing cells were filled in; the hub must keep them "
                    "empty exactly as the publisher published them"
                )
            elif nb > na:
                out.append(f"{table}.{c}: {nb - na} published values were emptied")
        diff = (con.execute("SELECT count(*) FROM (SELECT * FROM a EXCEPT ALL SELECT * FROM b)").fetchone() or (0,))[0]
        extra = (con.execute("SELECT count(*) FROM (SELECT * FROM b EXCEPT ALL SELECT * FROM a)").fetchone() or (0,))[0]
        if not out and (diff or extra):
            out.append(f"{table}: {diff} snapshot rows are changed or missing in the published table")
        return out
    finally:
        con.close()


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def check_release(registry: Registry, lake: Path, out: Path) -> list[str]:
    """The pre-publish checks. Each problem is one plain sentence; any problem stops the export."""
    release = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    hub = read_front_matter((out / "README.md").read_text(encoding="utf-8"))
    problems: list[str] = []
    for ds in registry:
        e = ds.entry
        folder = out / ds.id
        card_text = (folder / "README.md").read_text(encoding="utf-8")
        header = read_front_matter(card_text)
        sm = release["sources"][ds.id]
        # The card carries the publisher's citation.
        if sm["citation"] not in card_text or not e.citation.strip():
            problems.append(f"{ds.id}: the card does not carry the publisher's citation")
        # The source shows up under its ID area tag, on its card and on the hub.
        if not e.id_areas:
            problems.append(f"{ds.id}: no ID area is recorded in its catalog entry (id_areas)")
        for area in e.id_areas:
            tag = AREA_TAGS[area]
            if tag not in header.get("tags", []) or tag not in hub.get("tags", []):
                problems.append(f"{ds.id}: the hub does not list it under its ID area tag {tag}")
        data_files = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix in (".parquet", ".duckdb"))
        if not e.licence.redistribution:
            if data_files:
                problems.append(f"{ds.id}: its terms forbid redistribution but data files were staged")
            continue
        # Licence confirmed and recorded.
        snap_found = latest_snapshot(lake, ds.id)
        if _licence_text(registry, e.licence.id) is None or not (folder / LICENCE_FILE).is_file():
            problems.append(f"{ds.id}: licence {e.licence.id} has no recorded text in licenses/{e.licence.id}.md")
        if not e.licence.terms_url.startswith("https://"):
            problems.append(f"{ds.id}: the licence's terms page is not recorded")
        if snap_found is None:
            problems.append(f"{ds.id}: its snapshot is gone from the lake")
            continue
        snap_dir, snap = snap_found
        if snap.licence != e.licence.id or not snap.redistribution:
            problems.append(
                f"{ds.id}: the snapshot was taken under licence {snap.licence}, not the catalog's {e.licence.id}; "
                "refresh it before publishing"
            )
        if snap.partial:
            problems.append(f"{ds.id}: the snapshot is a partial development sample, not the full table")
        # Every file the manifest names is there with its hash.
        for name, meta in sm["files"].items():
            p = folder / name
            if not p.is_file() or sha256_file(p) != meta["sha256"]:
                problems.append(f"{ds.id}: {name} does not match its sha256 in manifest.json")
        # Suppression kept exactly as published.
        exported = out / data_path(ds.id)
        if exported.is_file():
            problems += suppression_problems(snap_dir / "data.duckdb", ds.id, exported)
        else:
            problems.append(f"{ds.id}: the data file is missing")
    return problems


def write_checklist(registry: Registry, out: Path) -> Path:
    """The manual step: a sample of numbers to check against the publisher's own page before announcing."""
    release = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    lines = [
        f"# Publish checklist: release {release['release_id']} (run {release['run_date']})",
        "",
        "Before announcing the release, open each publisher's page and tick each row whose numbers match.",
        "",
    ]
    con = duckdb.connect(":memory:")
    try:
        for sid, sm in release["sources"].items():
            if not sm["hosted"]:
                continue
            ds = registry.get(sid)
            lines += [f"## {ds.entry.title}", "", f"Publisher's page: {ds.entry.source.landing_url}", ""]
            rows = con.execute(
                f"SELECT * FROM read_parquet({_lit(str(out / data_path(sid)))}) "  # noqa: S608 - staged path, quoted
                f"USING SAMPLE reservoir({SAMPLE_ROWS} ROWS) REPEATABLE (42)"
            )
            names = [d[0] for d in rows.description or []]
            for r in rows.fetchall():
                cells = ", ".join(f"{n}={'(empty)' if v is None else v}" for n, v in zip(names, r, strict=True))
                lines.append(f"- [ ] {cells}")
            lines.append("")
    finally:
        con.close()
    path = out / CHECKLIST
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ------------------------------------------------------------------------------------------------ upload

Uploader = Callable[[Path, str, dict[str, Any]], str | None]


def export_public(
    registry: Registry,
    lake: Path,
    dest: str,
    *,
    run_date: str | None = None,
    uploader: Uploader | None = None,
    staging: Path = STAGING,
) -> Release:
    """Stage, check and (for hf://) upload one release. Raises PublishCheckFailed before any upload."""
    run_date = run_date or datetime.now(UTC).strftime("%Y-%m-%d")
    if not _RUN_DATE.fullmatch(run_date):
        raise PublishCheckFailed(f"run date {run_date!r} is not YYYY-MM-DD")
    to_hub = dest.startswith(HF_PREFIX)
    repo = hf_repo_id(dest) if to_hub else None
    if to_hub and uploader is None and not os.environ.get("HF_TOKEN"):
        raise PublishCheckFailed("publishing to Hugging Face needs a write token in the HF_TOKEN environment variable")
    out = staging if to_hub else Path(dest)
    release = stage(registry, lake, out, run_date=run_date, repo=repo)
    problems = check_release(registry, lake, out)
    if problems:
        raise PublishCheckFailed("pre-publish checks failed:\n  - " + "\n  - ".join(problems))
    write_checklist(registry, out)
    result = Release(
        release_id=release["release_id"],
        run_date=run_date,
        dest=dest,
        local=out,
        hosted=[s for s, v in release["sources"].items() if v["hosted"]],
        listed=[s for s, v in release["sources"].items() if not v["hosted"]],
    )
    if to_hub:
        result.tag = (uploader or upload_hf)(out, dest, release)
    return result


def upload_files(out: Path) -> list[Path]:
    """What goes to the Hub: everything staged but the manual checklist."""
    return sorted(p for p in out.rglob("*") if p.is_file() and p.name != CHECKLIST)


def _tagged_release(api: Any, repo_id: str, tag: str) -> str | None:
    """The release_id in manifest.json at `tag`, or None when the tagged commit has none."""
    from huggingface_hub.errors import EntryNotFoundError

    try:
        path = api.hf_hub_download(repo_id, "manifest.json", repo_type="dataset", revision=tag)
    except EntryNotFoundError:
        return None
    rid = json.loads(Path(path).read_text(encoding="utf-8")).get("release_id")
    return None if rid is None else str(rid)


def upload_hf(out: Path, dest: str, release: dict[str, Any], api: Any = None) -> str:
    """One commit with the release, then the tag release-<run date> on it (coffee-data-tracker's _upload_hf).

    The token is read only from HF_TOKEN at run time. The tag names the release served for that run date, so a
    corrected export of the same run moves it; a re-export of the same release leaves it where it is.
    """
    from huggingface_hub import CommitOperationAdd, HfApi

    repo_id = hf_repo_id(dest)
    api = api or HfApi(token=os.environ.get("HF_TOKEN"))
    api.create_repo(repo_id, repo_type="dataset", private=False, exist_ok=True)
    ops = [
        CommitOperationAdd(path_in_repo=p.relative_to(out).as_posix(), path_or_fileobj=str(p))
        for p in upload_files(out)
    ]
    rid = str(release["release_id"])
    commit = api.create_commit(
        repo_id=repo_id,
        repo_type="dataset",
        operations=ops,
        commit_message=f"Weekly release {rid} (run {release['run_date']})",
    )
    tag = release_tag(str(release["run_date"]))
    if tag in {t.name for t in api.list_repo_refs(repo_id, repo_type="dataset").tags}:
        tagged = _tagged_release(api, repo_id, tag)
        if tagged == rid:
            print(f"{repo_id}: {tag} is already on release {rid}; left as it is", file=sys.stderr)
            return tag
        print(f"{repo_id}: moving {tag} from release {tagged} to {rid}", file=sys.stderr)
        api.delete_tag(repo_id, tag=tag, repo_type="dataset")
    api.create_tag(
        repo_id,
        tag=tag,
        tag_message=f"release {rid}",
        revision=getattr(commit, "oid", None),
        repo_type="dataset",
        exist_ok=True,
    )
    return tag
