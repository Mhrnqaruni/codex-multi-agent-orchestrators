"""Inspect wheel contents against the tracked package; never upload artifacts."""

from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    tracked = (
        subprocess.run(
            ["git", "ls-files", "-z", "codex_orchestrators"], cwd=root, check=True, capture_output=True
        )
        .stdout.decode()
        .split("\0")
    )
    expected = {name for name in tracked if name.endswith(".py")}
    wheels = list((root / "dist").glob("*.whl"))
    if not wheels or not expected:
        raise RuntimeError("Build a wheel and stage the complete package before inspection")
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            actual = {name for name in names if name.startswith("codex_orchestrators/")}
            if actual != expected:
                raise RuntimeError("Wheel package contents do not match tracked source")
            for name in names:
                path = Path(name)
                if path.is_absolute() or ".." in path.parts:
                    raise RuntimeError("Unsafe wheel member")
                if name in actual:
                    if archive.read(name) != (root / name).read_bytes():
                        raise RuntimeError("Wheel source differs from candidate")
                elif ".dist-info/" not in name:
                    raise RuntimeError("Unexpected wheel content")
            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            if len(metadata_names) != 1:
                raise RuntimeError("Missing or ambiguous package metadata")
            metadata = archive.read(metadata_names[0]).decode()
            if "License-Expression: LicenseRef-Codex-Orchestrators-Noncommercial-1.0" not in metadata:
                raise RuntimeError("License metadata mismatch")
            if "Requires-Dist:" in metadata:
                raise RuntimeError("Unexpected runtime dependency")
            prefix = metadata_names[0].removesuffix("METADATA")
            allowed_metadata = {
                prefix + name
                for name in (
                    "METADATA",
                    "WHEEL",
                    "entry_points.txt",
                    "top_level.txt",
                    "RECORD",
                    "licenses/LICENSE",
                )
            }
            if set(names) - actual - allowed_metadata or len(names) != len(set(names)):
                raise RuntimeError("Unexpected or duplicate wheel metadata")
            if archive.read(prefix + "licenses/LICENSE") != (root / "LICENSE").read_bytes():
                raise RuntimeError("Packaged license text differs from candidate")
        print(f"Wheel content/source/license check passed: {wheel.name}")
    tracked_all = set(
        subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True)
        .stdout.decode()
        .split("\0")
    )
    sdists = list((root / "dist").glob("*.tar.gz"))
    if not sdists:
        raise RuntimeError("Build an sdist before inspection")
    for sdist in sdists:
        with tarfile.open(sdist) as archive:
            for member in archive.getmembers():
                parts = Path(member.name).parts
                if Path(member.name).is_absolute() or ".." in parts or member.issym() or member.islnk():
                    raise RuntimeError("Unsafe sdist member")
                if member.isdir():
                    continue
                relative = Path(*parts[1:]).as_posix()
                generated = relative in {"PKG-INFO", "setup.cfg"} or relative.startswith(
                    "codex_multi_agent_orchestrators.egg-info/"
                )
                if not generated and relative not in tracked_all:
                    raise RuntimeError("Untracked file in sdist")
                if not member.isfile():
                    raise RuntimeError("Nonregular sdist member")
        print(f"Sdist tracked-content check passed: {sdist.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
