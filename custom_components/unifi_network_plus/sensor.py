"""Sensors for UniFi Network+."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    PERCENTAGE,
    UnitOfDataRate,
    UnitOfInformation,
    UnitOfPower,
    UnitOfTemperature,
)
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
    """Set up UniFi Network+ sensors.

    Per-device and per-radio sensors are discovered dynamically: besides
    the initial batch (from whatever the coordinator's first refresh
    already saw), a coordinator listener keeps watching every subsequent
    poll for devices/radios not seen before (e.g. a newly adopted AP or
    switch) and adds entities for them on the fly - no Home Assistant
    restart or manual re-adding of the integration required.
    """
    coordinator: UniFiNetworkPlusCoordinator = hass.data[DOMAIN][entry.entry_id][RUNTIME_COORDINATOR]

    async_add_entities(
        [
            WanDownloadSensor(entry, coordinator),
            WanUploadSensor(entry, coordinator),
            WanLatencySensor(entry, coordinator),
            WanPacketLossSensor(entry, coordinator),
            WanAvailabilitySensor(entry, coordinator),
            Wan2AvailabilitySensor(entry, coordinator),
            WanDropsSensor(entry, coordinator),
            IspNameSensor(entry, coordinator),
            SpeedtestDownloadSensor(entry, coordinator),
            SpeedtestUploadSensor(entry, coordinator),
            SpeedtestPingSensor(entry, coordinator),
            SpeedtestLastRunSensor(entry, coordinator),
            ConnectedApsSensor(entry, coordinator),
            SwitchCountSensor(entry, coordinator),
            GuestClientsSensor(entry, coordinator),
            IotClientsSensor(entry, coordinator),
            MonthlyUsageSensor(entry, coordinator),
            TopClientsSensor(entry, coordinator),
            ConnectedClientsSensor(entry, coordinator),
        ]
    )

    known_device_macs: set[str] = set()
    known_radio_keys: set[tuple[str, str]] = set()
    known_port_keys: set[tuple[str, int]] = set()

    def _discover_new_devices() -> None:
        if not coordinator.data:
            return
        new_entities: list[UniFiBaseSensor] = []
        for device in coordinator.data.devices:
            if device.mac not in known_device_macs:
                known_device_macs.add(device.mac)
                for spec in DEVICE_METRICS:
                    if spec.device_types is None or device.device_type in spec.device_types:
                        new_entities.append(DeviceMetricSensor(entry, coordinator, device.mac, spec))
            for radio in device.radios:
                key = (device.mac, radio.radio)
                if key not in known_radio_keys:
                    known_radio_keys.add(key)
                    new_entities.append(
                        RadioChannelUtilizationSensor(entry, coordinator, device.mac, radio.radio)
                    )
                    new_entities.append(RadioTxRetriesSensor(entry, coordinator, device.mac, radio.radio))
            for port in device.ports:
                key = (device.mac, port.port_idx)
                if key not in known_port_keys:
                    known_port_keys.add(key)
                    new_entities.append(PortLinkSpeedSensor(entry, coordinator, device.mac, port.port_idx))
                    new_entities.append(PortPoePowerSensor(entry, coordinator, device.mac, port.port_idx))
        if new_entities:
            async_add_entities(new_entities)

    _discover_new_devices()
    entry.async_on_unload(coordinator.async_add_listener(_discover_new_devices))


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


class Wan2AvailabilitySensor(UniFiBaseSensor):
    """Secondary WAN (failover) uptime-monitor availability.

    Unavailable (not just 0%) when the controller reports no "WAN2" entry
    at all in ``uptime_stats`` - most single-WAN setups won't have one, so
    this entity naturally goes unavailable for them rather than reporting
    a misleading 0%.
    """

    _attr_translation_key = "wan2_availability"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_wan2_availability"

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.wan_health.wan2_availability_percent if self.coordinator.data else None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class WanDropsSensor(UniFiBaseSensor):
    """Count of WAN connection drops tracked by the controller."""

    _attr_translation_key = "wan_drops"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:lan-disconnect"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_wan_drops"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.network_health.wan_drops if self.coordinator.data else None

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


class SpeedtestDownloadSensor(UniFiBaseSensor):
    """Download speed from the controller's own periodic/manual ISP speed
    test - not a continuous live measurement, see SpeedtestLastRunSensor."""

    _attr_translation_key = "speedtest_download"
    _attr_native_unit_of_measurement = UnitOfDataRate.MEGABITS_PER_SECOND
    _attr_device_class = SensorDeviceClass.DATA_RATE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:speedometer"

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_speedtest_download"

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.network_health.speedtest_download_mbps if self.coordinator.data else None


class SpeedtestUploadSensor(UniFiBaseSensor):
    """Upload speed from the controller's own periodic/manual ISP speed test."""

    _attr_translation_key = "speedtest_upload"
    _attr_native_unit_of_measurement = UnitOfDataRate.MEGABITS_PER_SECOND
    _attr_device_class = SensorDeviceClass.DATA_RATE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:speedometer"

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_speedtest_upload"

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.network_health.speedtest_upload_mbps if self.coordinator.data else None


