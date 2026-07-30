from pydantic import BaseModel

from uwazi_api.domain.file_fieldname import FileFieldname
from uwazi_api.domain.FileType import FileType


class EntityFileUpload(BaseModel):
    """A file to upload alongside an Entity in a single multipart request.

    The ``fieldname`` determines how Uwazi classifies the file:

    * ``FileFieldname.FILE`` or ``FileFieldname.DOCUMENT`` → **primary document**
    * ``FileFieldname.ATTACHMENT`` → **supporting file** (attachment)

    The ``filename`` is sent in the multipart ``Content-Disposition`` header
    and becomes the original filename stored by Uwazi.  For non-ASCII names,
    prefer to also send an ``originalname`` body field (not yet supported
    by this model).

    .. code-block:: python

        file = EntityFileUpload(
            fieldname=FileFieldname.FILE,         # → primary document
            filename="report.pdf",
            content=pdf_bytes,
            content_type=FileType.PDF,
        )
        supporting = EntityFileUpload(
            fieldname=FileFieldname.ATTACHMENT,   # → supporting file
            filename="appendix.pdf",
            content=other_pdf_bytes,
            content_type=FileType.PDF,
        )
    """

    fieldname: FileFieldname = FileFieldname.FILE
    """Multipart field name. ``FILE``/``DOCUMENT`` → primary document;
    ``ATTACHMENT`` → supporting file."""

    filename: str = ""
    """Original filename sent in the ``Content-Disposition`` header."""

    content: bytes
    """Raw file bytes."""

    content_type: FileType = FileType.BIN
    """MIME type of the file content."""
