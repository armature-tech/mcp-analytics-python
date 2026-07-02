from __future__ import annotations

import unittest

from armature_mcp_analytics import (
    append_telemetry_hint,
    decorate_input_schema_with_telemetry,
    extract_telemetry_arguments,
)


class SchemaTests(unittest.TestCase):
    def test_decorates_json_schema_with_optional_telemetry(self) -> None:
        schema = {
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"],
        }

        decorated = decorate_input_schema_with_telemetry(schema)

        self.assertIsNot(decorated, schema)
        self.assertEqual(decorated["required"], ["customer_id"])
        self.assertIn("telemetry", decorated["properties"])
        self.assertNotIn("required", decorated["properties"]["telemetry"])
        self.assertEqual(decorated["properties"]["telemetry"]["properties"]["intent"]["type"], "string")

    def test_required_telemetry_mode_is_internal_but_supported(self) -> None:
        decorated = decorate_input_schema_with_telemetry(
            {"type": "object", "properties": {}, "required": []},
            {"telemetry": {"intent": "required"}},
        )

        self.assertEqual(decorated["required"], ["telemetry"])
        self.assertEqual(decorated["properties"]["telemetry"]["required"], ["intent"])

    def test_extract_telemetry_strips_handler_args(self) -> None:
        args, telemetry = extract_telemetry_arguments(
            {
                "customer_id": "cus_123",
                "telemetry": {"intent": "look up customer"},
            }
        )

        self.assertEqual(args, {"customer_id": "cus_123"})
        self.assertEqual(telemetry, {"intent": "look up customer"})

    def test_append_telemetry_hint_is_idempotent(self) -> None:
        once = append_telemetry_hint("Look up a customer.")
        twice = append_telemetry_hint(once)
        self.assertEqual(once, twice)


if __name__ == "__main__":
    unittest.main()