class SpeedtestPingSensor(UniFiBaseSensor):
    """Ping from the controller's own periodic/manual ISP speed test."""

    _attr_translation_key = "speedtest_ping"
    _attr_native_unit_of_measurement = "ms"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:speedometer"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_speedtest_ping"

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.network_health.speedtest_ping_ms if self.coordinator.data else None


class SpeedtestLastRunSensor(UniFiBaseSensor):
    """When the controller last ran its ISP speed test - use this to judge
    how stale the Speedtest Download/Upload/Ping readings are."""

    _attr_translation_key = "speedtest_last_run"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_speedtest_last_run"

    @property
    def native_value(self) -> datetime | None:
        if not self.coordinator.data:
            return None
        epoch = self.coordinator.data.network_health.speedtest_last_run
        if not epoch:
            return None
        return datetime.fromtimestamp(epoch, tz=timezone.utc)


class ConnectedApsSensor(UniFiBaseSensor):
    """Number of access points the controller considers connected."""

    _attr_translation_key = "connected_aps"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:wifi"

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_connected_aps"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.network_health.connected_aps if self.coordinator.data else None


class SwitchCountSensor(UniFiBaseSensor):
    """Number of switches the controller manages."""

    _attr_translation_key = "switch_count"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:switch"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_switch_count"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.network_health.switch_count if self.coordinator.data else None


class GuestClientsSensor(UniFiBaseSensor):
    """Number of clients currently on a guest network (wired + wireless)."""

    _attr_translation_key = "guest_clients"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:account-question"

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_guest_clients"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.network_health.guest_clients if self.coordinator.data else None


class IotClientsSensor(UniFiBaseSensor):
    """Number of clients on an IoT network (wired + wireless)."""

    _attr_translation_key = "iot_clients"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:chip"

    def __init__(self, entry: ConfigEntry, coordinator: UniFiNetworkPlusCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_iot_clients"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.network_health.iot_clients if self.coordinator.data else None


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
                    "ccq": client.ccq,
                    "essid": client.essid,
                    "channel": client.channel,
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


class DeviceBaseSensor(UniFiBaseSensor):
    """Base for a sensor grouped under one physical UniFi device's own
    Home Assistant device entry (linked back to the controller device via
    ``via_device``), rather than the shared controller device.
    """

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: UniFiNetworkPlusCoordinator,
        device_mac: str,
    ) -> None:
        super().__init__(entry, coordinator)
        self._device_mac = device_mac

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
            "model": (device.model if device else None) or "UniFi Device",
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


class RadioBaseSensor(DeviceBaseSensor):
    """Base for a per-AP-radio sensor."""

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: UniFiNetworkPlusCoordinator,
        device_mac: str,
        radio: str,
    ) -> None:
        super().__init__(entry, coordinator, device_mac)
        self._radio = radio

    def _find_radio(self):
        device = self._find_device()
        if not device:
            return None
        for radio in device.radios:
            if radio.radio == self._radio:
                return radio
        return None


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
        self._attr_translation_placeholders = {"radio": radio}

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
        self._attr_translation_placeholders = {"radio": radio}

    @property
    def native_value(self) -> float | None:
        radio = self._find_radio()
        return radio.tx_retries_percent if radio else None


@dataclass(frozen=True)
class DeviceMetricSpec:
    """Declarative spec for one per-device metric sensor.

    ``key`` names the matching attribute on ``parsing.DeviceInfo``.
    ``device_types`` restricts which UniFi device types (``"uap"``,
    ``"usw"``, ``"uxg"``) get this sensor - ``None`` means all types.
    """

    key: str
    translation_key: str
    unit: str | None
    device_class: SensorDeviceClass | None
    icon: str | None
    device_types: tuple[str, ...] | None


