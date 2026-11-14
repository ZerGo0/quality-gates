#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


MODEL = "jina-code-embeddings-0.5b"
TASK = "code2code.query"
INSTRUCTION = "Find an equivalent code snippet given the following code snippet:\n"
NATIVE_DIMENSIONS = 896
OUTPUT_DIMENSIONS = 256
MAX_REQUEST_BYTES = 16 * 1024 * 1024


class ClientRequestError(ValueError):
    pass


class UpstreamResponseError(RuntimeError):
    pass


def prepare_upstream_request(payload: Any) -> tuple[dict[str, Any], str, int]:
    if not isinstance(payload, dict):
        raise ClientRequestError("request body must be a JSON object")

    model = payload.get("model")
    if model != MODEL:
        raise ClientRequestError(f"model must be {MODEL!r}")

    dimensions = payload.get("dimensions")
    if dimensions != OUTPUT_DIMENSIONS:
        raise ClientRequestError(f"dimensions must be {OUTPUT_DIMENSIONS}")

    task = payload.get("task")
    if task != TASK:
        raise ClientRequestError(f"task must be {TASK!r}")

    inputs = payload.get("input")
    if not isinstance(inputs, list) or not inputs:
        raise ClientRequestError("input must be a non-empty JSON array")
    if not all(isinstance(value, str) and value for value in inputs):
        raise ClientRequestError("every input must be a non-empty string")

    upstream_payload = {
        "model": MODEL,
        "input": [f"{INSTRUCTION}{value}" for value in inputs],
        "encoding_format": "float",
    }
    return upstream_payload, model, len(inputs)


def prepare_client_response(
    payload: Any,
    requested_model: str,
    expected_count: int,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise UpstreamResponseError("upstream response must be a JSON object")
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != expected_count:
        raise UpstreamResponseError("upstream returned the wrong number of embeddings")

    by_index: dict[int, list[float]] = {}
    for item in data:
        if not isinstance(item, dict):
            raise UpstreamResponseError("upstream embedding item must be an object")
        index = item.get("index")
        vector = item.get("embedding")
        if not isinstance(index, int) or isinstance(index, bool):
            raise UpstreamResponseError("upstream embedding index must be an integer")
        if index in by_index or not 0 <= index < expected_count:
            raise UpstreamResponseError("upstream embedding index is invalid")
        if not isinstance(vector, list) or len(vector) != NATIVE_DIMENSIONS:
            raise UpstreamResponseError(
                f"upstream embedding must contain {NATIVE_DIMENSIONS} values"
            )
        if not all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            for value in vector
        ):
            raise UpstreamResponseError("upstream embedding contains an invalid value")

        truncated = [float(value) for value in vector[:OUTPUT_DIMENSIONS]]
        norm = math.sqrt(sum(value * value for value in truncated))
        if not math.isfinite(norm) or norm == 0:
            raise UpstreamResponseError("truncated upstream embedding has zero norm")
        by_index[index] = [value / norm for value in truncated]

    embeddings = [
        {
            "object": "embedding",
            "embedding": by_index[index],
            "index": index,
        }
        for index in range(expected_count)
    ]
    response = {
        "object": "list",
        "data": embeddings,
        "model": requested_model,
    }
    usage = payload.get("usage")
    if isinstance(usage, dict):
        response["usage"] = usage
    return response


class AdapterServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], upstream_url: str) -> None:
        super().__init__(address, AdapterHandler)
        self.upstream_url = upstream_url


class AdapterHandler(BaseHTTPRequestHandler):
    server: AdapterServer

    def do_GET(self) -> None:
        if self.path != "/health":
            self._write_json(404, {"error": "not found"})
            return
        try:
            health_url = self.server.upstream_url.removesuffix("/v1/embeddings") + "/health"
            with urlopen(health_url, timeout=2) as response:
                if response.status != 200:
                    raise UpstreamResponseError("upstream health check failed")
        except (HTTPError, URLError, TimeoutError, UpstreamResponseError):
            self._write_json(503, {"status": "unavailable"})
            return
        self._write_json(200, {"status": "ok"})

    def do_POST(self) -> None:
        if self.path != "/v1/embeddings":
            self._write_json(404, {"error": "not found"})
            return
        try:
            payload = self._read_json()
            upstream_payload, requested_model, input_count = prepare_upstream_request(
                payload
            )
        except ClientRequestError as error:
            self._write_json(400, {"error": str(error)})
            return

        try:
            upstream_response = self._request_upstream(upstream_payload)
            response = prepare_client_response(
                upstream_response,
                requested_model,
                input_count,
            )
        except UpstreamResponseError as error:
            self._write_json(502, {"error": str(error)})
            return
        self._write_json(200, response)

    def _read_json(self) -> Any:
        content_type = self.headers.get("Content-Type", "").partition(";")[0].strip()
        if content_type != "application/json":
            raise ClientRequestError("Content-Type must be application/json")
        content_length = self.headers.get("Content-Length")
        try:
            length = int(content_length) if content_length is not None else -1
        except ValueError as error:
            raise ClientRequestError("Content-Length must be an integer") from error
        if not 0 <= length <= MAX_REQUEST_BYTES:
            raise ClientRequestError("request body size is invalid")
        try:
            return json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ClientRequestError("request body must be valid JSON") from error

    def _request_upstream(self, payload: dict[str, Any]) -> Any:
        request = Request(
            self.server.upstream_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=300) as response:
                if response.status != 200:
                    raise UpstreamResponseError(
                        f"upstream returned HTTP {response.status}"
                    )
                return json.load(response)
        except HTTPError as error:
            raise UpstreamResponseError(
                f"upstream returned HTTP {error.code}"
            ) from error
        except (URLError, TimeoutError) as error:
            raise UpstreamResponseError("upstream request failed") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpstreamResponseError("upstream returned invalid JSON") from error

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=17998)
    parser.add_argument(
        "--upstream-url",
        default="http://127.0.0.1:17997/v1/embeddings",
    )
    args = parser.parse_args()
    if args.host != "127.0.0.1":
        parser.error("--host must be 127.0.0.1")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not args.upstream_url.startswith("http://127.0.0.1:"):
        parser.error("--upstream-url must use loopback HTTP")

    server = AdapterServer((args.host, args.port), args.upstream_url)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
