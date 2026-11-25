"""The registered adapter types (ADR 0016): each a generic reader plus its declared capabilities.

The set is closed: a new type is a new ADR, a new entry here and in source_config.TYPE_NAMES (a test checks they
agree). Existing readers keep their own module functions; the entries below only adapt them to ``Read``.
"""

from __future__ import annotations

from collections.abc import Iterator

from health_data_registry.adapters import DOCUMENT_FIELDS, AdapterType, Page, Read, register
from health_data_registry.source_config import CONFIG_MODELS


def _socrata(read: Read) -> Iterator[Page]:
    from health_data_registry.adapters import socrata

    return socrata.pages(read.entry, read.get, max_rows=read.max_rows, fields=read.fields,
                         page_size=read.config.page_size)  # fmt: skip


def _odata(read: Read) -> Iterator[Page]:
    from health_data_registry.adapters import odata

    return odata.pages(read.entry, read.get, max_rows=read.max_rows, max_pages=read.config.max_pages)


def _https_csv(read: Read) -> Iterator[Page]:
    from health_data_registry.adapters import https_csv

    return https_csv.pages(read.entry, read.get, max_rows=read.max_rows, fields=read.fields)


def _tableau(read: Read) -> Iterator[Page]:
    from health_data_registry.adapters import tableau

    return tableau.pages(read.entry, read.get, max_rows=read.max_rows, fields=read.fields)


def _lazy(module: str, name: str):  # type: ignore[no-untyped-def]
    def call(*args, **kwargs):  # type: ignore[no-untyped-def]
        import importlib

        return getattr(importlib.import_module(f"health_data_registry.adapters.{module}"), name)(
            *args, **kwargs
        )

    return call


register(
    AdapterType(
        "socrata", CONFIG_MODELS["socrata"], _socrata, _lazy("socrata", "publisher_fields"),
        own_paths=lambda entry: [f"/api/views/{entry.source.dataset}.json"],
    )
)  # fmt: skip
register(AdapterType("odata", CONFIG_MODELS["odata"], _odata, _lazy("odata", "publisher_fields")))
register(
    AdapterType("https_csv", CONFIG_MODELS["https_csv"], _https_csv, _lazy("https_csv", "publisher_fields"))
)
register(
    AdapterType(
        "https_file", CONFIG_MODELS["https_file"], _lazy("https_file", "pages"), _lazy("https_file", "publisher_fields"),
        accept="text/csv, application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, application/zip, */*;q=0.5",
        adds_fields=DOCUMENT_FIELDS,
    )
)  # fmt: skip
register(
    AdapterType(
        "html_table", CONFIG_MODELS["html_table"], _lazy("html_table", "pages"), _lazy("html_table", "publisher_fields"),
        accept="text/html, application/xhtml+xml;q=0.9", follows_redirects=False, adds_fields=DOCUMENT_FIELDS,
    )
)  # fmt: skip
register(
    AdapterType(
        "pdf_table", CONFIG_MODELS["pdf_table"], _lazy("pdf_table", "pages"), _lazy("pdf_table", "publisher_fields"),
        accept="text/html, application/pdf;q=0.9", adds_fields=DOCUMENT_FIELDS,
    )
)  # fmt: skip
register(
    AdapterType(
        "tableau", CONFIG_MODELS["tableau"], _tableau, _lazy("tableau", "publisher_fields"),
        browser_capture=("/bootstrapSession/",),  # the answer that carries a view's data
    )
)  # fmt: skip
register(
    AdapterType(
        "doi", CONFIG_MODELS["doi"], None, None, checks_layout_on_read=True, runner="harvest",
        host_is_publisher=False, build_command="health-data-registry harvest-doi {conference} --year <year>",
    )
)  # fmt: skip