DEVICE_METRICS: list[DeviceMetricSpec] = [
    DeviceMetricSpec("cpu_percent", "device_cpu", PERCENTAGE, None, "mdi:chip", None),
    DeviceMetricSpec("memory_percent", "device_memory", PERCENTAGE, None, "mdi:memory", None),
    DeviceMetricSpec("client_count", "device_clients", None, None, "mdi:devices", None),
    DeviceMetricSpec(
        "satisfaction_percent", "device_satisfaction", PERCENTAGE, None,
        "mdi:emoticon-happy-outline", ("uap", "usw"),
    ),
    DeviceMetricSpec(
        "cpu_temp_celsius", "device_cpu_temp", UnitOfTemperature.CELSIUS,
        SensorDeviceClass.TEMPERATURE, None, ("uxg",),
    ),
    DeviceMetricSpec("storage_percent", "device_storage", PERCENTAGE, None, "mdi:harddisk", ("uxg",)),
    DeviceMetricSpec(
        "poe_power_watts", "device_poe_power", UnitOfPower.WATT,
        SensorDeviceClass.POWER, None, ("usw",),
    ),
    DeviceMetricSpec("active_ports", "device_active_ports", None, None, "mdi:ethernet", ("usw",)),
]


class DeviceMetricSensor(DeviceBaseSensor):
    """One per-device metric sensor, built from a ``DeviceMetricSpec``.

    A single parametrized class instead of one hand-written class per
    metric (CPU/RAM/temperature/storage/PoE/...) since they all share the
    same "look up this device, read one attribute off it" shape.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: UniFiNetworkPlusCoordinator,
        device_mac: str,
        spec: DeviceMetricSpec,
    ) -> None:
        super().__init__(entry, coordinator, device_mac)
        self._spec = spec
        self._attr_unique_id = f"{entry.entry_id}_{device_mac}_{spec.key}"
        self._attr_translation_key = spec.translation_key
        if spec.unit is not None:
            self._attr_native_unit_of_measurement = spec.unit
        if spec.device_class is not None:
            self._attr_device_class = spec.device_class
        if spec.icon is not None:
            self._attr_icon = spec.icon

    @property
    def native_value(self) -> Any:
        device = self._find_device()
        return getattr(device, self._spec.key) if device else None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class PortBaseSensor(DeviceBaseSensor):
    """Base for a per-switch-port sensor.

    Disabled by default (``_attr_entity_registry_enabled_default = False``):
    a 48-port switch would otherwise add dozens of near-identical entities
    most users never look at individually - the aggregated PoE Power/Active
    Ports sensors on the switch device itself (see DEVICE_METRICS) cover the
    common case. Enable specific ports from Settings -> Entities if you want
    them (e.g. to graph one particular port's PoE draw over time).
    """

    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: UniFiNetworkPlusCoordinator,
        device_mac: str,
        port_idx: int,
    ) -> None:
        super().__init__(entry, coordinator, device_mac)
        self._port_idx = port_idx

    def _find_port(self):
        device = self._find_device()
        if not device:
            return None
        for port in device.ports:
            if port.port_idx == self._port_idx:
                return port
        return None


class PortLinkSpeedSensor(PortBaseSensor):
    """Negotiated link speed for one switch port."""

    _attr_translation_key = "port_link_speed"
    _attr_native_unit_of_measurement = UnitOfDataRate.MEGABITS_PER_SECOND
    _attr_device_class = SensorDeviceClass.DATA_RATE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: UniFiNetworkPlusCoordinator,
        device_mac: str,
        port_idx: int,
    ) -> None:
        super().__init__(entry, coordinator, device_mac, port_idx)
        self._attr_unique_id = f"{entry.entry_id}_{device_mac}_port{port_idx}_link_speed"
        port = self._find_port()
        self._attr_translation_placeholders = {"port": port.name if port else f"Port {port_idx}"}

    @property
    def native_value(self) -> float | None:
        port = self._find_port()
        if not port or not port.is_up:
            return None
        return port.speed_mbps

    @property
    def available(self) -> bool:
        port = self._find_port()
        return super().available and port is not None


class PortPoePowerSensor(PortBaseSensor):
    """PoE power draw for one switch port."""

    _attr_translation_key = "port_poe_power"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: UniFiNetworkPlusCoordinator,
        device_mac: str,
        port_idx: int,
    ) -> None:
        super().__init__(entry, coordinator, device_mac, port_idx)
        self._attr_unique_id = f"{entry.entry_id}_{device_mac}_port{port_idx}_poe_power"
        port = self._find_port()
        self._attr_translation_placeholders = {"port": port.name if port else f"Port {port_idx}"}

    @property
    def native_value(self) -> float | None:
        port = self._find_port()
        return port.poe_power_watts if port else None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None
