"""Operational CLI scripts (migrations, sync, standalone worker, etc.).

These modules are deliberately kept free of FastHTML / Starlette
imports so they can run in lightweight containers (e.g. the
``WORKER_MODE=standalone`` worker container introduced in #348) without
dragging in the full web framework dependency graph.
"""
