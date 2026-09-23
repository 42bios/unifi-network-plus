"""Sensors for UniFi Network+."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, PERCENTAGE, UnitOfDataRate, UnitOfInformation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import RUNTIME_COORDINATOR
from .const import DOMAIN, MANUFACTURER
from .coordinator import UniFiNetworkPlusCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up UniFi Network+ sensors."""
    coordinator: UniFiNetworkPlusCoordinator = hass.data[DOMAIN][entry.entry_id][RUNTIME_COORDINATOR]

    entities: list[UniFiBaseSensor] = [
        WanDownloadSensor(entry, coordinator),
        WanUploadSensor(entry, coordinator),
        WanLatencySensor(entry, coordinator),
        WanPacketLossSensor(entry, coordinator),
        WanAvailabilitySensor(entry, coordinator),
        IspNameSensor(entry, coordinator),
        MonthlyUsageSensor(entry, coordinator),
        TopClientsSensor(entry, coordinator),
        ConnectedClientsSensor(entry, coordinator),
    ]

    # One channel-utilization + one tx-retries sensor per AP radio,
    # created dynamically from whatever devices report radio stats.
    if coordinator.data:
        for device in coordinator.data.devices:
            for radio in device.radios:
                entities.append(RadioChannelUtilizationSensor(entry, coordinator, device.mac, radio.radio))
                entities.append(RadioTxRetriesSensor(entry, coordinator, device.mac, radio.radio))

    async_add_entities(entities)


class UniFiBaseSensor(CoordinatorEntity[UniFiNetworkPlusCoordinator], SensorEntity):
    """Base class providing the shared (single) controller device."""

    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator) -> None:
        super().__init__(coordinator)
        self._entry = entry

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": self._entry.title,
            "manufacturer": MANUFACTURER,
            "model": "Network Controller",
        }


class WanDownloadSensor(UniFiBaseSensor):
    """Current WAN download throughput."""

    _attr_translation_key = "wan_download"
    _attr_native_unit_of_measurement = UnitOfDataRate.MEGABITS_PER_SECOND
    _attr_device_class = SensorDeviceClass.DATA_RATE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_wan_download"

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.wan.download_mbps if self.coordinator.data else None


class WanUploadSensor(UniFiBaseSensor):
    """Current WAN upload throughput."""

    _attr_translation_key = "wan_upload"
    _attr_native_unit_of_measurement = UnitOfDataRate.MEGABITS_PER_SECOND
    _attr_device_class = SensorDeviceClass.DATA_RATE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_wan_upload"

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.wan.upload_mbps if self.coordinator.data else None


class WanLatencySensor(UniFiBaseSensor):
    """Average WAN latency."""

    _attr_translation_key = "wan_latency"
    _attr_native_unit_of_measurement = "ms"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_wan_latency"

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.wan.latency_ms if self.coordinator.data else None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class WanPacketLossSensor(UniFiBaseSensor):
    """WAN packet loss percentage."""

    _attr_translation_key = "wan_packet_loss"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_wan_packet_loss"

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.wan.packet_loss_percent if self.coordinator.data else None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class WanAvailabilitySensor(UniFiBaseSensor):
    """WAN uptime-monitor availability (the controller's own ping/DNS probe
    success rate over its rolling window - the closest equivalent this
    controller exposes to a "connectivity success rate")."""

    _attr_translation_key = "wan_availability"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_wan_availability"

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.wan_health.availability_percent if self.coordinator.data else None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class IspNameSensor(UniFiBaseSensor):
    """Name of the ISP the gateway's WAN connection reports."""

    _attr_translation_key = "isp_name"
    _attr_icon = "mdi:web"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_isp_name"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.wan_health.isp_name if self.coordinator.data else None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class MonthlyUsageSensor(UniFiBaseSensor):
    """Total WAN data usage for the current calendar month."""

    _attr_translation_key = "monthly_usage"
    _attr_native_unit_of_measurement = UnitOfInformation.GIGABYTES
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_monthly_usage"

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.monthly_usage.total_gb if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        usage = self.coordinator.data.monthly_usage
        return {
            "download_gb": usage.download_gb,
            "upload_gb": usage.upload_gb,
            "days_counted": usage.days_counted,
        }


