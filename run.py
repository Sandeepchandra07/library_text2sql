import os
import signal
import subprocess
import sys
import time

import uvicorn

ROOT = os.path.dirname(os.path.abspath(__file__))
AGENT_FILE = os.getenv("AGENT_FILE", os.path.join(ROOT, "livekit_agent", "agent.py"))
# "dev" = development, "start" = production
AGENT_MODE = os.getenv("AGENT_MODE", "dev")
PORT = int(os.getenv("PORT", "8000"))


MODULE = os.getenv("AGENT_MODULE", "livekit_agent.agent")


def start_agent() -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.Popen(
        [sys.executable, "-m", MODULE, AGENT_MODE],
        cwd=ROOT,
        env=env,
    )

    time.sleep(2)
    if proc.poll() is not None:
        raise RuntimeError(f"LiveKit agent start hote hi band ho gaya (exit code {proc.returncode})")
    return proc


def stop_agent(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


if __name__ == "__main__":
    agent_proc = start_agent()
    try:
        uvicorn.run("app.api:app", host="0.0.0.0", port=8000, workers=1, log_level="info")
    finally:
        stop_agent(agent_proc)