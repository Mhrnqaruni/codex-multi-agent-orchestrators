"""Inspect wheel contents against the tracked package; never upload artifacts."""

from pathlib import Path
import subprocess
import sys
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
        print(f"Wheel content/source/license check passed: {wheel.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
