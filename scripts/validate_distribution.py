from __future__ import annotations

import argparse
import tarfile
import zipfile
from email.message import Message
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

PACKAGE_FILES = {
    "pytest_nats/__init__.py",
    "pytest_nats/_provisioning.py",
    "pytest_nats/_runtime.py",
    "pytest_nats/py.typed",
}


def parse_metadata(contents: bytes) -> Message:
    return BytesParser().parsebytes(contents)


def validate_metadata(metadata: Message, version: str) -> None:
    assert metadata["Name"] == "pytest-nats"
    assert metadata["Version"] == version
    assert metadata["Requires-Python"] == ">=3.11"
    assert metadata["License-Expression"] == "MIT"
    assert "Repository, https://github.com/m3nowak/pytest-nats" in metadata.get_all("Project-URL", [])
    assert "Issues, https://github.com/m3nowak/pytest-nats/issues" in metadata.get_all("Project-URL", [])
    classifiers = metadata.get_all("Classifier", [])
    for python_version in ("3.11", "3.12", "3.13", "3.14", "3.15"):
        assert f"Programming Language :: Python :: {python_version}" in classifiers
    assert "Framework :: Pytest" in classifiers
    assert "Typing :: Typed" in classifiers


def validate_sdist(path: Path, version: str) -> None:
    prefix = f"pytest_nats-{version}/"
    with tarfile.open(path, "r:gz") as archive:
        names = set(archive.getnames())
        assert f"{prefix}LICENSE" in names
        assert {f"{prefix}src/{name}" for name in PACKAGE_FILES} <= names
        metadata_file = archive.extractfile(f"{prefix}PKG-INFO")
        assert metadata_file is not None
        validate_metadata(parse_metadata(metadata_file.read()), version)


def validate_wheel(path: Path, version: str) -> None:
    metadata_name = f"pytest_nats-{version}.dist-info/METADATA"
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        assert PACKAGE_FILES <= names
        assert metadata_name in names
        license_files = [name for name in names if PurePosixPath(name).name == "LICENSE"]
        assert len(license_files) == 1
        validate_metadata(parse_metadata(archive.read(metadata_name)), version)


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate pytest-nats release distributions")
    parser.add_argument("version")
    parser.add_argument("dist", type=Path)
    args = parser.parse_args()

    expected_sdist = args.dist / f"pytest_nats-{args.version}.tar.gz"
    expected_wheel = args.dist / f"pytest_nats-{args.version}-py3-none-any.whl"
    distributions = [path for path in args.dist.iterdir() if path.name != ".gitignore"]
    assert sorted(distributions) == sorted((expected_sdist, expected_wheel))
    validate_sdist(expected_sdist, args.version)
    validate_wheel(expected_wheel, args.version)


if __name__ == "__main__":
    main()
