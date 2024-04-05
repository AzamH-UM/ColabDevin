#Rewritten sandbox.py to use subprocess instead of docker

import atexit
import os
import select
import sys
import time
import uuid
from collections import namedtuple
from typing import Dict, List, Tuple

import concurrent.futures

from opendevin import config

InputType = namedtuple("InputType", ["content"])
OutputType = namedtuple("OutputType", ["content"])

DIRECTORY_REWRITE = config.get("DIRECTORY_REWRITE")  # helpful for sandbox-in-sandbox scenarios
CONTAINER_IMAGE = config.get("SANDBOX_CONTAINER_IMAGE")

# FIXME: On some containers, the devin user doesn't have enough permission, e.g. to install packages
# How do we make this more flexible?
RUN_AS_DEVIN = config.get("RUN_AS_DEVIN").lower() != "false"
USER_ID = 1000
if config.get_or_none("SANDBOX_USER_ID") is not None:
    USER_ID = int(config.get_or_default("SANDBOX_USER_ID", ""))
elif hasattr(os, "getuid"):
    USER_ID = os.getuid()


class BackgroundCommand:
    def __init__(self, id: int, command: str, result, pid: int):
        self.id = id
        self.command = command
        self.result = result
        self.pid = pid

    def parse_sandbox_exec_output(self, logs: bytes) -> Tuple[bytes, bytes]:
        res = b""
        tail = b""
        i = 0
        byte_order = sys.byteorder
        while i < len(logs):
            prefix = logs[i : i + 8]
            if len(prefix) < 8:
                msg_type = prefix[0:1]
                if msg_type in [b"\x00", b"\x01", b"\x02", b"\x03"]:
                    tail = prefix
                break

            msg_type = prefix[0:1]
            padding = prefix[1:4]
            if (
                msg_type in [b"\x00", b"\x01", b"\x02", b"\x03"]
                and padding == b"\x00\x00\x00"
            ):
                msg_length = int.from_bytes(prefix[4:8], byteorder=byte_order)
                res += logs[i + 8 : i + 8 + msg_length]
                i += 8 + msg_length
            else:
                res += logs[i : i + 1]
                i += 1
        return res, tail

    def read_logs(self) -> str:
        # TODO: get an exit code if process is exited
        logs = b""
        last_remains = b""
        while True:
            ready_to_read, _, _ = select.select([self.result.output], [], [], 0.1)  # type: ignore[has-type]
            if ready_to_read:
                data = self.result.output.read(4096)  # type: ignore[has-type]
                if not data:
                    break
                chunk, last_remains = self.parse_sandbox_exec_output(last_remains + data)
                logs += chunk
            else:
                break
        return (logs + last_remains).decode("utf-8", errors="replace")


