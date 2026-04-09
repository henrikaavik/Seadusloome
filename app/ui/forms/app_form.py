"""``AppForm`` — urlencoded default form for non-upload pages.

FastHTML's ``Form`` defaults to ``enctype="multipart/form-data"`` because
that lets file uploads work without ceremony, but it makes every other
form heavier on the wire and surprises tools that assume the standard
``application/x-www-form-urlencoded`` body. ``AppForm`` flips the default
so the urlencoded encoding is the norm; explicit ``Form()`` (or
``AppForm(enctype="multipart/form-data")``) should still be used for
file-upload pages.
"""

from fasthtml.common import Form as _Form


def AppForm(*c, **kw):  # noqa: N802 - PascalCase matches FastHTML FT builders
    """Form with ``application/x-www-form-urlencoded`` as the default encoding.

    Use the regular FastHTML ``Form()`` directly when you need
    ``multipart/form-data`` for file uploads.
    """
    kw.setdefault("enctype", "application/x-www-form-urlencoded")
    return _Form(*c, **kw)
