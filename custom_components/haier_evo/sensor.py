import weakref
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.const import UnitOfTemperature
from homeassistant.const import TEMPERATURE
from .const import DOMAIN
from . import api
from .logger import _LOGGER


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities) -> bool:
    haier_object = hass.data[DOMAIN][config_entry.entry_id]
    entities = []
    for device in haier_object.devices:
        entities.extend(device.create_entities_sensor())
    _LOGGER.debug(f"sensor: {len(entities)} entities created")
    if entities:
        async_add_entities(entities)
        haier_object.write_ha_state()
    return True


class HaierSensor(SensorEntity):

    def __init__(self, device: api.HaierDevice):
        self._device = weakref.proxy(device)
        self._device_attr_name = None

        device.add_write_ha_state_callback(self.async_write_ha_state)

    @property
    def device_info(self) -> dict:
        return self._device.device_info

    @property
    def available(self) -> bool:
        return self._device.available

    @property
    def native_value(self) -> float:
        return getattr(self._device, self._device_attr_name, 0.0)


class HaierREFTemperatureSensor(HaierSensor):
    _attr_device_class = TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, device: api.HaierREF):
        super().__init__(device)
        self._device_attr_name = "current_temperature"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_temperature"
        self._attr_name = f"{device.device_name} Температура в помещении"


class HaierREFFridgeTemperatureSensor(HaierREFTemperatureSensor):

    def __init__(self, device: api.HaierREF):
        super().__init__(device)
        self._device_attr_name = "current_fridge_temperature"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_fridge_temperature"
        self._attr_name = f"{device.device_name} Температура холодильной камеры"


class HaierREFFreezerTemperatureSensor(HaierREFTemperatureSensor):

    def __init__(self, device: api.HaierREF):
        super().__init__(device)
        self._device_attr_name = "current_freezer_temperature"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_freezer_temperature"
        self._attr_name = f"{device.device_name} Температура морозильной камеры"


class HaierREFFridgeModeSensor(HaierREFTemperatureSensor):

    def __init__(self, device: api.HaierREF):
        super().__init__(device)
        self._device_attr_name = "fridge_mode"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_fridge_mode"
        self._attr_name = f"{device.device_name} Режим холодильной камеры"

    @property
    def native_value(self) -> float:
        return float(getattr(self._device, self._device_attr_name, 0.0))


class HaierREFFreezerModeSensor(HaierREFFridgeModeSensor):

    def __init__(self, device: api.HaierREF):
        super().__init__(device)
        self._device_attr_name = "freezer_mode"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_freezer_mode"
        self._attr_name = f"{device.device_name} Режим морозильной камеры"


class HaierWMRemainingTimeSensor(HaierSensor):
    _attr_icon = "mdi:timer-outline"

    def __init__(self, device: api.HaierWM):
        super().__init__(device)
        self._device_attr_name = "remaining_total_minutes"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_remaining_time"
        self._attr_name = f"{device.device_name} Осталось (мин)"

    @property
    def native_value(self) -> float:
        return getattr(self._device, self._device_attr_name, 0)


class HaierWMStatusSensor(HaierSensor):
    _attr_icon = "mdi:washing-machine"

    def __init__(self, device: api.HaierWM):
        super().__init__(device)
        self._device_attr_name = "status_text"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_status"
        self._attr_name = f"{device.device_name} Статус"

    @property
    def native_value(self) -> float:
        return getattr(self._device, self._device_attr_name, "idle")


class HaierWMProgramDurationSensor(HaierSensor):
    _attr_icon = "mdi:clock-outline"

    def __init__(self, device: api.HaierWM):
        super().__init__(device)
        self._device_attr_name = "program_duration"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_program_duration"
        self._attr_name = f"{device.device_name} Длительность программы (мин)"
