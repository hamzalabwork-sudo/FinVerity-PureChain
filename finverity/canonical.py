"""Canonicalization E* = Canon(E) and commitment H = SHA256(E*).

Implements the RFC 8785 (JSON Canonicalization Scheme) profile used by FinVerity:
  * object members sorted by key (keys are restricted to ASCII, so code-point order
    equals the UTF-16 order required by RFC 8785),
  * no insignificant whitespace, UTF-8 output,
  * strings normalized to Unicode NFC before serialization,
  * floating-point numbers are rejected: monetary amounts must be decimal strings,
    which removes the float-formatting ambiguity that RFC 8785 otherwise has to specify.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any


class CanonicalizationError(ValueError):
    pass


def _normalize(value: Any, path: str = "$") -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str) or not k.isascii():
                raise CanonicalizationError(f"{path}: keys must be ASCII strings, got {k!r}")
            out[k] = _normalize(v, f"{path}.{k}")
        return out
    if isinstance(value, (list, tuple)):
        return [_normalize(v, f"{path}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        if abs(value) > 2**53 - 1:
            raise CanonicalizationError(f"{path}: integer outside IEEE-754 safe range")
        return value
    if isinstance(value, float):
        raise CanonicalizationError(f"{path}: floats are not allowed; use decimal strings")
    raise CanonicalizationError(f"{path}: unsupported type {type(value).__name__}")


def canonicalize(obj: Any) -> bytes:
    return json.dumps(
        _normalize(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def commit(obj: Any) -> bytes:
    """H_i = SHA256(Canon(E_i))"""
    return sha256(canonicalize(obj))
