from uwazi_property_filler.adapters.local_plugin_adapter import LocalPluginAdapter
from uwazi_property_filler.adapters.remote_http_adapter import RemoteHttpAdapter
from uwazi_property_filler.domain.extension_category import ExtensionCategory
from uwazi_property_filler.domain.extension_kind import ExtensionKind
from uwazi_property_filler.domain.extension_record import ExtensionRecord
from uwazi_property_filler.ports.extension_port import ExtensionPort
from uwazi_property_filler.ports.extension_registry_port import ExtensionRegistryPort


async def list_extensions(r: ExtensionRegistryPort) -> list[ExtensionRecord]:
    return await r.list_extensions()


async def set_enabled(r: ExtensionRegistryPort, extension_id: str, enabled: bool) -> None:
    await r.set_enabled(extension_id, enabled)


async def register(r: ExtensionRegistryPort, record: ExtensionRecord) -> None:
    await r.save_extension(record)


async def get_enabled(r: ExtensionRegistryPort, category: ExtensionCategory) -> list[ExtensionRecord]:
    return await r.get_enabled(category)


def build_runners(records: list[ExtensionRecord]) -> list[ExtensionPort]:
    runners: list[ExtensionPort] = []
    for record in records:
        if record.kind == ExtensionKind.LOCAL:
            runners.append(LocalPluginAdapter(record))
        elif record.kind == ExtensionKind.REMOTE:
            runners.append(RemoteHttpAdapter(record))
    return runners
