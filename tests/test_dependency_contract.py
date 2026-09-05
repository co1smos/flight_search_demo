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
        self.assertEqual(google_genai, "google-genai>=1.75,<1.76")
        self.assertEqual(airportsdata, "airportsdata==20260905")


if __name__ == "__main__":
    unittest.main()
