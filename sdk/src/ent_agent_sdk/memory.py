"""Conversation memory (SDK-11): per-session state that survives restarts, with config-only changes between
local (Floci) and AWS.

A LangGraph *checkpointer* stores each session's state. Pick one:

    EnterpriseAgentApp(graph, name="a", checkpointer=memory.in_memory())                       # dev/tests
    EnterpriseAgentApp(graph, name="a", checkpointer=memory.dynamodb("agent-checkpoints"))     # persistent

or set `ENT_CHECKPOINTER=dynamodb` and `ENT_CHECKPOINT_TABLE=agent-checkpoints` (no code change). Endpoint, region and
credentials follow the standard AWS settings (`AWS_ENDPOINT_URL` points it at Floci locally).

`DynamoDBSaver` is a write-through layer over LangGraph's `InMemorySaver`: reads are served from memory, a session's
history is loaded from DynamoDB the first time it is used, and every checkpoint and write is also stored in DynamoDB.
Items are limited to 400 KB by DynamoDB, so very large states need a different backend. An AgentCore Memory adapter
is not included (the AgentCore Memory API has not been verified against Floci or AWS).

Table layout: partition key `thread_id` (S), sort key `sk` (S):
`cp#<ns>#<checkpoint id>` (checkpoint + metadata), `blob#<ns>#<channel>#<version>` (channel values),
`w#<ns>#<checkpoint id>#<task id>#<index>` (pending writes).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

import boto3
from botocore.exceptions import ClientError
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata, CheckpointTuple
from langgraph.checkpoint.memory import InMemorySaver

from ent_agent_sdk.secrets import BOTO_CONFIG

logger = logging.getLogger("ent_agent_sdk.memory")

DEFAULT_TABLE = "agent-checkpoints"


class MemoryError_(RuntimeError):  # noqa: N801 (avoid shadowing the builtin MemoryError)
    """The checkpoint store could not be read or written."""


def in_memory() -> InMemorySaver:
    """A process-local checkpointer: sessions are lost on restart. For development and tests."""
    return InMemorySaver()


class DynamoDBSaver(InMemorySaver):
    def __init__(self, table_name: str = DEFAULT_TABLE, *, session: Any = None, endpoint_url: str | None = None,
                 region_name: str | None = None) -> None:
        super().__init__()
        self.table_name = table_name
        self._client = (session or boto3.session.Session()).client(
            "dynamodb", config=BOTO_CONFIG, endpoint_url=endpoint_url or os.environ.get("AWS_ENDPOINT_URL"),
            region_name=region_name,
        )
        self._loaded: set[str] = set()
        self._lock = threading.RLock()

    # --- table management ---------------------------------------------------------------------------------
    def ensure_table(self) -> None:
        """Create the table if it doesn't exist (local development; in AWS create it with your IaC)."""
        try:
            self._client.describe_table(TableName=self.table_name)
            return
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                raise MemoryError_(f"Could not check table '{self.table_name}'.") from None
        try:
            self._client.create_table(
                TableName=self.table_name,
                AttributeDefinitions=[{"AttributeName": "thread_id", "AttributeType": "S"},
                                      {"AttributeName": "sk", "AttributeType": "S"}],
                KeySchema=[{"AttributeName": "thread_id", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
                BillingMode="PAY_PER_REQUEST",
            )
        except ClientError as exc:
            raise MemoryError_(f"Could not create table '{self.table_name}' ({exc.response['Error']['Code']}).") from None

    # --- persistence helpers ------------------------------------------------------------------------------
    def _put_item(self, item: dict[str, Any]) -> None:
        try:
            self._client.put_item(TableName=self.table_name, Item=item)
        except ClientError as exc:
            raise MemoryError_(f"Could not store checkpoint data ({exc.response['Error']['Code']}).") from None

    @staticmethod
    def _typed(value: tuple[str, bytes]) -> dict[str, Any]:
        return {"M": {"t": {"S": value[0]}, "b": {"B": value[1]}}}

    @staticmethod
    def _untyped(attribute: dict[str, Any]) -> tuple[str, bytes]:
        return attribute["M"]["t"]["S"], bytes(attribute["M"]["b"]["B"])

    def _hydrate(self, thread_id: str) -> None:
        """Load a session's items from DynamoDB into the in-memory structures (once per session)."""
        with self._lock:
            if thread_id in self._loaded:
                return
            kwargs: dict[str, Any] = {
                "TableName": self.table_name, "KeyConditionExpression": "thread_id = :t",
                "ExpressionAttributeValues": {":t": {"S": thread_id}}, "ConsistentRead": True,
            }
            try:
                while True:
                    page = self._client.query(**kwargs)
                    for item in page.get("Items", []):
                        self._load_item(thread_id, item)
                    if "LastEvaluatedKey" not in page:
                        break
                    kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
            except ClientError as exc:
                raise MemoryError_(f"Could not load session history ({exc.response['Error']['Code']}).") from None
            self._loaded.add(thread_id)

    def _load_item(self, thread_id: str, item: dict[str, Any]) -> None:
        kind, _, rest = item["sk"]["S"].partition("#")
        if kind == "cp":
            ns, checkpoint_id = item["ns"]["S"], item["checkpoint_id"]["S"]
            parent = item.get("parent_id", {}).get("S") or None
            self.storage[thread_id][ns][checkpoint_id] = (self._untyped(item["checkpoint"]), self._untyped(item["metadata"]), parent)
        elif kind == "blob":
            self.blobs[(thread_id, item["ns"]["S"], item["channel"]["S"], item["version"]["S"])] = self._untyped(item["value"])
        elif kind == "w":
            key = (thread_id, item["ns"]["S"], item["checkpoint_id"]["S"])
            index = int(item["idx"]["N"])
            self.writes[key][(item["task_id"]["S"], index)] = (
                item["task_id"]["S"], item["channel"]["S"], self._untyped(item["value"]), item.get("task_path", {}).get("S", ""),
            )

    # --- BaseCheckpointSaver (sync) -----------------------------------------------------------------------
    def get_tuple(self, config: Any) -> CheckpointTuple | None:
        self._hydrate(config["configurable"]["thread_id"])
        with self._lock:
            return super().get_tuple(config)

    def list(self, config: Any, *, filter: dict[str, Any] | None = None, before: Any = None,
             limit: int | None = None) -> Iterator[CheckpointTuple]:
        if config:
            self._hydrate(config["configurable"]["thread_id"])
        with self._lock:
            return iter(list(super().list(config, filter=filter, before=before, limit=limit)))

    def put(self, config: Any, checkpoint: Checkpoint, metadata: CheckpointMetadata, new_versions: ChannelVersions) -> Any:
        thread_id = config["configurable"]["thread_id"]
        self._hydrate(thread_id)
        with self._lock:
            result = super().put(config, checkpoint, metadata, new_versions)
            ns = config["configurable"].get("checkpoint_ns", "")
            stored = self.storage[thread_id][ns][checkpoint["id"]]
            self._put_item({
                "thread_id": {"S": thread_id}, "sk": {"S": f"cp#{ns}#{checkpoint['id']}"}, "ns": {"S": ns},
                "checkpoint_id": {"S": checkpoint["id"]}, "checkpoint": self._typed(stored[0]),
                "metadata": self._typed(stored[1]), **({"parent_id": {"S": stored[2]}} if stored[2] else {}),
            })
            for channel, version in new_versions.items():
                blob = self.blobs.get((thread_id, ns, channel, version))
                if blob is not None:
                    self._put_item({
                        "thread_id": {"S": thread_id}, "sk": {"S": f"blob#{ns}#{channel}#{version}"}, "ns": {"S": ns},
                        "channel": {"S": channel}, "version": {"S": str(version)}, "value": self._typed(blob),
                    })
            return result

    def put_writes(self, config: Any, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = "") -> None:
        thread_id = config["configurable"]["thread_id"]
        self._hydrate(thread_id)
        with self._lock:
            super().put_writes(config, writes, task_id, task_path)
            ns = config["configurable"].get("checkpoint_ns", "")
            checkpoint_id = config["configurable"]["checkpoint_id"]
            for (stored_task, index), (tid, channel, value, path) in self.writes[(thread_id, ns, checkpoint_id)].items():
                if stored_task != task_id:
                    continue
                self._put_item({
                    "thread_id": {"S": thread_id}, "sk": {"S": f"w#{ns}#{checkpoint_id}#{task_id}#{index:06d}"},
                    "ns": {"S": ns}, "checkpoint_id": {"S": checkpoint_id}, "task_id": {"S": tid}, "idx": {"N": str(index)},
                    "channel": {"S": channel}, "value": self._typed(value), "task_path": {"S": path},
                })

    def delete_thread(self, thread_id: str) -> None:
        self._hydrate(thread_id)
        with self._lock:
            super().delete_thread(thread_id)
            try:
                page = self._client.query(
                    TableName=self.table_name, KeyConditionExpression="thread_id = :t",
                    ExpressionAttributeValues={":t": {"S": thread_id}}, ProjectionExpression="thread_id, sk",
                )
                for item in page.get("Items", []):
                    self._client.delete_item(TableName=self.table_name, Key={"thread_id": item["thread_id"], "sk": item["sk"]})
            except ClientError as exc:
                raise MemoryError_(f"Could not delete session history ({exc.response['Error']['Code']}).") from None
            self._loaded.discard(thread_id)

    # --- async: run the blocking AWS calls off the event loop --------------------------------------------------
    async def aget_tuple(self, config: Any) -> CheckpointTuple | None:
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(self, config: Any, *, filter: dict[str, Any] | None = None, before: Any = None,
                    limit: int | None = None) -> AsyncIterator[CheckpointTuple]:
        items = await asyncio.to_thread(lambda: list(self.list(config, filter=filter, before=before, limit=limit)))
        for item in items:
            yield item

    async def aput(self, config: Any, checkpoint: Checkpoint, metadata: CheckpointMetadata, new_versions: ChannelVersions) -> Any:
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config: Any, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = "") -> None:
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        await asyncio.to_thread(self.delete_thread, thread_id)


def dynamodb(table_name: str | None = None, *, create: bool | None = None, **kwargs: Any) -> DynamoDBSaver:
    """A persistent checkpointer. `create=True` makes the table if missing (default: only when `AWS_ENDPOINT_URL` is
    set, i.e. against Floci; in AWS create the table with your IaC)."""
    saver = DynamoDBSaver(table_name or os.environ.get("ENT_CHECKPOINT_TABLE", DEFAULT_TABLE), **kwargs)
    if create if create is not None else bool(os.environ.get("AWS_ENDPOINT_URL")):
        saver.ensure_table()
    return saver


def from_environment() -> Any | None:
    """The checkpointer named by `ENT_CHECKPOINTER` (`memory` or `dynamodb`), or None if unset."""
    kind = os.environ.get("ENT_CHECKPOINTER", "").strip().lower()
    if not kind:
        return None
    if kind == "memory":
        return in_memory()
    if kind == "dynamodb":
        return dynamodb()
    raise ValueError(f"ENT_CHECKPOINTER must be 'memory' or 'dynamodb', got '{kind}'.")
