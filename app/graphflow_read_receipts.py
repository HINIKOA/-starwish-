"""Bounded, process-local receipts for authorized source reads."""
from __future__ import annotations

import hmac
import json
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from .db import estimate_tokens
from .graphflow import PROVIDER, GraphFlowError

MIN_TOKENS = 256
MAX_TOKENS = 1400
DEFAULT_TOKENS = 750
_TTL_SECONDS = 300
_MAX_RECEIPTS = 32
_SECRET = secrets.token_bytes(32)
_LOCK = threading.Lock()
_RECEIPTS: OrderedDict[str, _Receipt] = OrderedDict()
_Selection = tuple[str, str, bool]
_Version = tuple[bytes, bytes]


@dataclass(frozen=True)
class _Receipt:
    id: str
    workspace: Path
    selection: _Selection
    version: _Version
    expires: float
    text: str
    source_partial: bool
    source_truncated: bool
    # A delta is a splice into the previous complete text. The receipt still owns the full text.
    delta: tuple[str, int, int, int] | None = None

    @property
    def body(self) -> str:
        if self.delta:
            _, start, _, new_end = self.delta
            return self.text[start:new_end]
        return self.text


def _prune(now: float) -> None:
    while _RECEIPTS and next(iter(_RECEIPTS.values())).expires <= now:
        _RECEIPTS.popitem(last=False)


def _lookup(workspace: Path, receipt_id: str) -> _Receipt | None:
    with _LOCK:
        _prune(time.monotonic())
        receipt = _RECEIPTS.get(receipt_id)
        return receipt if receipt and receipt.workspace == workspace else None


def _cursor(receipt_id: str, position: int) -> str:
    data = f"{receipt_id[4:]}:{position}"
    mac = hmac.digest(_SECRET, data.encode("ascii"), "sha256").hex()[:24]
    return f"swc:{data}:{mac}"


def _stale() -> GraphFlowError:
    return GraphFlowError("STALE_CURSOR: restart the source read without a cursor.", 409)


def _packet(receipt: _Receipt, text: str, budget: int, *, next_cursor: str = "",
            unchanged: bool = False) -> dict:
    result = {
        "status": "unchanged" if unchanged else "delta" if receipt.delta else "full",
        "text": text,
        "provider": PROVIDER,
        "truncated": bool(next_cursor),
        "sourcePartial": receipt.source_partial,
        "sourceTruncated": receipt.source_truncated,
        "tokenBudget": {"limit": budget, "estimated": 0, "approximate": True},
    }
    if next_cursor:
        result["nextCursor"] = next_cursor
    else:
        result["receiptId"] = receipt.id
    if receipt.delta and not unchanged:
        base, start, old_end, _ = receipt.delta
        result["baseReceipt"] = base
        result["replace"] = {"start": start, "end": old_end}
    tokens = result["tokenBudget"]
    while True:
        estimate = estimate_tokens(json.dumps(result, ensure_ascii=False))
        if estimate == tokens["estimated"]:
            return result
        tokens["estimated"] = estimate


def _cost(packet: dict) -> int:
    return packet["tokenBudget"]["estimated"]


def _page(receipt: _Receipt, offset: int, budget: int) -> dict:
    body = receipt.body
    final = _packet(receipt, body[offset:], budget)
    if _cost(final) <= budget:
        return final

    # Keep the whole JSON response within budget, including escaping and the next cursor.
    low, high, best = offset + 1, len(body) - 1, None
    while low <= high:
        end = (low + high) // 2
        candidate = _packet(receipt, body[offset:end], budget, next_cursor=_cursor(receipt.id, end))
        if _cost(candidate) <= budget:
            best, low = candidate, end + 1
        else:
            high = end - 1
    if best is None:
        raise GraphFlowError("Output budget is too small for a source page.")
    return best


def _splice(old: str, new: str) -> tuple[int, int, int]:
    start, limit = 0, min(len(old), len(new))
    while start < limit and old[start] == new[start]:
        start += 1
    tail = 0
    while tail < limit - start and old[-1 - tail] == new[-1 - tail]:
        tail += 1
    return start, len(old) - tail, len(new) - tail


@dataclass(frozen=True)
class ReadRequest:
    workspace: Path
    selection: _Selection
    budget: int
    receipt_id: str = ""
    offset: int = 0

    def _previous(self) -> _Receipt | None:
        receipt = _lookup(self.workspace, self.receipt_id) if self.receipt_id else None
        return receipt if receipt and receipt.selection == self.selection else None

    def reuse(self, version: _Version) -> dict | None:
        previous = self._previous()
        if self.offset:
            if not previous or previous.version != version or self.offset >= len(previous.body):
                raise _stale()
            return _page(previous, self.offset, self.budget)
        if previous and previous.version == version:
            return _packet(previous, "", self.budget, unchanged=True)
        return None

    def publish(self, version: _Version, text: str, *, partial: bool, source_truncated: bool) -> dict:
        previous = self._previous()
        now = time.monotonic()
        args = (f"swr:{secrets.token_hex(16)}", self.workspace, self.selection, version,
                now + _TTL_SECONDS, text, partial, source_truncated)
        receipt = _Receipt(*args)
        unchanged = False
        if previous and previous.version[0] == version[0]:
            unchanged = (previous.text, previous.source_partial, previous.source_truncated) == (
                text, partial, source_truncated)
            if not unchanged and previous.text != text:
                start, old_end, new_end = _splice(previous.text, text)
                delta = _Receipt(*args, delta=(previous.id, start, old_end, new_end))
                delta_cost = _cost(_packet(delta, delta.body, self.budget))
                if delta_cost <= self.budget and delta_cost < _cost(_packet(receipt, text, self.budget)):
                    receipt = delta
        result = (_packet(receipt, "", self.budget, unchanged=True)
                  if unchanged else _page(receipt, 0, self.budget))
        with _LOCK:
            _prune(now)
            _RECEIPTS[receipt.id] = receipt
            if len(_RECEIPTS) > _MAX_RECEIPTS:
                _RECEIPTS.popitem(last=False)
        return result


def prepare(workspace: Path, selection: _Selection, budget: int | None, since_receipt: str,
            cursor: str) -> ReadRequest:
    budget = DEFAULT_TOKENS if budget is None else budget
    if type(budget) is not int or not MIN_TOKENS <= budget <= MAX_TOKENS:
        raise GraphFlowError(f"maxOutputTokens must be an integer from {MIN_TOKENS} to {MAX_TOKENS}.")
    if not isinstance(since_receipt, str) or not isinstance(cursor, str):
        raise GraphFlowError("Invalid read receipt or cursor.")
    if cursor:
        if selection != ("", "", False) or since_receipt:
            raise GraphFlowError("A cursor cannot be combined with a new source selection or receipt.")
        match = re.fullmatch(r"swc:([0-9a-f]{32}):([1-9][0-9]{0,4}):[0-9a-f]{24}", cursor)
        if not match:
            raise GraphFlowError("Invalid source cursor.")
        receipt_id, offset = f"swr:{match[1]}", int(match[2])
        if not hmac.compare_digest(cursor, _cursor(receipt_id, offset)):
            raise GraphFlowError("Invalid source cursor.")
        receipt = _lookup(workspace, receipt_id)
        if not receipt or offset >= len(receipt.body):
            raise _stale()
        return ReadRequest(workspace, receipt.selection, budget, receipt_id, offset)
    if since_receipt and not re.fullmatch(r"swr:[0-9a-f]{32}", since_receipt):
        raise GraphFlowError("Invalid source receipt.")
    return ReadRequest(workspace, selection, budget, since_receipt)
