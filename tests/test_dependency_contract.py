import tomllib
import unittest
from pathlib import Path


class DependencyContractTests(unittest.TestCase):
    def test_experimental_interactions_and_airport_dataset_are_pinned(self) -> None:
        pyproject = tomllib.loads(
            (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        )
        dependencies = pyproject["project"]["dependencies"]
        google_genai = next(
            item for item in pyproject["project"]["dependencies"]
            if item.startswith("google-genai")
        )
        airportsdata = next(item for item in dependencies if item.startswith("airportsdata"))
        pydantic_settings = next(
            item for item in dependencies if item.startswith("pydantic-settings")
        )
        self.assertEqual(google_genai, "google-genai>=1.75,<1.76")
        self.assertEqual(airportsdata, "airportsdata==20260905")
        self.assertEqual(pydantic_settings, "pydantic-settings>=2.7,<3")

    def test_browser_use_floor_uses_declared_google_sdk_retry_boundary(self) -> None:
        pyproject = tomllib.loads(
            (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        )
        browser_use = next(
            item for item in pyproject["project"]["dependencies"]
            if item.startswith("browser-use")
        )
        self.assertEqual(browser_use, "browser-use>=0.8.0")

        live_run = (Path(__file__).parents[1] / "src/flight_search_demo/live_run.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("HttpRetryOptions(attempts=1)", live_run)
        self.assertNotIn("ChatGoogle(model=primary_model, api_key=api_key, max_retries=", live_run)


if __name__ == "__main__":
    unittest.main()
