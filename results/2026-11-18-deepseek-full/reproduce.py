"""Повторить кампанию из корня проекта в новом каталоге результатов."""

import argparse
import getpass
import os
import secrets
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MODEL = "deepseek-ai/DeepSeek-V4.1-Flash-1017"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    token = secrets.token_urlsafe(32)
    server = dict(
        os.environ,
        LLM_API_KEY=getpass.getpass("Ключ DeepSeek: "),
        LLM_MODEL=MODEL,
        LLM_PROVIDER_URL="https://litellm-backend.deeppavlov.ai",
        CLIENT_TOKENS=token,
        GATEWAY_PORT="19880",
        SANDBOX_MANAGER_PORT="19881",
    )
    client = dict(os.environ, CLIENT_TOKEN=token, SANDBOX_TEST_URL="http://127.0.0.1:19881")
    compose = ["docker", "compose", "-p", "are-deepseek-cp2"]

    def run(name, *options):
        command = [
            sys.executable,
            "-m",
            "baf.runner",
            "--url",
            "http://127.0.0.1:19880",
            "--mount",
            "--output",
            str(args.output / f"{name}.jsonl"),
            *options,
        ]
        with (args.output / f"{name}.log").open("x") as log:
            return subprocess.run(
                command, env=client, stdout=log, stderr=subprocess.STDOUT
            ).returncode

    def round_run(number):
        return run(f"round-{number}", "--suite", "full")

    subprocess.run(["make", "prepare-workdir"], check=True)
    try:
        subprocess.run(
            [*compose, "up", "--build", "--wait", "--wait-timeout", "120"], env=server, check=True
        )
        if run("pilot", "--task", "B1.1"):
            raise SystemExit("Пробная B1.1 не прошла")
        with ThreadPoolExecutor(3) as pool:
            exit_codes = list(pool.map(round_run, (1, 2, 3)))
            return int(any(exit_codes))
    finally:
        subprocess.run([*compose, "down", "-v"], env=server, check=True)


if __name__ == "__main__":
    raise SystemExit(main())