class SandboxInteractive:
    closed = False
    cur_background_id = 0
    background_commands: Dict[int, BackgroundCommand] = {}

    def __init__(
        self,
        workspace_dir: str | None = None,
        container_image: str | None = None,
        timeout: int = 120,
        id: str | None = None,
    ):
        if id is not None:
            self.instance_id = id
        else:
            self.instance_id = str(uuid.uuid4())
        if workspace_dir is not None:
            os.makedirs(workspace_dir, exist_ok=True)
            # expand to absolute path
            self.workspace_dir = os.path.abspath(workspace_dir)
        else:
            self.workspace_dir = os.getcwd()
            print(f"workspace unspecified, using current directory: {workspace_dir}")
        if DIRECTORY_REWRITE != "":
            parts = DIRECTORY_REWRITE.split(":")
            self.workspace_dir = self.workspace_dir.replace(parts[0], parts[1])
            print("Rewriting workspace directory to:", self.workspace_dir)

        # TODO: this timeout is actually essential - need a better way to set it
        # if it is too short, the container may still waiting for previous
        # command to finish (e.g. apt-get update)
        # if it is too long, the user may have to wait for a unnecessary long time
        self.timeout: int = timeout

        if container_image is None:
            self.container_image = CONTAINER_IMAGE
        else:
            self.container_image = container_image

        self.container_name = f"sandbox-{self.instance_id}"

        if RUN_AS_DEVIN:
            self.setup_devin_user()
        atexit.register(self.cleanup)

    def setup_devin_user(self):
        exit_code, logs = self.container.exec_run(
            [
                "/bin/bash",
                "-c",
                f'useradd --shell /bin/bash -u {USER_ID} -o -c "" -m devin',
            ],
            workdir="/workspace",
        )

    def get_exec_cmd(self, cmd: str) -> List[str]:
        if RUN_AS_DEVIN:
            return ["su", "devin", "-c", cmd]
        else:
            return ["/bin/bash", "-c", cmd]

    def read_logs(self, id) -> str:
        if id not in self.background_commands:
            raise ValueError("Invalid background command id")
        bg_cmd = self.background_commands[id]
        return bg_cmd.read_logs()

    def execute(self, cmd: str) -> Tuple[int, str]:
        # TODO: each execute is not stateful! We need to keep track of the current working directory
        def run_command(container, command):
            return container.exec_run(command,workdir="/workspace")
        # Use ThreadPoolExecutor to control command and set timeout
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(run_command, self.container, self.get_exec_cmd(cmd))
            try:
                exit_code, logs = future.result(timeout=self.timeout)
            except concurrent.futures.TimeoutError:
                print("Command timed out, killing process...")
                pid = self.get_pid(cmd)
                if pid is not None:
                    self.container.exec_run(
                        f"kill -9 {pid}", workdir="/workspace"
                    )
                return -1, f"Command: \"{cmd}\" timed out"
        return exit_code, logs.decode("utf-8")

    def execute_in_background(self, cmd: str) -> BackgroundCommand:
        result = self.container.exec_run(
            self.get_exec_cmd(cmd), socket=True, workdir="/workspace"
        )
        result.output._sock.setblocking(0)
        pid = self.get_pid(cmd)
        bg_cmd = BackgroundCommand(self.cur_background_id, cmd, result, pid)
        self.background_commands[bg_cmd.id] = bg_cmd
        self.cur_background_id += 1
        return bg_cmd

    def get_pid(self, cmd):
        exec_result = self.container.exec_run("ps aux")
        processes = exec_result.output.decode('utf-8').splitlines()
        cmd = " ".join(self.get_exec_cmd(cmd))

        for process in processes:
            if cmd in process:
                pid = process.split()[1] # second column is the pid
                return pid
        return None

    def kill_background(self, id: int) -> BackgroundCommand:
        if id not in self.background_commands:
            raise ValueError("Invalid background command id")
        bg_cmd = self.background_commands[id]
        if bg_cmd.pid is not None:
            self.container.exec_run(
                f"kill -9 {bg_cmd.pid}", workdir="/workspace"
            )
        bg_cmd.result.output.close()
        self.background_commands.pop(id)
        return bg_cmd


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Interactive Sandbox container")
    parser.add_argument(
        "-d",
        "--directory",
        type=str,
        default=None,
        help="The directory to mount as the workspace in the Sandbox container.",
    )
    args = parser.parse_args()

    try:
        sandbox_interactive = SandboxInteractive(
            workspace_dir=args.directory,
        )
    except Exception as e:
        print(f"Failed to start Sandbox container: {e}")
        sys.exit(1)

    print("Interactive Sandbox container started. Type 'exit' or use Ctrl+C to exit.")

    bg_cmd = sandbox_interactive.execute_in_background(
        "while true; do echo 'dot ' && sleep 1; done"
    )

    sys.stdout.flush()
    try:
        while True:
            try:
                user_input = input(">>> ")
            except EOFError:
                print("\nExiting...")
                break
            if user_input.lower() == "exit":
                print("Exiting...")
                break
            if user_input.lower() == "kill":
                sandbox_interactive.kill_background(bg_cmd.id)
                print("Background process killed")
                continue
            exit_code, output = sandbox_interactive.execute(user_input)
            print("exit code:", exit_code)
            print(output + "\n", end="")
            if bg_cmd.id in sandbox_interactive.background_commands:
                logs = sandbox_interactive.read_logs(bg_cmd.id)
                print("background logs:", logs, "\n")
            sys.stdout.flush()
    except KeyboardInterrupt:
        print("\nExiting...")
    sandbox_interactive.close()
