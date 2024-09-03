import asyncio
import json
import os
import sys
import urllib.parse

from aiohttp import web

from typing import IO, Any, Optional


class SubprocessError(Exception):
    def __init__(
        self,
        stdout: Optional[bytes],
        stderr: Optional[bytes],
    ):
        self.stdout = stdout
        self.stderr = stderr

    def __str__(self):
        if self.stderr:
            return self.stderr.decode()
        if self.stdout:
            return self.stdout.decode()
        return "NO OUTPUT"

    def __repr__(self):
        if not self.stderr and not self.stdout:
            return "NO OUTPUT"

        result = ""
        if self.stdout:
            result += f"STDOUT: {self.stdout}"
        if self.stderr:
            result += f"STDERR: {self.stderr}"
        return result


async def update_git_repo(repo_dir: str):
    os.chdir(repo_dir)
    proc = await asyncio.subprocess.create_subprocess_shell(
        "git pull origin master",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    exit_code = await proc.wait()
    if exit_code != 0:
        raise SubprocessError(
            await proc.stdout.read() if proc.stdout else b"",
            await proc.stderr.read() if proc.stderr else b"",
        )


async def build_binary(repo_dir: str):
    os.chdir(repo_dir)
    proc = await asyncio.subprocess.create_subprocess_shell(
        "cargo build",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    exit_code = await proc.wait()
    if exit_code != 0:
        raise SubprocessError(
            await proc.stdout.read() if proc.stdout else b"",
            await proc.stderr.read() if proc.stderr else b"",
        )


async def get_repo_description(repo_dir: str):
    os.chdir(repo_dir)
    proc = await asyncio.subprocess.create_subprocess_shell(
        'git log -n 1 --pretty=format:"%H"',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    exit_code = await proc.wait()
    if exit_code == 0:
        assert proc.stdout
        result = await proc.stdout.read()
        return result.decode()

    raise SubprocessError(
        await proc.stdout.read() if proc.stdout else b"",
        await proc.stderr.read() if proc.stderr else b"",
    )


async def start_binary(
    binary_path: str,
    stdout_log: IO[Any],
    stderr_log: IO[Any],
):
    proc = await asyncio.subprocess.create_subprocess_exec(
        binary_path,
        stdout=stdout_log,
        stderr=stderr_log,
    )
    return proc


async def wait_for_process_or_signal(
    proc: asyncio.subprocess.Process,
    update_queue: asyncio.Queue[str],
) -> Optional[str]:
    proc_task = asyncio.create_task(proc.wait())
    kill_task = asyncio.create_task(update_queue.get())
    complete, _ = await asyncio.wait(
        [proc_task, kill_task], return_when=asyncio.FIRST_COMPLETED
    )

    kill_task.cancel()
    if proc_task in complete:
        return_code = proc_task.result()
        print(f"binary returned code {return_code}")
        return None
    else:
        proc.terminate()
        return_code = await proc_task
        print(f"binary was terminated with code {return_code}")

        new_commit = kill_task.result()
        while not update_queue.empty():
            new_commit = await update_queue.get()

        return new_commit


async def handle_webhook(queue: asyncio.Queue[str], request: web.Request):
    raw_data = await request.text()
    converted_data = urllib.parse.unquote(raw_data)
    json_data = json.loads(converted_data[len("payload=") :])
    if json_data["ref"] == "refs/heads/master":
        await queue.put(json_data["after"])

    return web.Response()


async def webhook_runner(host: str, port: int, update_queue: asyncio.Queue[str]):
    app = web.Application()
    app.add_routes([web.post("/{_}", lambda x: handle_webhook(update_queue, x))])
    print(f"webhook listening at {host}:{port}")
    await web._run_app(app, host=host, port=port, print=lambda _: None)


async def binary_runner(repo_dir: str, log_dir: str, update_queue: asyncio.Queue[str]):
    await update_git_repo(repo_dir)
    await build_binary(repo_dir)

    commit = await get_repo_description(repo_dir)
    current_log_dir = os.path.join(log_dir, commit)
    if not os.path.isdir(current_log_dir):
        os.mkdir(current_log_dir)

    executable_path = os.path.join(repo_dir, "./target/debug/pumpkin")
    try_counter = 0
    while True:
        stdout_path = os.path.join(current_log_dir, f"stdout_{try_counter}.txt")
        stderr_path = os.path.join(current_log_dir, f"stderr_{try_counter}.txt")

        stdout_log = open(stdout_path, "w")
        stderr_log = open(stderr_path, "w")

        proc = await start_binary(
            executable_path,
            stdout_log,
            stderr_log,
        )
        print("The binary has started")

        new_commit = await wait_for_process_or_signal(proc, update_queue)

        stdout_log.close()
        stderr_log.close()

        if new_commit is None:
            print("The binary died for an unknown reason! Sleeping for 10 minutes.")
            await asyncio.sleep(10 * 60)
        else:
            print("New commit detected; rebuilding and restarting.")
            while not update_queue.empty():
                await update_git_repo(repo_dir)
                await build_binary(repo_dir)

            commit = await get_repo_description(repo_dir)
            current_log_dir = os.path.join(log_dir, commit)
            if not os.path.isdir(current_log_dir):
                os.mkdir(current_log_dir)

            try_counter = 0
            continue

        try_counter += 1


async def async_main(
    repo_path: str,
    log_path: str,
    webhook_host: str,
    webhook_port: int,
):
    update_queue = asyncio.Queue()
    webhook_task = asyncio.create_task(
        webhook_runner(webhook_host, webhook_port, update_queue)
    )
    binary_task = asyncio.create_task(binary_runner(repo_path, log_path, update_queue))

    completed, pending = await asyncio.wait(
        [webhook_task, binary_task], return_when=asyncio.FIRST_EXCEPTION
    )

    for complete in completed:
        if exc := complete.exception():
            raise exc

    for pend in pending:
        pend.cancel()

    await asyncio.wait(pending)


def main():
    repo_path = sys.argv[1]
    log_path = sys.argv[2]
    asyncio.run(
        async_main(
            os.path.realpath(repo_path),
            os.path.realpath(log_path),
            "127.0.0.1",
            8888,
        )
    )


if __name__ == "__main__":
    main()
