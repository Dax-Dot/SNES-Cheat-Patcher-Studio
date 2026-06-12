"""GUI state tests that do not require opening a Tk window."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from snes_cheat_patcher.catalog import CatalogCheat
from snes_cheat_patcher.core import PatchError

try:
    from snes_cheat_patcher.gui import App
except ImportError as exc:
    App = None
    TK_IMPORT_ERROR = exc
else:
    TK_IMPORT_ERROR = None


class FakeVar:
    def __init__(self, value=False):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeWidget:
    def __init__(self):
        self.state = None

    def configure(self, **options):
        self.state = options.get("state")


def fake_plan(code: str, offset: int):
    return SimpleNamespace(
        code=SimpleNamespace(text=code),
        file_offset=offset,
    )


@unittest.skipIf(App is None, f"Tkinter is unavailable: {TK_IMPORT_ERROR}")
class GuiLogicTests(unittest.TestCase):
    def test_busy_state_disables_and_restores_mutating_controls(self):
        app = App.__new__(App)
        app.busy_widgets = [FakeWidget(), FakeWidget()]
        app.catalog_checkbuttons = [FakeWidget()]
        app.generate_btn = FakeWidget()

        app._set_busy(True)
        self.assertTrue(app._busy)
        self.assertTrue(all(
            widget.state == "disabled"
            for widget in (*app.busy_widgets, *app.catalog_checkbuttons, app.generate_btn)
        ))

        app._set_busy(False)
        self.assertFalse(app._busy)
        self.assertTrue(all(
            widget.state == "normal"
            for widget in (*app.busy_widgets, *app.catalog_checkbuttons, app.generate_btn)
        ))

    def test_failed_conflict_replacement_restores_previous_selection(self):
        app = App.__new__(App)
        app._busy = False
        old_plan = fake_plan("OLD", 0x100)
        new_plan = fake_plan("NEW", 0x100)
        old_var = FakeVar(True)
        new_var = FakeVar(True)
        app.catalog_rows = {
            "old": (CatalogCheat(1, "Old cheat", ("OLD",)), [old_plan], old_var),
            "new": (CatalogCheat(2, "New cheat", ("NEW",)), [new_plan], new_var),
        }
        app.plans = [old_plan]
        app.plan_catalog_indices = {"OLD": 1}
        app.logger = Mock()
        app._rebuild_plan_tree = Mock()
        app._update_catalog_selected_count = Mock()
        app._refresh_live_log = Mock()
        app._append_plan = Mock(side_effect=PatchError("simulated failure"))

        with patch("snes_cheat_patcher.gui.messagebox.askyesno", return_value=True), \
             patch("snes_cheat_patcher.gui.messagebox.showerror"):
            app._on_catalog_toggle("new")

        self.assertEqual(app.plans, [old_plan])
        self.assertEqual(app.plan_catalog_indices, {"OLD": 1})
        self.assertTrue(old_var.get())
        self.assertFalse(new_var.get())


if __name__ == "__main__":
    unittest.main(verbosity=2)
