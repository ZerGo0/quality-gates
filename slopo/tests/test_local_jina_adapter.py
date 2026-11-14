import math
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_jina_adapter import (  # noqa: E402
    INSTRUCTION,
    MODEL,
    NATIVE_DIMENSIONS,
    OUTPUT_DIMENSIONS,
    TASK,
    ClientRequestError,
    UpstreamResponseError,
    prepare_client_response,
    prepare_upstream_request,
)


class PrepareUpstreamRequestTests(unittest.TestCase):
    def test_applies_the_recommended_code2code_query_instruction(self) -> None:
        upstream, model, count = prepare_upstream_request(
            {
                "model": MODEL,
                "input": ["function example() {}"],
                "dimensions": OUTPUT_DIMENSIONS,
                "task": TASK,
            }
        )

        self.assertEqual(model, MODEL)
        self.assertEqual(count, 1)
        self.assertEqual(
            upstream["input"],
            [f"{INSTRUCTION}function example() {{}}"],
        )

    def test_rejects_a_different_task(self) -> None:
        with self.assertRaisesRegex(ClientRequestError, "task must be"):
            prepare_upstream_request(
                {
                    "model": MODEL,
                    "input": ["code"],
                    "dimensions": OUTPUT_DIMENSIONS,
                    "task": "nl2code.query",
                }
            )


class PrepareClientResponseTests(unittest.TestCase):
    def test_truncates_and_normalizes_the_native_embedding(self) -> None:
        response = prepare_client_response(
            {
                "data": [
                    {
                        "index": 0,
                        "embedding": [1.0] * NATIVE_DIMENSIONS,
                    }
                ]
            },
            MODEL,
            1,
        )

        vector = response["data"][0]["embedding"]
        self.assertEqual(len(vector), OUTPUT_DIMENSIONS)
        self.assertAlmostEqual(math.sqrt(sum(value * value for value in vector)), 1)

    def test_rejects_an_upstream_dimension_mismatch(self) -> None:
        with self.assertRaisesRegex(UpstreamResponseError, "896 values"):
            prepare_client_response(
                {"data": [{"index": 0, "embedding": [1.0] * 256}]},
                MODEL,
                1,
            )


if __name__ == "__main__":
    unittest.main()
