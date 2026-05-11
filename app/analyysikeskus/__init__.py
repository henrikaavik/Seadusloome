"""Analüüsikeskus — the legal-analysis workflow hub (#714).

This package currently exposes only a placeholder landing page; the
workflow directory and the individual analysis workflows land in the
follow-up issues (#720 landing page, #722 Normi mõjuahel, #723 EL
ülevõtt).
"""

from app.analyysikeskus.routes import register_analyysikeskus_routes

__all__ = ["register_analyysikeskus_routes"]
