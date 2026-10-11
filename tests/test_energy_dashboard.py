"""Dashboard contract tests runnable without a Home Assistant installation.

Run: python -m unittest discover -s tests -v
These mock the HA manager/registry, not a live recorder or frontend.
"""
import copy
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "wattelse"


def load_dashboard():
    modules = {name: types.ModuleType(name) for name in (
        "homeassistant", "homeassistant.core", "homeassistant.helpers",
        "homeassistant.helpers.entity_registry", "wattelse",
    )}
    modules["homeassistant.core"].HomeAssistant = object
    modules["wattelse"].__path__ = [str(ROOT)]
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(
            "wattelse.energy_dashboard", ROOT / "energy_dashboard.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module, modules


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dashboard, self.modules = load_dashboard()
        self.pairs = [("sensor.charge", "sensor.charge_cost")]
        self.grid = {
            "type": "grid", "flow_from": [{
                "stat_energy_from": "sensor.electricity",
                "stat_cost": "sensor.electricity_cost",
            }], "flow_to": [{"stat_energy_to": "sensor.export"}],
        }
        self.gas = {
            "type": "gas", "stat_energy_from": "sensor.gas",
            "stat_cost": "sensor.gas_cost",
        }
        self.manager = types.SimpleNamespace(
            data={"energy_sources": [self.grid, self.gas],
                  "device_consumption": [{"stat_consumption": "sensor.device"}]},
        )

        async def update(data):
            self.manager.data.update(data)

        self.manager.async_update = AsyncMock(side_effect=update)
        self.manager_patch = patch.object(
            self.dashboard, "_get_manager", AsyncMock(return_value=self.manager)
        )
        self.manager_patch.start()
        self.addCleanup(self.manager_patch.stop)

    async def test_gas_registration_and_idempotence(self):
        before = copy.deepcopy(self.grid)
        await self.dashboard.async_add_sources(None, self.pairs, "gas")
        sources = self.manager.data["energy_sources"]
        self.assertEqual(sources[0], before)
        self.assertEqual(sources[-1]["type"], "gas")
        self.assertEqual(sources[-1]["stat_cost"], "sensor.charge_cost")
        await self.dashboard.async_add_sources(None, self.pairs, "gas")
        self.assertEqual(self.manager.async_update.await_count, 1)
        self.assertEqual(len(self.manager.data["device_consumption"]), 1)

    async def test_default_electricity_and_switch_to_gas(self):
        await self.dashboard.async_add_sources(None, self.pairs)
        grid = next(s for s in self.manager.data["energy_sources"] if s["type"] == "grid")
        self.assertEqual(grid["flow_from"][-1]["stat_energy_from"], "sensor.charge")
        await self.dashboard.async_add_sources(None, self.pairs, "gas")
        grid = next(s for s in self.manager.data["energy_sources"] if s["type"] == "grid")
        self.assertEqual(len(grid["flow_from"]), 1)
        self.assertEqual(grid["flow_to"], [{"stat_energy_to": "sensor.export"}])

    async def test_switch_back_and_remove(self):
        await self.dashboard.async_add_sources(None, self.pairs, "gas")
        await self.dashboard.async_add_sources(None, self.pairs, "electricity")
        self.assertEqual(sum(s["type"] == "gas" for s in self.manager.data["energy_sources"]), 1)
        await self.dashboard.async_remove_sources(None, ["sensor.charge"])
        self.assertEqual(len(self.manager.data["energy_sources"]), 2)

    async def test_legacy_cleanup(self):
        sources = [self.gas, {"type": "grid", "stat_energy_from": "sensor.charge"}]
        self.assertEqual(self.dashboard._without_ours(sources, {"sensor.charge"}), [self.gas])

    async def test_vat_fuel_separation_and_skip_own_sensors(self):
        registry = types.SimpleNamespace(async_get=lambda entity: (
            types.SimpleNamespace(platform="wattelse") if entity == "sensor.charge" else None
        ))
        self.modules["homeassistant.helpers.entity_registry"].async_get = lambda hass: registry
        self.manager.data["energy_sources"].append({
            "type": "gas", "stat_energy_from": "sensor.charge", "stat_cost": "sensor.charge_cost",
        })
        with patch.dict(sys.modules, self.modules):
            self.assertEqual(await self.dashboard.async_detect_vat_sources(None), ["sensor.electricity_cost"])
            self.assertEqual(await self.dashboard.async_detect_vat_sources(None, "gas"), ["sensor.gas_cost"])
            self.gas.pop("stat_cost")
            self.gas["number_energy_price"] = 0.06
            self.assertEqual(await self.dashboard.async_detect_vat_sources(None, "gas"), ["sensor.gas_cost"])


if __name__ == "__main__":
    unittest.main()
