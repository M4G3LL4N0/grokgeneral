from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

from .errors import StateError, ValidationError
from .storage import StateStore, atomic_write_json, canonical_json, is_secret_key, redact
from .timeutil import is_expired, isoformat, utc_now

_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_SHA256_KEY = re.compile(r"^[0-9a-f]{64}$")
_VALUE_ENVELOPE = "__grokgeneral_cache_value__"


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _validate_key(value: Any) -> str:
    key = _text(value, "cache key")
    if len(key) > 200 or not _SAFE_KEY.fullmatch(key) or ".." in key:
        raise ValidationError("cache key must be a contained path-safe identifier")
    return key


def _safe_value(value: Any) -> Any:
    return redact(value)


def _key_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _key_payload(item) for key, item in value.items() if not is_secret_key(str(key))}
    if isinstance(value, list):
        return [_key_payload(item) for item in value]
    return value


def _state_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {_VALUE_ENVELOPE: value}


def _record_value(record: Mapping[str, Any]) -> Any:
    value = record.get("value")
    if isinstance(value, dict) and set(value) == {_VALUE_ENVELOPE}:
        return value[_VALUE_ENVELOPE]
    return value


class Cache:
    def __init__(self, state: StateStore) -> None:
        self.state = state
        self.state.initialize()
        self.cache_dir = self.state.cache_dir.resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.cache_dir.chmod(0o700)
        except OSError:
            pass

    def key(self, kind: str, payload: Any) -> str:
        kind = _text(kind, "kind")
        material = {"kind": kind, "payload": _key_payload(payload)}
        return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()

    def _artifact_path(self, key: str) -> Path:
        key = _validate_key(key)
        filename_key = key if _SHA256_KEY.fullmatch(key) else hashlib.sha256(key.encode("utf-8")).hexdigest()
        candidate = (self.cache_dir / f"{filename_key}.json").resolve(strict=False)
        try:
            candidate.relative_to(self.cache_dir)
        except ValueError as exc:
            raise ValidationError("cache artifact is outside the state cache directory") from exc
        if candidate.exists() and candidate.is_symlink():
            raise ValidationError("cache artifact must not be a symbolic link")
        return candidate

    def _remove_artifact(self, key: str) -> None:
        try:
            path = self._artifact_path(key)
        except ValidationError:
            return
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
        except FileNotFoundError:
            pass

    def _write_artifact(
        self,
        key: str,
        kind: str,
        value: Any,
        metadata: Mapping[str, Any],
        expires_at: str | None,
    ) -> None:
        path = self._artifact_path(key)
        atomic_write_json(
            path,
            {
                "key": key,
                "kind": kind,
                "value": value,
                "metadata": dict(metadata),
                "expires_at": expires_at,
            },
        )

    def get(self, key: str, default: Any = None) -> Any:
        key = _validate_key(key)
        record = self.state.get_record("cache_entries", key)
        if record is None:
            return default
        expires_at = record.get("expires_at")
        try:
            expired = is_expired(expires_at)
        except ValueError as exc:
            raise StateError(f"malformed cache expiration for {key}") from exc
        if expired:
            self.invalidate(key=key)
            return default
        return _record_value(record)

    def put(
        self,
        key: str,
        value: Any,
        kind: str,
        ttl_seconds: int | float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        key = _validate_key(key)
        kind = _text(kind, "kind")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise ValidationError("cache metadata must be a mapping")
        if ttl_seconds is not None:
            if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)):
                raise ValidationError("ttl_seconds must be a number or None")
            if not math.isfinite(float(ttl_seconds)) or float(ttl_seconds) < 0:
                raise ValidationError("ttl_seconds must be a finite non-negative number")
        safe_value = _safe_value(value)
        safe_metadata = redact(dict(metadata or {}))
        expires_at = None
        if ttl_seconds is not None:
            expires_at = isoformat(utc_now() + timedelta(seconds=float(ttl_seconds)))
        existing = self.state.get_record("cache_entries", key)
        now = isoformat(utc_now())
        record = {
            "id": key,
            "kind": kind,
            "data": _state_value(safe_value),
            "expires_at": expires_at,
            "metadata": safe_metadata,
            "created_at": (existing or {}).get("created_at") or now,
            "updated_at": now,
        }
        with self.state.transaction() as connection:
            self.state.put_record(connection, "cache_entries", record)
        self._write_artifact(key, kind, safe_value, safe_metadata, expires_at)
        return {
            "key": key,
            "kind": kind,
            "value": safe_value,
            "metadata": safe_metadata,
            "expires_at": expires_at,
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
        }

    def _all_records(self) -> list[dict[str, Any]]:
        return self.state.list_records("cache_entries")

    def _remove_records(self, records: list[dict[str, Any]]) -> int:
        if not records:
            return 0
        with self.state.transaction() as connection:
            for record in records:
                key = str(record.get("key") or record.get("id") or "")
                if key:
                    connection.execute("DELETE FROM cache_entries WHERE key=?", (key,))
        for record in records:
            key = str(record.get("key") or record.get("id") or "")
            if key:
                self._remove_artifact(key)
        return len(records)

    def invalidate(self, kind: str | None = None, key: str | None = None) -> int:
        if kind is not None:
            kind = _text(kind, "kind")
        if key is not None:
            key = _validate_key(key)
        records = self._all_records()
        selected = [
            record
            for record in records
            if (kind is None or record.get("kind") == kind)
            and (key is None or str(record.get("key") or record.get("id")) == key)
        ]
        return self._remove_records(selected)

    def status(self) -> dict[str, Any]:
        records = self._all_records()
        active = 0
        expired = 0
        total_bytes = 0
        for record in records:
            try:
                is_expired_record = is_expired(record.get("expires_at"))
            except ValueError as exc:
                raise StateError("malformed cache expiration") from exc
            if is_expired_record:
                expired += 1
            else:
                active += 1
            total_bytes += len(canonical_json(_record_value(record)).encode("utf-8"))
        return {
            "entries": len(records),
            "count": len(records),
            "active": active,
            "expired": expired,
            "bytes": total_bytes,
            "size_bytes": total_bytes,
            "directory": str(self.cache_dir),
            "cache_dir": str(self.cache_dir),
        }

    def inspect(self) -> list[dict[str, Any]]:
        result = []
        for record in self._all_records():
            expires_at = record.get("expires_at")
            try:
                expired = is_expired(expires_at)
            except ValueError as exc:
                raise StateError("malformed cache expiration") from exc
            result.append(
                {
                    "key": record.get("key") or record.get("id"),
                    "kind": record.get("kind"),
                    "value": _record_value(record),
                    "metadata": redact(record.get("metadata") or {}),
                    "expires_at": expires_at,
                    "expired": expired,
                    "created_at": record.get("created_at"),
                    "updated_at": record.get("updated_at"),
                }
            )
        return result

    def prune(self) -> int:
        expired_records = []
        for record in self._all_records():
            try:
                if is_expired(record.get("expires_at")):
                    expired_records.append(record)
            except ValueError as exc:
                raise StateError("malformed cache expiration") from exc
        return self._remove_records(expired_records)
