"""Public sidecar error categories shared by engine adapters and HTTP routes."""


class InvalidDocumentError(ValueError):
    """Submitted bytes are not a safely decodable supported document."""
