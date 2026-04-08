"""SHACL validation for RDF data against ontology shapes."""

import logging
from pathlib import Path

from pyshacl import validate as shacl_validate
from rdflib import Graph

logger = logging.getLogger(__name__)


def load_shapes(shapes_dir: Path) -> Graph:
    """Load all SHACL shape files from a directory into a single graph."""
    shapes = Graph()
    shape_files = sorted(shapes_dir.glob("*.ttl")) + sorted(shapes_dir.glob("*.jsonld"))

    for shape_file in shape_files:
        fmt = "json-ld" if shape_file.suffix == ".jsonld" else "turtle"
        try:
            shapes.parse(str(shape_file), format=fmt)
            logger.info("Loaded shape: %s", shape_file.name)
        except Exception:
            logger.exception("Failed to load shape: %s", shape_file.name)

    logger.info("Loaded %d shape triples from %d files", len(shapes), len(shape_files))
    return shapes


def validate_graph(data_graph: Graph, shapes_graph: Graph) -> tuple[bool, str]:
    """Validate an RDF graph against SHACL shapes.

    Returns:
        Tuple of (conforms: bool, report_text: str).
    """
    conforms, _results_graph, results_text = shacl_validate(
        data_graph,
        shacl_graph=shapes_graph,
        inference="none",
        abort_on_first=False,
    )

    if conforms:
        logger.info("SHACL validation passed")
    else:
        logger.warning("SHACL validation failed:\n%s", results_text)

    return bool(conforms), str(results_text)
