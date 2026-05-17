"""Async client for Logseq's local HTTP JSON-RPC API."""

from __future__ import annotations

import uuid
from types import TracebackType
from typing import Any

import httpx
from loguru import logger
from pydantic import BaseModel, Field

_DEFAULT_TIMEOUT = httpx.Timeout(30.0)


class JsonRpcRequest(BaseModel):
    """JSON-RPC 2.0 request envelope."""

    jsonrpc: str = Field(default="2.0")
    id: int | str
    method: str
    params: list[Any] | dict[str, Any] = Field(default_factory=lambda: [])


class LogseqClient:
    """Thin async wrapper around the Logseq local HTTP API.

    The API listens on a configurable base URL (commonly ``http://localhost:12315``)
    and expects a Bearer token in ``Authorization`` for authenticated calls.
    """

    def __init__(
        self,
        api_url: str,
        token: str,
        *,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        """Create a client bound to a Logseq instance.

        Args:
            api_url: Base URL of the Logseq HTTP API (no trailing slash required).
            token: API token used as ``Bearer`` credentials.
            timeout: Optional HTTP client timeout. Defaults to 30 seconds.
        """
        base = api_url.rstrip("/") + "/"
        self._timeout = timeout or _DEFAULT_TIMEOUT
        self._client = httpx.AsyncClient(
            base_url=base,
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._timeout,
        )

    async def aclose(self) -> None:
        """Release underlying HTTP resources."""
        await self._client.aclose()

    async def __aenter__(self) -> LogseqClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def _post_json_rpc(self, payload: JsonRpcRequest) -> dict[str, Any]:
        """Send a single JSON-RPC request and return the parsed JSON object.

        Raises:
            httpx.HTTPError: When the transport fails.
            ValueError: When the response body is not a JSON object.
        """
        body = payload.model_dump()
        response = await self._client.post("", json=body)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            msg = "Logseq API returned a non-object JSON body"
            raise ValueError(msg)
        return data

    async def append_block(
        self,
        parent_uuid: str,
        content: str,
        properties: dict[str, str],
    ) -> str:
        """Append a child block beneath ``parent_uuid`` via ``logseq.Editor.insertBlock``.

        Sends a JSON-RPC request so the new block is nested as a child of
        ``parent_uuid`` (``sibling: False``) with the given ``properties``.

        Args:
            parent_uuid: UUID of the parent Logseq block (or page identifier string).
            content: Markdown / outliner text for the new block body.
            properties: Block properties (e.g. ``{"id": "<uuid>"}``) as string pairs.

        Returns:
            The created block's ``uuid`` string from the Logseq API response.

        Raises:
            httpx.HTTPError: When the HTTP transport fails.
            ValueError: When the response body is not a JSON object.
            RuntimeError: When the RPC reports an error, omits a result, or omits ``uuid``.
        """
        payload = JsonRpcRequest(
            id=str(uuid.uuid4()),
            method="logseq.Editor.insertBlock",
            params=[
                parent_uuid,
                content,
                {"sibling": False, "properties": properties},
            ],
        )
        data = await self._post_json_rpc(payload)

        if "error" in data:
            err = data["error"]
            logger.error(
                "Logseq insertBlock failed (RPC error): parent_uuid={parent_uuid!r} "
                "content={content!r} error={error!r}",
                parent_uuid=parent_uuid,
                content=content,
                error=err,
            )
            msg = f"Logseq insertBlock RPC error: {err!r}"
            raise RuntimeError(msg)

        result = data.get("result")
        if result is None:
            logger.error(
                "Logseq insertBlock failed (null result): parent_uuid={parent_uuid!r} "
                "content={content!r} response_keys={keys!r}",
                parent_uuid=parent_uuid,
                content=content,
                keys=list(data),
            )
            raise RuntimeError("Logseq insertBlock returned no result")

        if not isinstance(result, dict):
            logger.error(
                "Logseq insertBlock failed (unexpected result type): parent_uuid={parent_uuid!r} "
                "content={content!r} result={result!r}",
                parent_uuid=parent_uuid,
                content=content,
                result=result,
            )
            raise RuntimeError("Logseq insertBlock returned a non-object result")

        block_uuid = result.get("uuid")
        if not isinstance(block_uuid, str) or not block_uuid:
            logger.error(
                "Logseq insertBlock failed (missing uuid): parent_uuid={parent_uuid!r} "
                "content={content!r} result={result!r}",
                parent_uuid=parent_uuid,
                content=content,
                result=result,
            )
            raise RuntimeError("Logseq insertBlock result missing uuid")

        return block_uuid
