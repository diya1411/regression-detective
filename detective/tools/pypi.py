"""Package-registry tool: release history from the PyPI JSON API (or a pinned local mirror)."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version


@dataclass
class Release:
    version: str
    uploaded: datetime
    yanked: bool = False


class Registry:
    """PyPI JSON API client with an on-disk cache.

    `mirror` points at a directory containing `index.json` ({name: [{version, upload_time}]})
    plus wheel files; it plays the role of the pinned registry mirror from the design (§9) and is
    consulted before PyPI.
    """

    def __init__(self, cache_dir: Path, mirror: Path | None = None, base_url: str = "https://pypi.org/pypi",
                 offline: bool = False):
        self.cache_dir = cache_dir / "pypi"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.mirror = mirror
        self.base_url = base_url.rstrip("/")
        self.offline = offline
        self._mirror_index: dict | None = None
        self.calls: list[str] = []

    def _mirror(self) -> dict:
        if self._mirror_index is None:
            self._mirror_index = {}
            if self.mirror and (self.mirror / "index.json").exists():
                raw = json.loads((self.mirror / "index.json").read_text())
                self._mirror_index = {canonicalize_name(k): v for k, v in raw.items()}
        return self._mirror_index

    def releases(self, name: str) -> list[Release]:
        key = canonicalize_name(name)
        if key in self._mirror():
            self.calls.append(f"mirror:{key}")
            return [Release(r["version"], _parse_time(r["upload_time"]), r.get("yanked", False))
                    for r in self._mirror()[key]]
        cache = self.cache_dir / f"{key}.json"
        data = None
        if cache.exists() and time.time() - cache.stat().st_mtime < 6 * 3600:
            data = json.loads(cache.read_text())
        elif not self.offline:
            try:
                self.calls.append(f"pypi:{key}")
                r = httpx.get(f"{self.base_url}/{key}/json", timeout=20, follow_redirects=True)
                if r.status_code == 200:
                    data = r.json()
                    cache.write_text(json.dumps({"releases": data.get("releases", {})}))
            except httpx.HTTPError:
                data = None
        if data is None and cache.exists():
            data = json.loads(cache.read_text())
        if not data:
            return []
        out = []
        for ver, files in (data.get("releases") or {}).items():
            if not files:
                continue
            times = [_parse_time(f["upload_time_iso_8601"] if "upload_time_iso_8601" in f else f["upload_time"])
                     for f in files]
            out.append(Release(ver, min(times), all(f.get("yanked", False) for f in files)))
        return out

    def version_at(self, name: str, spec: str, when: datetime) -> str | None:
        """Newest non-yanked, non-prerelease version matching spec that existed at `when`."""
        try:
            sset = SpecifierSet(spec or "")
        except InvalidSpecifier:
            sset = SpecifierSet("")
        best: Version | None = None
        for r in self.releases(name):
            if r.yanked or r.uploaded > when:
                continue
            try:
                v = Version(r.version)
            except InvalidVersion:
                continue
            if v.is_prerelease or v not in sset:
                continue
            if best is None or v > best:
                best = v
        return str(best) if best else None

    def releases_between(self, name: str, start: datetime, end: datetime) -> list[Release]:
        return sorted([r for r in self.releases(name) if start < r.uploaded <= end and not r.yanked],
                      key=lambda r: r.uploaded)


def _parse_time(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
