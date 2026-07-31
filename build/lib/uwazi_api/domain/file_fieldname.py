from enum import StrEnum


class FileFieldname(StrEnum):
    """Allowed multipart field names for entity file uploads.

    Uwazi's ``UploadMiddleware.multiple()`` classifies each file based on
    its field name:

    * ``FILE`` or ``DOCUMENT`` → classified as **primary document**
      (the fieldname contains ``"file"`` or ``"document"``).
    * ``ATTACHMENT`` → classified as **supporting file** (attachment);
      any value that is neither ``FILE`` nor ``DOCUMENT`` works.
    """

    FILE = "file"
    """Multipart field name for a primary document."""

    DOCUMENT = "document"
    """Multipart field name for a primary document (alternative)."""

    ATTACHMENT = "attachment"
    """Multipart field name for a supporting file (attachment)."""
