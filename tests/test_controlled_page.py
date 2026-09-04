import urllib.request
import unittest

from flight_search_demo.controlled_page import ControlledPageServer, PAGE_TITLE


class ControlledPageServerTests(unittest.TestCase):
    def test_serves_expected_controlled_page(self) -> None:
        server = ControlledPageServer(host="127.0.0.1", port=0)
        server.start()
        host, port = server.address
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/") as response:
                body = response.read().decode("utf-8")
        finally:
            server.stop()

        self.assertIn(PAGE_TITLE, body)
        self.assertIn("Save marker to this browser profile", body)
        self.assertIn("Attempt offsite navigation", body)

    def test_offsite_target_is_outside_the_controlled_page_host(self) -> None:
        server = ControlledPageServer(host="127.0.0.1", port=0)
        server.start()
        host, port = server.address
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/") as response:
                body = response.read().decode("utf-8")
        finally:
            server.stop()

        self.assertIn('href="https://example.com/"', body)


if __name__ == "__main__":
    unittest.main()
