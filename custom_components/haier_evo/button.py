import weakref
from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from .const import DOMAIN
from . import api


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities) -> bool:
    haier_object = hass.data[DOMAIN][config_entry.entry_id]
    entities = []
    for device in haier_object.devices:
        entities.extend(device.create_entities_button())
    if entities:
        async_add_entities(entities)
        haier_object.write_ha_state()
    return True


class HaierButton(ButtonEntity):
    _attr_should_poll = False

    def __init__(self, device: api.HaierDevice) -> None:
        self._device = weakref.proxy(device)

    @property
    def device_info(self) -> dict:
        return self._device.device_info


class HaierWMStartButton(HaierButton):
    _attr_icon = "mdi:play-circle"

    def __init__(self, device: api.HaierWM) -> None:
        super().__init__(device)
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_start"
        self._attr_name = f"{device.device_name} Старт"
        self._device = device

    async def async_press(self) -> None:
        await self.hass.async_add_executor_job(self._device.start_program)


class HaierWMPauseButton(HaierButton):
    _attr_icon = "mdi:pause-circle"

    def __init__(self, device: api.HaierWM) -> None:
        super().__init__(device)
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_pause"
        self._attr_name = f"{device.device_name} Пауза"
        self._device = device

    async def async_press(self) -> None:
        await self.hass.async_add_executor_job(self._device.pause_program)


class HaierWMCancelButton(HaierButton):
    _attr_icon = "mdi:stop-circle"

    def __init__(self, device: api.HaierWM) -> None:
        super().__init__(device)
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_cancel"
        self._attr_name = f"{device.device_name} Отмена"
        self._device = device

    async def async_press(self) -> None:
        await self.hass.async_add_executor_job(self._device.cancel_program)


