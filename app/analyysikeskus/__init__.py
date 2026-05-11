"""Analüüsikeskus — the legal-analysis workflow hub (#714).

Exposes the workflow directory (#720), the reusable analysis-result
shell (#721), and two stub workflows — ``Normi mõjuahel`` (#722 fills it
in) and ``EL ülevõtt ja harmoneerimine`` (#723 fills it in). The other
six Section-7 workflows are deferred to a follow-up epic.
"""

from app.analyysikeskus.result_shell import analysis_result_shell
from app.analyysikeskus.routes import register_analyysikeskus_routes

__all__ = ["analysis_result_shell", "register_analyysikeskus_routes"]
