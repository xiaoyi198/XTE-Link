"""Checks for tray port selection and cooperative driver shutdown."""

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from pc_driver.driver import main
from pc_driver.tray import automatic_port, TrayApp


class TrayTests(unittest.TestCase):
    def test_auto_port_ignores_unrelated_and_ambiguous_devices(self):
        other = SimpleNamespace(device="COM1", vid=None, pid=None)
        x4 = SimpleNamespace(device="COM3", vid=0x303A, pid=0x1001)
        second = SimpleNamespace(device="COM4", vid=0x303A, pid=0x1001)
        self.assertIsNone(automatic_port([other]))
        self.assertEqual(automatic_port([other, x4]), "COM3")
        self.assertIsNone(automatic_port([x4, second]))

    def test_port_selection_stops_old_session_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder, patch("pc_driver.tray.pystray.Icon"):
            app = TrayApp(Path(folder))
            app.select_port("COM7")(app.icon, None)
            self.assertTrue(app.session_stop.is_set())
            self.assertEqual(TrayApp(Path(folder)).selected_port, "COM7")
            app.select_port(None)(app.icon, None)
            self.assertIsNone(TrayApp(Path(folder)).selected_port)

    def test_bad_saved_port_is_ignored(self):
        with tempfile.TemporaryDirectory() as folder, patch("pc_driver.tray.pystray.Icon"):
            path = Path(folder) / "tray.json"
            for raw in ('{"port":"loop://"}', '[]', 'bad JSON'):
                path.write_text(raw, encoding="utf-8")
                self.assertIsNone(TrayApp(Path(folder)).selected_port)

    def test_exit_stops_session_and_tray(self):
        with tempfile.TemporaryDirectory() as folder, patch("pc_driver.tray.pystray.Icon"):
            app = TrayApp(Path(folder))
            app.quit(app.icon, None)
            self.assertTrue(app.stop_event.is_set())
            self.assertTrue(app.session_stop.is_set())
            app.icon.stop.assert_called_once()

    def test_quit_requests_cancel_and_waits_for_maintenance_to_release(self):
        with tempfile.TemporaryDirectory() as folder, patch("pc_driver.tray.pystray.Icon"):
            app = TrayApp(Path(folder))
            app.installer_running.set()
            app.maintenance.set()
            finished = []
            with patch("pc_driver.tray.threading.Thread") as thread:
                app.quit(app.icon, None)
                self.assertTrue(app.installer_cancel.is_set())
                self.assertTrue(app.stop_event.is_set())
                app.icon.stop.assert_not_called()
                finish = thread.call_args.kwargs["target"]
                def release(_):
                    finished.append(True)
                    app.maintenance.clear()
                    app.installer_running.clear()
                with patch("pc_driver.tray.time.sleep", side_effect=release):
                    finish()
                self.assertEqual(finished, [True])
                app.icon.stop.assert_called_once()
                app.quit(app.icon, None)
                thread.assert_called_once()

    def test_snapshot_is_detached_and_device_update_does_not_hold_lock_for_ui(self):
        with tempfile.TemporaryDirectory() as folder, patch("pc_driver.tray.pystray.Icon"):
            app = TrayApp(Path(folder))
            app.devices["first"] = {"firmware": "old"}
            snapshot = app.state_snapshot()
            snapshot["devices"]["first"]["firmware"] = "changed"
            snapshot["config"]["devices"]["new"] = "key"
            self.assertEqual(app.devices["first"]["firmware"], "old")
            self.assertEqual(app.config["devices"], {})
            app.configure_wifi = Mock()
            def check_unlocked():
                self.assertTrue(app.lock.acquire(blocking=False))
                app.lock.release()
            app.icon.update_menu.side_effect = check_unlocked
            app.device_changed({"device": "first", "configured": True, "firmware": "new"})
            self.assertEqual(app.state_snapshot()["devices"]["first"]["firmware"], "new")

    def test_first_launch_and_default_menu_show_read_only_status(self):
        with tempfile.TemporaryDirectory() as folder, patch("pc_driver.tray.pystray.Icon"):
            app = TrayApp(Path(folder))
            app.worker = Mock()
            app.status_dialog = Mock()
            app.installer_dialog = Mock()
            app.setup(app.icon)
            app.status_dialog.assert_called_once()
            app.installer_dialog.assert_not_called()

    def test_existing_window_is_raised_without_creating_second_window(self):
        with tempfile.TemporaryDirectory() as folder, patch("pc_driver.tray.pystray.Icon"):
            app = TrayApp(Path(folder))
            app.dialog_lock.acquire()
            try:
                with patch("pc_driver.tray.threading.Thread") as thread:
                    app.status_dialog()
                thread.assert_not_called()
                self.assertTrue(app.dialog_show.is_set())
                self.assertFalse(app.open_maintenance.is_set())
            finally:
                app.dialog_lock.release()

    def test_driver_shutdown_releases_serial_and_bridge(self):
        stop = threading.Event()
        connection = Mock(in_waiting=0)

        def write(frame):
            value = json.loads(frame)
            if "host" in value:
                self.assertEqual(value["host"], "DEMO-PC")
                stop.set()
            return len(frame)

        connection.write.side_effect = write
        callback = Mock()
        with patch("pc_driver.driver.open_serial", return_value=connection), \
             patch("pc_driver.driver.AgentManager") as manager, \
             patch("pc_driver.driver.WindowsVolume"), \
             patch("pc_driver.driver.sys.stderr"):
            self.assertEqual(main(["--demo", "--port", "COM3"], stop, callback), 0)
        callback.assert_called_once_with(True, "USB COM3")
        connection.close.assert_called_once()
        manager.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
