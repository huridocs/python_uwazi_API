from uwazi_property_filler.domain.fill_status import FillStatus
from uwazi_property_filler.domain.pdf_item import PdfItem
from uwazi_property_filler.ports.document_store_port import DocumentStorePort
from uwazi_property_filler.ports.uwazi_port import UwaziPort


async def list_pdfs(
    u: UwaziPort,
    s: DocumentStorePort,
    instance_key: str,
    template: str,
    language: str,
    filter_value: str | None = None,
) -> tuple[list[PdfItem], list[PdfItem]]:
    """Fetch the template's documents, upsert their status rows, return them
    split into (pending, validated)."""
    items = await u.list_documents(template, language, filter_value)
    for item in items:
        await s.upsert_status(instance_key, item)
    shared_ids = {item.shared_id for item in items}
    pending = [it for it in await s.list_by_status(instance_key, FillStatus.PENDING, language) if it.shared_id in shared_ids]
    validated = [
        it for it in await s.list_by_status(instance_key, FillStatus.VALIDATED, language) if it.shared_id in shared_ids
    ]
    return pending, validated
