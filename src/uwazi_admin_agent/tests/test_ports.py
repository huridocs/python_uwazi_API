import pytest

from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.segmentation_repository_port import SegmentationRepositoryPort


def test_entity_repository_port_is_abstract() -> None:
    with pytest.raises(TypeError):
        EntityRepositoryPort()  # type: ignore[abstract]


def test_backup_store_port_is_abstract() -> None:
    with pytest.raises(TypeError):
        BackupStorePort()  # type: ignore[abstract]


def test_segmentation_repository_port_is_abstract() -> None:
    with pytest.raises(TypeError):
        SegmentationRepositoryPort()  # type: ignore[abstract]
