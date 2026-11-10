"""Bootstraps `uv` into ~/.local/bin and runs `uv sync` for agy-rpd-scoping-agent."""

import os
import platform
import shutil
import stat
import subprocess
import tarfile
import tempfile
import urllib.request

LOCAL_BIN = os.path.expanduser("~/.local/bin")
UV_BIN = os.path.join(LOCAL_BIN, "uv")
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


def ensure_uv() -> str:
    existing = shutil.which("uv") or (UV_BIN if os.path.exists(UV_BIN) else None)
    if existing:
        print(f"Found existing uv at: {existing}")
        return existing

    os.makedirs(LOCAL_BIN, exist_ok=True)
    arch = platform.machine()
    url = f"https://github.com/astral-sh/uv/releases/latest/download/uv-{arch}-unknown-linux-gnu.tar.gz"
    print(f"Downloading uv from {url} ...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tar_path = os.path.join(tmpdir, "uv.tar.gz")
        urllib.request.urlretrieve(url, tar_path)
        with tarfile.open(tar_path, "r:gz") as tar:
            for member in tar.getmembers():
                if os.path.basename(member.name) in ("uv", "uvx") and member.isfile():
                    extracted = tar.extractfile(member)
                    if extracted:
                        dest = os.path.join(LOCAL_BIN, os.path.basename(member.name))
                        with open(dest, "wb") as out_f:
                            shutil.copyfileobj(extracted, out_f)
                        os.chmod(dest, os.stat(dest).st_mode | stat.S_IEXEC)
    print(f"Installed uv to {UV_BIN}")
    return UV_BIN


def main() -> None:
    uv_path = ensure_uv()
    print(f"Running `{uv_path} sync` in {PROJECT_DIR} ...")
    subprocess.run([uv_path, "sync"], cwd=PROJECT_DIR, check=True)
    print("Environment synced successfully.")


if __name__ == "__main__":
    main()
