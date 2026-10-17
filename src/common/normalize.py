"""Shared helpers: config loading and ID / URL normalization.

Team rules: NPI = 10-digit string, TIN = 9-digit zero-padded string.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import yaml

ROOT = Path(__file__).resolve().parents[2]  # project root (folder with config.yaml)


def load_config(path: Path | None = None) -> dict:
    """Read config.yaml and turn every entry of `paths` into an absolute Path under ROOT."""
    path = path or ROOT / "config.yaml"
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["paths"] = {k: ROOT / v for k, v in cfg.get("paths", {}).items()}
    return cfg


_DIGITS = re.compile(r"\D")


def norm_npi(x) -> str | None:
    """Return a 10-digit NPI string, or None if the value is not a valid-length NPI."""
    if x is None:
        return None
    s = _DIGITS.sub("", str(x).split(".")[0])
    return s if len(s) == 10 else None


def norm_tin(x) -> str | None:
    """Return a 9-digit zero-padded TIN string, or None if it cannot be one."""
    if x is None:
        return None
    s = _DIGITS.sub("", str(x).split(".")[0])
    if not s or len(s) > 9:
        return None
    return s.zfill(9)


def norm_url(url: str) -> str:
    """Canonical form of a file URL, used for de-duplication.

    Drops the query string and fragment (signed URLs carry expiring tokens there),
    lower-cases scheme and host, and decodes %-escapes in the path.
    """
    u = urlsplit(url.strip())
    return urlunsplit((u.scheme.lower(), u.netloc.lower(), unquote(u.path), "", ""))


def file_name_from_url(url: str) -> str:
    """Last path segment of a URL (e.g. '2026-09-01_..._in-network-rates.json.gz')."""
    return unquote(urlsplit(url).path).rstrip("/").rsplit("/", 1)[-1]
