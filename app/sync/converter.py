"""Convert ontology repo JSON-LD files to RDF/Turtle using rdflib."""

import json
import logging
from pathlib import Path

from rdflib import Graph

logger = logging.getLogger(__name__)

DOMAINS = {
    "riigiteataja": "Enacted laws (Riigi Teataja)",
    "eelnoud": "Draft legislation (eelnõud)",
    "riigikohus": "Supreme Court decisions (Riigikohus)",
    "curia": "EU Court decisions (Curia)",
    "eurlex": "EU legislation (EUR-Lex)",
}


def load_index(repo_path: Path) -> list[dict]:  # type: ignore[type-arg]
    """Load INDEX.json and return the list of law entries."""
    index_path = repo_path / "krr_outputs" / "INDEX.json"
    if not index_path.exists():
        raise FileNotFoundError(f"INDEX.json not found at {index_path}")
    with open(index_path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("laws", [])  # type: ignore[no-any-return]


def parse_jsonld_file(path: Path) -> Graph:
    """Parse a single JSON-LD file into an rdflib Graph."""
    g = Graph()
    g.parse(str(path), format="json-ld")
    return g


def convert_ontology(repo_path: Path) -> Graph:
    """Convert all ontology JSON-LD files to a single merged RDF graph.

    Reads:
    - combined_ontology.jsonld (main unified graph)
    - Individual _peep.json files from krr_outputs/
    - Domain-specific files from subdirectories (eelnoud, riigikohus, curia, eurlex)

    Returns a merged rdflib Graph.
    """
    krr_path = repo_path / "krr_outputs"
    if not krr_path.exists():
        raise FileNotFoundError(f"krr_outputs not found at {krr_path}")

    merged = Graph()
    entity_counts: dict[str, int] = {}

    # 1. Parse combined_ontology.jsonld (contains all enacted law data)
    combined_path = krr_path / "combined_ontology.jsonld"
    if combined_path.exists():
        logger.info("Parsing combined_ontology.jsonld...")
        g = parse_jsonld_file(combined_path)
        count = len(g)
        merged += g
        entity_counts["combined_ontology"] = count
        logger.info("  %d triples from combined_ontology.jsonld", count)
    else:
        logger.warning("combined_ontology.jsonld not found, parsing individual files")
        # Fallback: parse individual _peep.json files
        peep_files = sorted(krr_path.glob("*_peep.json"))
        total = 0
        for peep_file in peep_files:
            try:
                g = parse_jsonld_file(peep_file)
                merged += g
                total += len(g)
            except Exception:
                logger.exception("Failed to parse %s", peep_file.name)
        entity_counts["enacted_laws"] = total
        logger.info("  %d triples from %d individual _peep.json files", total, len(peep_files))

    # 2. Parse domain-specific subdirectories
    for domain_dir, domain_name in DOMAINS.items():
        domain_path = krr_path / domain_dir
        if not domain_path.exists():
            logger.info("Skipping %s (directory not found)", domain_name)
            continue

        jsonld_files = sorted(domain_path.glob("*.json")) + sorted(domain_path.glob("*.jsonld"))
        if not jsonld_files:
            logger.info("Skipping %s (no JSON-LD files)", domain_name)
            continue

        domain_count = 0
        for path in jsonld_files:
            try:
                g = parse_jsonld_file(path)
                merged += g
                domain_count += len(g)
            except Exception:
                logger.exception("Failed to parse %s/%s", domain_dir, path.name)

        entity_counts[domain_dir] = domain_count
        logger.info(
            "  %d triples from %s (%d files)", domain_count, domain_name, len(jsonld_files)
        )

    total_triples = len(merged)
    logger.info("Total: %d triples across %d sources", total_triples, len(entity_counts))

    for source, count in sorted(entity_counts.items()):
        logger.info("  %s: %d triples", source, count)

    return merged


def serialize_to_turtle(graph: Graph) -> str:
    """Serialize an rdflib Graph to Turtle format."""
    return graph.serialize(format="turtle")
