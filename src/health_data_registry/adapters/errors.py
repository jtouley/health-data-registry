"""The one layout refusal every adapter type, extension and reader subclasses (ADR 0016).

Its own module, with no imports, so pure code (the named extensions, held to an import allowlist) can raise it
too, and the server and the CLI give one plain-words refusal for all.
"""

from __future__ import annotations


class LayoutError(ValueError):
    """The publisher's answer is not laid out as the catalog entry describes; nothing is guessed.

    Every type's layout error subclasses this, so the server and the CLI give one plain-words refusal for all.
    """

    code = "layout_changed"
    next_step = (
        "The publisher changed how this source is laid out, so it was not read. Use the source's own page for now; "
        "a maintainer reviews the change before it is read again."
    )
