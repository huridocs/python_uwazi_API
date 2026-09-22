from typing import Any

from uwazi_property_filler.domain.fill_audit import FillAuditRecord
from uwazi_property_filler.ports.audit_store_port import AuditStorePort
from uwazi_property_filler.ports.document_store_port import DocumentStorePort
from uwazi_property_filler.ports.uwazi_port import UwaziPort


async def validate_fill(
    u: UwaziPort,
    s: DocumentStorePort,
    a: AuditStorePort,
    instance_key: str,
    shared_id: str,
    template_name: str,
    language: str,
    metadata: dict[str, Any],
    extension_ids: list[str],
    before: dict[str, Any],
) -> None:
    """Write metadata to Uwazi; on success mark validated + audit per property.

    If ``update_metadata`` raises, the error propagates and the status stays
    pending (the UI toasts the error).
    """
    await u.update_metadata(shared_id, template_name, language, metadata)
    await s.mark_validated(instance_key, shared_id, language, metadata)
    for prop, after_value in metadata.items():
        await a.save_audit(
            FillAuditRecord(
                instance_key=instance_key,
                shared_id=shared_id,
                property_name=prop,
                before_value=before.get(prop),
                after_value=after_value,
                extension_ids=extension_ids,
            )
        )
