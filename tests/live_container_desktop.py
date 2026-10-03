"""Run the rendered native controls/trade/recovery regression inside the desktop image.

Compile test classes with ./gradlew testClasses before invoking this developer probe.
"""

import argparse
from pathlib import Path

from civ_agent.container_support import mount, podman


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--image", default="localhost/civ-agent:desktop")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output_dir.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (output / "probe").exists():
        parser.error("Select a new output directory")
    script = """
import os, pathlib, shutil, subprocess
shutil.copytree('/opt/unciv/engine', '/output/engine')
assets = pathlib.Path('/output/engine/android/assets')
env = dict(os.environ, DISPLAY=':98')
x = subprocess.Popen(['Xvfb', ':98', '-screen', '0', '1280x800x24', '-nolisten', 'tcp'])
try:
    import time
    deadline=time.monotonic()+10
    while not pathlib.Path('/tmp/.X11-unix/X98').exists():
        if time.monotonic()>deadline: raise RuntimeError('Xvfb did not start')
        time.sleep(.1)
    command=['java','-XX:MaxRAMPercentage=65','-Djava.awt.headless=false',
             '-Duncivai.assetsDir='+str(assets),'-cp','/test-classes:/opt/unciv/lib/*',
             'uncivai.ManagedDesktopProbe','/output/probe']
    result=subprocess.run(command,env=env,cwd=assets,timeout=180)
    raise SystemExit(result.returncode)
finally:
    x.terminate()
    x.wait(timeout=10)
"""
    result = podman(
        "run",
        "--rm",
        "--network=none",
        "--userns=keep-id",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=512m,mode=1777",
        "--memory=6g",
        "--cpus=2",
        "--pids-limit=512",
        *mount(root / "build/classes/kotlin/test", "/test-classes"),
        *mount(output, "/output", writable=True),
        "--entrypoint",
        "/opt/app/.venv/bin/python",
        args.image,
        "-c",
        script,
        timeout=210,
        check=False,
    )
    (output / "probe.log").write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f"Rendered probe failed; inspect {output / 'probe.log'}")
    print((output / "probe/COMPLETE").read_text())


if __name__ == "__main__":
    main()
