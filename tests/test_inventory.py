from __future__ import annotations

import pytest
import pytest_asyncio

from episode.config import EpisodeConfig
from episode.domain.models import Area, Device, Event
from episode.inventory import InventoryConflictError, InventoryService
from episode.storage.repository import Repository


@pytest_asyncio.fixture
async def inventory(tmp_path):
    repository = Repository(EpisodeConfig(data_dir=str(tmp_path)))
    await repository.initialize()
    service = InventoryService(repository)
    try:
        yield repository, service
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_archived_inventory_is_retained_but_excluded_from_runtime_lists(inventory):
    repository, service = inventory
    await service.save_area(Area(id="gate", name="Gate"), create=True)
    await service.save_device(
        Device(id="camera", name="Camera", device_type="camera", area_id="gate"),
        create=True,
    )

    device = await repository.get_device("camera")
    device.enabled = False
    await service.save_device(device, create=False)

    assert await repository.list_devices() == []
    assert [item.id for item in await repository.list_devices(include_disabled=True)] == ["camera"]
    assert (await repository.get_device("camera")).enabled is False


@pytest.mark.asyncio
async def test_inventory_prevents_unsafe_deletion_and_duplicate_addresses(inventory):
    repository, service = inventory
    await service.save_area(Area(id="gate", name="Gate"), create=True)
    await service.save_device(
        Device(
            id="camera",
            name="Camera",
            device_type="camera",
            area_id="gate",
            ip_address="192.0.2.10",
        ),
        create=True,
    )

    with pytest.raises(InventoryConflictError, match="already used"):
        await service.save_device(
            Device(
                id="camera-two",
                name="Camera two",
                device_type="camera",
                area_id="gate",
                ip_address="192.0.2.10",
            ),
            create=True,
        )

    await repository.create_event(Event(device_id="camera", area_id="gate", event_type="motion"))
    with pytest.raises(InventoryConflictError, match="history"):
        await service.delete_device("camera")
    with pytest.raises(InventoryConflictError, match="Devices or incident history"):
        await service.delete_area("gate")
