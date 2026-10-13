"""Build and verify the experimental Debian package using only Python stdlib."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
from pathlib import Path, PurePosixPath
import tarfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "linux-intelbras"
VERSION = "0.12.0-1"
CONTROL = f"""Package: {PACKAGE}
Version: {VERSION}
Architecture: all
Maintainer: Jeferson Lackman <318690780+poajefersonld-1983@users.noreply.github.com>
Conflicts: simnext-linux
Replaces: simnext-linux
Section: video
Priority: optional
Depends: python3 (>= 3.11), python3-pyqt6 (>= 6.8), python3-pyqt6.qtmultimedia (>= 6.8), ffmpeg (>= 7.1)
Description: Linux-Intelbras camera monitor for Debian 13
 Independent Linux implementation with configurable RTSP playback.
 Experimental release; device compatibility requires validation.
"""


def tar_gz(files: dict[str, tuple[bytes, int]]) -> bytes:
    """Create sorted, reproducible root-owned tar members and gzip header."""
    stream = io.BytesIO()
    with gzip.GzipFile(fileobj=stream, mode="wb", filename="", mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            directories = sorted({parent.as_posix() for name in files
                                  for parent in PurePosixPath(name).parents
                                  if parent.as_posix() != "."})
            for name in directories:
                info = tarfile.TarInfo(name)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = "root"
                archive.addfile(info)
            for name, (content, mode) in sorted(files.items()):
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mode = mode
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = "root"
                archive.addfile(info, io.BytesIO(content))
    return stream.getvalue()


def ar_member(name: str, content: bytes) -> bytes:
    header = (
        f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(content):<10}`\n"
    ).encode("ascii")
    if len(header) != 60:
        raise ValueError("Invalid ar member header")
    return header + content + (b"\n" if len(content) % 2 else b"")


def verify(path: Path, expected: dict[str, tuple[bytes, int]]) -> None:
    """Read back ar and both tar archives; reject unexpected paths and metadata."""
    raw = path.read_bytes()
    if raw[:8] != b"!<arch>\n":
        raise ValueError("Missing ar signature")
    members: dict[str, bytes] = {}
    offset = 8
    while offset < len(raw):
        header = raw[offset : offset + 60]
        if len(header) != 60 or header[-2:] != b"`\n":
            raise ValueError("Invalid ar header")
        name = header[:16].decode("ascii").strip().removesuffix("/")
        size = int(header[48:58])
        offset += 60
        content = raw[offset : offset + size]
        if len(content) != size or name in members:
            raise ValueError("Truncated or duplicate ar member")
        members[name] = content
        offset += size
        if size % 2:
            if raw[offset : offset + 1] != b"\n":
                raise ValueError("Invalid ar padding")
            offset += 1
    if offset != len(raw) or list(members) != ["debian-binary", "control.tar.gz", "data.tar.gz"]:
        raise ValueError("Unexpected Debian archive structure")
    if members["debian-binary"] != b"2.0\n":
        raise ValueError("Unsupported Debian format")
    inventories = {}
    for name in ["control.tar.gz", "data.tar.gz"]:
        if members[name][4:8] != b"\0\0\0\0":
            raise ValueError("Non-deterministic gzip timestamp")
        observed = {}
        directories = set()
        with tarfile.open(fileobj=io.BytesIO(members[name]), mode="r:gz") as archive:
            for item in archive:
                selected = PurePosixPath(item.name)
                if selected.is_absolute() or ".." in selected.parts or not (item.isfile() or item.isdir()):
                    raise ValueError(f"Unsafe tar member: {item.name}")
                if item.name in observed or item.name in directories or item.uid or item.gid or item.mtime:
                    raise ValueError("Duplicate member or non-deterministic ownership")
                if item.mode not in (0o644, 0o755):
                    raise ValueError("Unexpected permissions")
                if item.isdir():
                    if item.mode != 0o755:
                        raise ValueError("Unexpected directory permissions")
                    directories.add(item.name)
                    continue
                if any(parent.as_posix() not in directories for parent in selected.parents
                       if parent.as_posix() != "."):
                    raise ValueError("Missing parent directory before file")
                file = archive.extractfile(item)
                if file is None:
                    raise ValueError("Missing tar content")
                observed[item.name] = (file.read(), item.mode)
        inventories[name] = observed
    if inventories["data.tar.gz"] != expected:
        raise ValueError("Packaged data differs from selected source files")
    control_files = inventories["control.tar.gz"]
    if set(control_files) != {"control", "md5sums"}:
        raise ValueError("Unexpected control files")
    if control_files["control"][0] != CONTROL.encode("utf-8"):
        raise ValueError("Incorrect package metadata")
    for line in control_files["md5sums"][0].decode("ascii").splitlines():
        digest, name = line.split("  ", 1)
        if hashlib.md5(expected[name][0]).hexdigest() != digest:
            raise ValueError(f"md5 mismatch: {name}")


def build(output: Path) -> Path:
    source = ROOT / "src" / "simnext_linux"
    files: dict[str, tuple[bytes, int]] = {}
    for path in sorted(source.rglob("*.py")):
        if "__pycache__" in path.parts or path.is_symlink():
            continue
        name = "usr/share/linux-intelbras/simnext_linux/" + path.relative_to(source).as_posix()
        files[name] = (path.read_bytes(), 0o644)
    if "usr/share/linux-intelbras/simnext_linux/__main__.py" not in files:
        raise ValueError("Application __main__.py is missing")
    files["usr/bin/linux-intelbras"] = ((ROOT / "packaging/linux-intelbras").read_bytes().replace(b"\r\n", b"\n"), 0o755)
    files["usr/bin/monitor-linux"] = (b'#!/bin/sh\nexec linux-intelbras "$@"\n', 0o755)
    files["usr/share/applications/linux-intelbras.desktop"] = ((ROOT / "packaging/linux-intelbras.desktop").read_bytes().replace(b"\r\n", b"\n"), 0o644)
    license_path = ROOT / "LICENSE"
    if license_path.is_file():
        files[f"usr/share/doc/{PACKAGE}/copyright"] = ((ROOT / "COPYRIGHT").read_bytes() + b"\n" + license_path.read_bytes(), 0o644)
    files[f"usr/share/doc/{PACKAGE}/isic-transport-MIT"] = ((ROOT / "packaging/isic-transport-LICENSE").read_bytes(), 0o644)
    sums = "".join(f"{hashlib.md5(data).hexdigest()}  {name}\n" for name, (data, _) in sorted(files.items()))
    control = tar_gz({"control": (CONTROL.encode("utf-8"), 0o644), "md5sums": (sums.encode("ascii"), 0o644)})
    data = tar_gz(files)
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"{PACKAGE}_{VERSION}_all.deb"
    path.write_bytes(b"!<arch>\n" + ar_member("debian-binary", b"2.0\n") + ar_member("control.tar.gz", control) + ar_member("data.tar.gz", data))
    verify(path, files)
    print(f"{path}\n{path.stat().st_size} bytes\nSHA-256 {hashlib.sha256(path.read_bytes()).hexdigest()}\nVerified {len(files)} data files; root ownership and 644/755 permissions.")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    build(parser.parse_args().output)
