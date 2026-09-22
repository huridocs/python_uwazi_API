from uwazi_property_filler.ports.pdf_cache_port import PdfCachePort
from uwazi_property_filler.ports.uwazi_port import UwaziPort


async def refresh(
    u: UwaziPort,
    c: PdfCachePort,
    template: str,
    language: str,
    filter_value: str | None = None,
) -> int:
    """List the template's documents and cache each one's PDF bytes.

    Returns the number of PDFs cached. This is the "Connect & Refresh" action.
    """
    items = await u.list_documents(template, language, filter_value)
    count = 0
    for item in items:
        data = await u.get_pdf_bytes(item.filename)
        if data is None:
            continue
        await c.put(item.filename, data)
        count += 1
    return count
