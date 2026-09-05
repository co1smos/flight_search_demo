import tomllib
import unittest
from pathlib import Path


class DependencyContractTests(unittest.TestCase):
    def test_google_genai_range_includes_interactions_api(self) -> None:
        pyproject = tomllib.loads(
            (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        )
        dependency = next(
            item for item in pyproject["project"]["dependencies"]
            if item.startswith("google-genai")
        )
        self.assertEqual(dependency, "google-genai>=1.60,<2")


if __name__ == "__main__":
    unittest.main()
