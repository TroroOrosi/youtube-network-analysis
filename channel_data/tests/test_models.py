from __future__ import annotations

import unittest

from channel_data.errors import ChannelDataError, ErrorCode


class ChannelDataErrorContractTests(unittest.TestCase):
    def test_error_codes_are_stable_machine_values(self) -> None:
        self.assertEqual(ErrorCode.INVALID_INPUT.value, "INVALID_INPUT")
        self.assertEqual(
            ErrorCode.RESOURCE_NOT_FOUND_OR_FORBIDDEN.value,
            "RESOURCE_NOT_FOUND_OR_FORBIDDEN",
        )
        self.assertEqual(ErrorCode.DATASET_NOT_READY.value, "DATASET_NOT_READY")
        self.assertEqual(ErrorCode.INVALID_CURSOR.value, "INVALID_CURSOR")

    def test_errors_expose_only_stable_safe_fields(self) -> None:
        error = ChannelDataError(
            ErrorCode.PERMISSION_DENIED,
            message="Required permission is missing",
            field="context.permissions",
            retryable=False,
            correlation_id="correlation-1",
            reason_code="MISSING_PERMISSION",
        )

        self.assertEqual(error.code, "PERMISSION_DENIED")
        self.assertEqual(error.message, "Required permission is missing")
        self.assertEqual(error.field, "context.permissions")
        self.assertFalse(error.retryable)
        self.assertEqual(error.correlation_id, "correlation-1")
        self.assertEqual(error.reason_code, "MISSING_PERMISSION")
        self.assertEqual(str(error), error.message)

    def test_generated_correlation_id_is_nonempty_and_nonsecret(self) -> None:
        error = ChannelDataError(
            ErrorCode.INVALID_INPUT,
            message="Invalid input",
        )

        self.assertTrue(error.correlation_id.startswith("error_"))
        self.assertNotIn("Invalid input", error.correlation_id)


if __name__ == "__main__":
    unittest.main()
