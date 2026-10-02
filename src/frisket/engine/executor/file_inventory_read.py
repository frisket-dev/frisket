"""Host admission of one bounded page, not a new file execution path."""

from __future__ import annotations

from dataclasses import dataclass

from frisket.actions.import_inventory_types import InventoryFile
from frisket.actions.types import TableError
from frisket.engine.executor.import_blob_stage import AdmittedImportBlobStager
from frisket.engine.store.blob_backend import ProjectBlobStore
from frisket.engine.store.import_inventory import ImportInventory


@dataclass(frozen=True)
class FileInventoryAdmission:
    """Internal job-bound authority; never constructed from an HTTP body.

    The host resolves the opaque ref in its project's owned import directory
    and verifies project/storage identity before opening the inventory. Every
    item must have been stored in this canonical backend before being appended.
    """

    ref: str
    inventory: ImportInventory
    owner: ProjectBlobStore
    after: int
    limit: int = 256
    byte_budget: int = 64 * 1024 * 1024


class AdmittedFileInventoryReader:
    def __init__(
        self,
        admission: FileInventoryAdmission | None = None,
        stager: AdmittedImportBlobStager | None = None,
    ) -> None:
        self.admission = admission
        self.stager = stager
        self.used = False
        self.next_cursor = admission.after if admission else 0
        self.admitted_bytes = 0
        self.complete = False
        self.facts: list[dict] = []

    def files(self, ref: str) -> tuple[InventoryFile, ...]:
        admission = self.admission
        if admission is None or self.stager is None or ref != admission.ref:
            raise TableError(
                "invalid_file_inventory",
                "File inventory requires matching host admission.",
            )
        if self.used:
            raise TableError(
                "invalid_file_inventory", "An inventory page can be read only once."
            )
        self.used = True
        page = admission.inventory.page(
            after=admission.after,
            limit=admission.limit,
            byte_budget=admission.byte_budget,
        )
        files = tuple(
            InventoryFile(
                filename=item["filename"],
                file=self.stager.admit_owned(
                    admission.owner,
                    digest=item["sha256"],
                    size=item["size"],
                    filename=item["filename"],
                    mime=item["mime"],
                ),
            )
            for item in page
        )
        self.next_cursor = page[-1]["ordinal"] if page else admission.after
        self.admitted_bytes = sum(item["size"] for item in page)
        self.complete = (
            admission.inventory.sealed
            and self.next_cursor == admission.inventory.totals()["count"]
        )
        self.facts = [
            {
                "kind": "import_inventory",
                "ref": admission.ref,
                "after": admission.after,
                "through": self.next_cursor,
                "count": len(files),
                "bytes": self.admitted_bytes,
            }
        ]
        return files
