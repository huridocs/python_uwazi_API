from abc import ABC, abstractmethod

from uwazi_api.domain.segmentation import Segmentation


class SegmentationRepositoryPort(ABC):
    """Read-only access to Uwazi's document segmentation (§ segmentation).

    A thin seam over ``uwazi_api``'s :class:`SegmentationRepository` /
    :class:`FileService` so the generation agent can read the structured text of
    a primary document (paragraphs with page number, bounding box, and text)
    before authoring extraction logic over it. Implementations return the
    validated :class:`Segmentation` model — the parsed, page-ordered paragraphs —
    never raw bytes.

    Async by signature (matching :class:`EntityRepositoryPort` /
    :class:`FileRepositoryPort`); the underlying ``requests`` calls are
    synchronous. No ``uwazi_api`` change: adapters import and delegate.
    """

    @abstractmethod
    async def get_by_file_id(self, file_id: str) -> Segmentation:
        """Fetch one file's segmentation by its ``_id`` (``GET /api/v2/files/{file_id}/segmentation``).

        ``file_id`` is the document ``_id`` that ``peek_entity_files`` returns
        for kind=document entries. Raises :class:`SegmentationNotFoundError` when
        the segmentation feature is disabled, the file is not a document, or no
        ready segmentation exists yet, and :class:`SegmentationError` for other
        failures (e.g. a 401 without admin credentials).
        """
        ...

    @abstractmethod
    async def get_by_shared_id(self, shared_id: str, language: str) -> Segmentation:
        """Resolve an entity's primary document for ``language`` and return its segmentation.

        Convenience for the common case where the agent works from a ``shared_id``
        (via ``query_entities``) rather than a raw file ``_id``. ``language`` is
        the entity row locale (ISO 639-1, e.g. ``"en"``); the service maps it to
        the document's file language and looks up the matching primary document.
        Raises :class:`SegmentationNotFoundError` when the language is unsupported
        or the entity has no document for it.
        """
        ...
