"""frisket doctor subpackage — deterministic self-probes that report, never
crash. ``embeddings`` backs the ``frisket doctor embeddings`` subcommand."""

from frisket.operability.doctor.embeddings import report_embeddings

__all__ = ["report_embeddings"]
