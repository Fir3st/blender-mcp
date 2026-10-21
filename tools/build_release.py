"""Build local source/addon archives using only the Python standard library."""

import ast
import hashlib
import io
from pathlib import Path
import tarfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
FILES = [
    "addon.py", "pyproject.toml", "uv.lock", "README.md", "CHANGELOG.md", "docs/usage.md",
    "docs/clients.md", "docs/troubleshooting.md", "LICENSE", "NOTICE",
    "src/blender_mcp_local/__init__.py", "src/blender_mcp_local/server.py", "src/blender_mcp_local/advanced.py",
    "src/blender_mcp_local/production.py", "src/blender_mcp_local/bridge_log.py",
    "src/blender_mcp_local/error_log.py", "tests/test_error_log.py", "tests/test_bridge_log.py",
    "tests/test_lifecycle.py", "logs/README.md",
    "tests/test_server.py", "tests/test_addon_transport.py", "tests/blender_host.py", "tests/run_blender_integration.py",
    "tools/build_release.py",
]


def main():
    tree = ast.parse((ROOT / "src/blender_mcp_local/__init__.py").read_text())
    version = ast.literal_eval(tree.body[0].value)
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    source = output / f"blender_mcp_local-{version}.tar.gz"
    prefix = f"blender_mcp_local-{version}"
    with tarfile.open(source, "w:gz") as archive:
        for filename in FILES:
            archive.add(ROOT / filename, arcname=f"{prefix}/{filename}", recursive=False)
        metadata = f"Metadata-Version: 2.3\nName: blender-mcp-local\nVersion: {version}\nSummary: Tool-first local Blender MCP\nLicense: Apache-2.0\nClassifier: License :: OSI Approved :: Apache Software License\nRequires-Python: >=3.10\nRequires-Dist: mcp>=1.27.0,<2\nRequires-Dist: pydantic>=2.11,<3\n"
        data = metadata.encode()
        info = tarfile.TarInfo(f"{prefix}/PKG-INFO")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    addon = output / f"blender-mcp-local-addon-{version}.zip"
    with zipfile.ZipFile(addon, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(ROOT / "addon.py", "blender_mcp_local_addon/__init__.py")
        archive.write(ROOT / "LICENSE", "blender_mcp_local_addon/LICENSE")
        archive.write(ROOT / "NOTICE", "blender_mcp_local_addon/NOTICE")
    sums = []
    for artifact in (source, addon):
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        sums.append(f"{digest}  {artifact.name}")
        print(artifact.relative_to(ROOT))
    (output / "SHA256SUMS").write_text("\n".join(sums) + "\n")


if __name__ == "__main__":
    main()
