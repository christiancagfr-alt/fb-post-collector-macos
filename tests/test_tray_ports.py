import socket
import unittest

from fb_collector.tray import can_bind, pick_port


class TrayPortTests(unittest.TestCase):
    def test_occupied_port_cannot_be_reused_and_next_port_is_selected(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        occupied_port = listener.getsockname()[1]
        try:
            self.assertFalse(can_bind("127.0.0.1", occupied_port))
            # Windows may also occupy/reserve the adjacent ephemeral port.
            selected_port, existing = pick_port("127.0.0.1", occupied_port, count=32)
            self.assertGreater(selected_port, occupied_port)
            self.assertLess(selected_port, occupied_port + 32)
            self.assertTrue(can_bind("127.0.0.1", selected_port))
            self.assertFalse(existing)
        finally:
            listener.close()


if __name__ == "__main__":
    unittest.main()