class TopClientsSensor(UniFiBaseSensor):
    """State = busiest client by traffic; attributes = full ranked list."""

    _attr_translation_key = "top_clients"
    _attr_icon = "mdi:account-multiple-check"

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_top_clients"

    @property
    def native_value(self) -> str | None:
        if not self.coordinator.data or not self.coordinator.data.top_clients:
            return None
        return self.coordinator.data.top_clients[0].name

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        return {
            "clients": [
                {
                    "name": client.name,
                    "mac": client.mac,
                    "rx_bytes": client.rx_bytes,
                    "tx_bytes": client.tx_bytes,
                    "total_bytes": client.total_bytes,
                    "signal_dbm": client.signal_dbm,
                    "is_wired": client.is_wired,
                }
                for client in self.coordinator.data.top_clients
            ]
        }


class ConnectedClientsSensor(UniFiBaseSensor):
    """Total number of currently connected clients."""

    _attr_translation_key = "connected_clients"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:devices"

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_connected_clients"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.client_count if self.coordinator.data else None


class RadioBaseSensor(UniFiBaseSensor):
    """Base for a per-AP-radio sensor.

    Unlike the controller-level sensors (grouped under the single
    "Network Controller" device via ``UniFiBaseSensor.device_info``), these
    are grouped under a device entry for the individual AP itself - so e.g.
    "Living Room AP"'s two radio sensors show up together under that AP in
    the Home Assistant UI, linked back to the controller via ``via_device``,
    the same pattern the core ``unifi`` integration uses for its own
    per-device entities.
    """

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: UniFiNetworkPlusCoordinator,
        device_mac: str,
        radio: str,
    ) -> None:
        super().__init__(entry, coordinator)
        self._device_mac = device_mac
        self._radio = radio

    def _find_radio(self):
        if not self.coordinator.data:
            return None
        for device in self.coordinator.data.devices:
            if device.mac != self._device_mac:
                continue
            for radio in device.radios:
                if radio.radio == self._radio:
                    return radio
        return None

    def _find_device(self):
        if not self.coordinator.data:
            return None
        for device in self.coordinator.data.devices:
            if device.mac == self._device_mac:
                return device
        return None

    def _find_device_name(self) -> str:
        device = self._find_device()
        return device.name if device else self._device_mac

    @property
    def device_info(self) -> dict[str, Any]:
        device = self._find_device()
        return {
            "identifiers": {(DOMAIN, f"{self._entry.entry_id}_{self._device_mac}")},
            "name": device.name if device else self._device_mac,
            "manufacturer": MANUFACTURER,
            "model": (device.model if device else None) or "UniFi Access Point",
            # HA logs a (non-fatal until 2027.8.0) deprecation warning for
            # this key in favour of "via_device_id" - which needs the
            # controller device's *registry id*, not something an entity
            # can know ahead of a device_registry lookup keyed by these
            # identifiers. Leaving the (domain, identifier) tuple form here
            # until there's a documented pattern for entities to resolve
            # that id themselves without an extra round trip on every
            # device_info access.
            "via_device": (DOMAIN, self._entry.entry_id),
        }


class RadioChannelUtilizationSensor(RadioBaseSensor):
    """Channel utilization percentage for one AP radio."""

    _attr_translation_key = "radio_channel_utilization"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: UniFiNetworkPlusCoordinator,
        device_mac: str,
        radio: str,
    ) -> None:
        super().__init__(entry, coordinator, device_mac, radio)
        self._attr_unique_id = f"{entry.entry_id}_{device_mac}_{radio}_channel_utilization"
        self._attr_translation_placeholders = {"device": self._find_device_name(), "radio": radio}

    @property
    def native_value(self) -> float | None:
        radio = self._find_radio()
        return radio.channel_utilization_percent if radio else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        radio = self._find_radio()
        if not radio:
            return {}
        return {"channel": radio.channel, "num_clients": radio.num_clients}


class RadioTxRetriesSensor(RadioBaseSensor):
    """TX retry percentage for one AP radio."""

    _attr_translation_key = "radio_tx_retries"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: UniFiNetworkPlusCoordinator,
        device_mac: str,
        radio: str,
    ) -> None:
        super().__init__(entry, coordinator, device_mac, radio)
        self._attr_unique_id = f"{entry.entry_id}_{device_mac}_{radio}_tx_retries"
        self._attr_translation_placeholders = {"device": self._find_device_name(), "radio": radio}

    @property
    def native_value(self) -> float | None:
        radio = self._find_radio()
        return radio.tx_retries_percent if radio else None
