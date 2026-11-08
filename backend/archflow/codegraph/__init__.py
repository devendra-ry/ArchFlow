"""Repository analysis: code graph → reconstructed architecture, drift and change impact.

Layout (only ``adapter.py`` imports Graphify; everything else works on ArchFlow's own normalized format):

* ``adapter.py``   — runs Graphify (tree-sitter extraction, graph build, Leiden communities, god nodes, import
                     cycles) and converts its output into the normalized graph below. Optional dependency:
                     ``uv sync --extra codegraph`` (``graphifyy==0.9.77``).
* ``detect.py``    — deterministic, dependency-free detections: languages, manifests, docker-compose,
                     Dockerfiles, Kubernetes, Terraform, infrastructure from imported libraries, entry points.
* ``worker.py``    — ``python -m archflow.codegraph.worker <repo_dir> <out.json>``: the analysis always runs in a
                     separate process (never inside the API process), with a timeout.
* ``cli.py``       — ``archflow-codegraph <folder> [-o out.json]``: analyse locally and upload only the graph.
* ``normalize.py`` — validation of normalized graphs (also uploaded ones), summaries, (de)compression.
* ``sources.py``   — public Git URL validation (SSRF guard), shallow clone with time and size limits; local dirs.
* ``jobs.py``      — background job runner; status in scale.job_store() (in-memory, or Redis when configured).
* ``reconstruct.py`` / ``drift.py`` / ``impact.py`` — proposed HLD, designed-vs-code drift, change impact.
* ``service.py``   — REST routes and the functions shared with the MCP tools.

Normalized graph (``format: archflow.codegraph/v1``)::

    {format, generator: {tool, version}, root_name,
     files:   [{path, language, symbols}],
     symbols: [{id, name, file, line, kind (file|class|function|method|symbol), community}],
     edges:   [{source, target, relation, confidence}],          # both ends are symbols
     communities: [{id, size, label, paths: [top dirs]}],
     detections: {languages, manifests, compose_services (merged across compose files), dockerfiles, k8s,
                  terraform, proxy_routes, infrastructure, frameworks, entry_points},
     analysis: {god_nodes: [{id, name, degree}], import_cycles: [[file, ...]]},
     stats: {files, symbols, edges, communities}}
"""

from __future__ import annotations

import importlib.util

FORMAT = "archflow.codegraph/v1"
GRAPHIFY_PIN = "0.9.77"
INSTALL_HINT = ("Repository analysis needs the optional 'codegraph' extra (Graphify). Install it with "
                "`uv sync --extra codegraph` in backend/ and restart the API. You can still upload a graph made "
                "with the `archflow-codegraph` CLI.")


def available() -> bool:
    """True when Graphify (the analysis engine) is installed. Checked without importing it."""
    try:
        return importlib.util.find_spec("graphify") is not None
    except (ImportError, ValueError):
        return False
