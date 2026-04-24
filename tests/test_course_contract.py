from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.services.course_contract import (
    OUTPUT_FORMAT_VERSION,
    build_export_payload,
    build_prompt_text,
    validate_import_payload,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures"


class CourseContractTests(unittest.TestCase):
    def test_export_payload_uses_current_version(self):
        payload = build_export_payload(
            trip_window={
                "startAt": "2026-05-01T09:00:00Z",
                "endAt": "2026-05-01T22:00:00Z",
            },
            course_start={"label": "삿포로역"},
            selected_places=[],
            selection_context={"theme": "food"},
        )

        self.assertEqual(payload["outputFormatVersion"], OUTPUT_FORMAT_VERSION)
        self.assertIn("tripWindow", payload)
        self.assertIn("selectedPlaces", payload)

    def test_prompt_contains_json(self):
        fixture = json.loads((FIXTURE_DIR / "course-export-v1.json").read_text())
        prompt = build_prompt_text(fixture)
        self.assertIn('"outputFormatVersion": "1.0"', prompt)
        self.assertIn("여행 코스를 제안", prompt)

    def test_valid_import_fixture_passes(self):
        fixture = json.loads((FIXTURE_DIR / "course-import-v1.json").read_text())
        validate_import_payload(fixture)

    def test_invalid_version_fixture_fails(self):
        fixture = json.loads(
            (FIXTURE_DIR / "course-import-invalid-version.json").read_text()
        )
        with self.assertRaises(ValueError):
            validate_import_payload(fixture)


if __name__ == "__main__":
    unittest.main()
