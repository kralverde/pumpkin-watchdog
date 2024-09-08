import asyncio
import json
import os
import sys
import urllib.parse

from aiohttp import web

from typing import IO, Any, List, Optional, Union


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
                result += "\n\n"
        if self.stderr:
            result += f"STDERR: {self.stderr}"
        return result


async def update_rust():
    print("updating rust")
    proc = await asyncio.subprocess.create_subprocess_shell(
        "rustup update",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    exit_code = await proc.wait()
    if exit_code != 0:
        raise SubprocessError(
            await proc.stdout.read() if proc.stdout else b"",
            await proc.stderr.read() if proc.stderr else b"",
        )


async def update_git_repo(repo_dir: str):
    print("updating repo")
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
    print("building binary")
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
        'git log -n 1 --pretty=format:"%H\n%s"',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    exit_code = await proc.wait()
    if exit_code == 0:
        assert proc.stdout
        result = await proc.stdout.read()
        return result.decode().splitlines()

    raise SubprocessError(
        await proc.stdout.read() if proc.stdout else b"",
        await proc.stderr.read() if proc.stderr else b"",
    )


async def start_binary(
    binary_path: str,
    stdout_log: IO[Any],
    stderr_log: IO[Any],
):
    env = os.environ.copy()
    env["RUST_BACKTRACE"] = "1"
    proc = await asyncio.subprocess.create_subprocess_exec(
        binary_path,
        stdout=stdout_log,
        stderr=stderr_log,
        env=env,
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
    if request.headers.get("X-GitHub-Event") == "push":
        raw_data = await request.text()
        converted_data = urllib.parse.unquote(raw_data)
        json_data = json.loads(converted_data[len("payload=") :])
        if json_data["repository"]["full_name"] in (
            "kralverde/Pumpkin",
            "Snowiiii/Pumpkin",
        ):
            print(f'commit detected on {json_data["ref"]}')
            if json_data["ref"] == "refs/heads/master":
                await queue.put(json_data["after"])

    return web.Response()


INDEX_DATA = """<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="color-scheme" content="light dark" />
    <title>Nightly PumpkinMC</title>
    <meta name="description" content="A nightly pumpkin minecraft server." />

    <!-- Pico.css -->
    <link
      rel="stylesheet"
      href="https://cdn.jsdelivr.net/npm/@picocss/pico@2.0.6/css/pico.min.css"
    />
  </head>

  <body>
    <div style="margin:15px;">
        <h1>A Nightly PumpkinMC Server</h1>
        {{error}}
        <p>This server is running <a href=https://github.com/Snowiiii/Pumpkin>Pumpkin MC</a> -- a Rust-written vanilla minecraft server -- straight from the master branch, updated every couple of hours (currently running <a href="https://github.com/Snowiiii/Pumpkin/commit/{{commit}}">({{short_commit}}) {{name}}</a>). The server is running in debug mode, so it will be less efficient/slower than any release builds!</p>
        <p>You can join and test with your Minecraft client at <b>pumpkin.kralverde.dev</b> on port <b>25565</b>. The goal of this particular server is just to have something public-facing to have lay-users try out and to stress test and see what real-world issues may come up. Feel free to do whatever to the <b>Minecraft</b> server; after all, the best way to find bugs is to open it to the public :p</p>
        <p>Logs can be found <a href=/logs>here</a> and are sorted by the commit that was running and the count of each (re)start of the Pumpkin binary. The current instance's logs can be found under the current commit hash directory with the highest number. The current STDOUT log can be found <a href="/logs/{{commit}}/stdout_{{count}}.txt">here</a> and the current STDERR log can be found <a href="/logs/{{commit}}/stderr_{{count}}.txt">here</a>.</p>
    </div>
  </body>
</html>"""


async def handle_index(
    commit_wrapper: List[Union[str, int]],
    request: web.Request,
):
    return web.Response(
        body=INDEX_DATA.replace("{{commit}}", str(commit_wrapper[0]))
        .replace("{{count}}", str(commit_wrapper[1]))
        .replace("{{name}}", str(commit_wrapper[2]))
        .replace("{{short_commit}}", str(commit_wrapper[0])[:8])
        .replace(
            "{{error}}",
            ""
            if not commit_wrapper[3]
            else f'<p style="color:red;"><b>{commit_wrapper[3]}</b></p>',
        ),
        content_type="text/html",
    )


async def webhook_runner(
    host: str,
    port: int,
    commit_wrapper: List[Union[str, int]],
    update_queue: asyncio.Queue[str],
):
    app = web.Application()
    app.add_routes(
        [
            web.post("/watchdog", lambda x: handle_webhook(update_queue, x)),
            web.get("/", lambda x: handle_index(commit_wrapper, x)),
        ]
    )
    print(f"webhook listening at {host}:{port}")
    await web._run_app(app, host=host, port=port, print=lambda _: None)


class MCException(Exception):
    pass


async def mc_read_var_int(reader: asyncio.StreamReader):
    value = 0
    position = 0
    bytes_read = 0
    while True:
        current_byte = (await reader.read(1))[0]
        bytes_read += 1
        value |= (current_byte & 0x7F) << position
        if (current_byte & 0x80) == 0:
            break
        position += 7
        if position >= 32:
            raise MCException("VarInt too big")
    return value, bytes_read


def mc_var_int_length(num: int):
    length = 0
    while True:
        length += 1
        if (num & ~0x7F) == 0:
            break
        num >>= 7
    return length


async def mc_write_var_int(num: int, writer: asyncio.StreamWriter):
    while True:
        if (num & ~0x7F) == 0:
            writer.write(bytes([num]))
            return
        writer.write(bytes([(num & 0x7F) | 0x80]))
        num >>= 7


async def handle_mc(
    current_error: List[str], reader: asyncio.StreamReader, writer: asyncio.StreamWriter
):
    try:
        packet_length, _ = await mc_read_var_int(reader)
        packet_id, id_bytes = await mc_read_var_int(reader)

        reason_text = f"{current_error[0]}"
        data = {"text": reason_text}

        if packet_id == 0:
            protocol_version = await mc_read_var_int(reader)
            server_address_length = await mc_read_var_int(reader)
            if server_address_length[0] > 255:
                raise MCException("Server address is too big")
            _ = await reader.read(server_address_length[0])
            _ = await reader.read(2)
            next_state = await mc_read_var_int(reader)

            real_packet_length = (
                id_bytes
                + protocol_version[1]
                + server_address_length[1]
                + server_address_length[0]
                + 2
                + next_state[1]
            )

            if real_packet_length != packet_length:
                raise MCException(
                    f"Bad packet length ({real_packet_length} vs {packet_length})"
                )

            if next_state[0] == 1:
                data = {
                    "version": {
                        "name": "Any",
                        "protocol": protocol_version[0],
                    },
                    "players": {
                        "max": 0,
                        "online": 0,
                        "sample": [],
                    },
                    "description": {
                        "text": reason_text,
                    },
                    "favicon": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAMAAACdt4HsAAAABGdBTUEAALGPC/xhBQAAAAFzUkdCAK7OHOkAAABjUExURQAAAPgxL/MwLfcwLvgwLvcwLvgwLvgxL/gwLvgwL/AvLfcwLu8vLfgxLvgwLusuLOsuK+8vLOwuLPIvLfEvLfMwLvQwLvcwLuwuLPgxL+8vLfcwL+8uLPgwLvgwL/gwL/gxL1kRqKYAAAAgdFJOUwDBT4vBwYn+9vZPwlD2ik9PT1BQUVFQilH2T8JRiMHCyVculgAAAJtJREFUWMPt1skOwjAMBFAXksYu+77T/P9XIijmCNL4wGXmPk9pnUgWYb5m05g1e7w/r6+M0f61vrMDgYkDFxBQB04gUD8hQIAAAQIECBAYYt636IrTg8DRgS26ph2Ca57IOqumKRfuULqslkZ4vy3PKZYW7Z/LcA8KeobkNzGDwMwBjb5G/dcnLPwndugYbsExiiyT6n3FB/UrD1lkOM21nDj+AAAAAElFTkSuQmCC",
                    "enforcesSecureChat": False,
                }

        data_string = json.dumps(data)
        packet_id_length = mc_var_int_length(0)
        data_prefix_length = mc_var_int_length(len(data_string))
        await mc_write_var_int(
            packet_id_length + data_prefix_length + len(data_string), writer
        )
        await mc_write_var_int(0, writer)
        await mc_write_var_int(len(data_string), writer)
        writer.write(data_string.encode())
    except Exception as e:
        print(f"Failed to handle minecraft: {e}")


async def minecraft_runner(
    host: str,
    port: int,
    mc_queue: asyncio.Queue[Optional[str]],
    mc_lock: asyncio.Lock,
):
    message = ["Booting up"]

    while True:
        await mc_lock.acquire()
        print("starting minecraft notifier")
        server = await asyncio.start_server(
            lambda x, y: handle_mc(message, x, y), host, port
        )
        addrs = ", ".join(str(sock.getsockname()) for sock in server.sockets)
        print(f"Serving minecraft notifier on {addrs}")
        async with server:
            await server.start_serving()

            restart_server = False
            while True:
                new_message = await mc_queue.get()
                if new_message is None:
                    if not restart_server:
                        print("stopping minecraft notifier")
                        server.close()
                        await server.wait_closed()
                        print("stopped minecraft notifier")
                        mc_lock.release()
                        restart_server = True
                else:
                    message[0] = new_message
                    print(f"updating message to {new_message}")
                    if restart_server:
                        break


async def binary_runner(
    repo_dir: str,
    log_dir: str,
    commit_wrapper: List[Union[str, int]],
    update_queue: asyncio.Queue[str],
    mc_queue: asyncio.Queue[Optional[str]],
    mc_lock: asyncio.Lock,
):
    try:
        await mc_queue.put("Updating Repo...")
        await update_git_repo(repo_dir)
    except SubprocessError as e:
        print(f"!WARNING! Failed to update repo: {e}")

    while True:
        try:
            await update_rust()
            break
        except SubprocessError as e:
            await mc_queue.put("Failed to update rust!")
            print(f"!WARNING! Failed to update rust: {e}\nSleeping for 10 minutes...")
            await asyncio.sleep(10 * 60)

    while True:
        try:
            await mc_queue.put("Building Binary...")
            await build_binary(repo_dir)
            break
        except SubprocessError as e:
            await mc_queue.put("Failed to build binary!")
            print(f"!WARNING! Failed to build binary: {e}\nSleeping for 10 minutes...")
            await asyncio.sleep(10 * 60)

    while True:
        try:
            commit, commit_name = await get_repo_description(repo_dir)
            break
        except SubprocessError as e:
            await mc_queue.put("Failed to get commit!")
            print(f"!WARNING! Failed to get commit: {e}\nSleeping for 10 minutes...")
            await asyncio.sleep(10 * 60)

    print(f"starting commit: {commit}")
    current_log_dir = os.path.join(log_dir, commit)
    if not os.path.isdir(current_log_dir):
        os.mkdir(current_log_dir)

    executable_path = os.path.join(repo_dir, "./target/debug/pumpkin")

    try_counter = 0
    for entry in os.scandir(current_log_dir):
        if entry.name.startswith("stdout_"):
            new_counter = int(entry.name.replace("stdout_", "").replace(".txt", ""))
            if new_counter >= try_counter:
                try_counter = new_counter + 1

    commit_wrapper[0] = commit
    commit_wrapper[2] = commit_name
    while True:
        commit_wrapper[1] = try_counter
        print(f"attempting to start the binary (try count: {try_counter})")
        stdout_path = os.path.join(current_log_dir, f"stdout_{try_counter}.txt")
        stderr_path = os.path.join(current_log_dir, f"stderr_{try_counter}.txt")

        stdout_log = open(stdout_path, "w")
        stderr_log = open(stderr_path, "w")

        await mc_queue.put(None)
        await mc_lock.acquire()

        proc = await start_binary(
            executable_path,
            stdout_log,
            stderr_log,
        )
        print("The binary has started")

        commit_wrapper[3] = ""
        new_commit = await wait_for_process_or_signal(proc, update_queue)

        stdout_log.close()
        stderr_log.close()

        mc_lock.release()
        if new_commit is None:
            print("The binary died for an unknown reason! Sleeping for 2 minutes.")
            commit_wrapper[3] = (
                "The PumpkinMC binary died for an unknown reason! Check the logs!"
            )
            await mc_queue.put("Binary failure! (Restarting in 5 minutes)")
            await asyncio.sleep(5 * 60)
            try_counter += 1
        else:
            try_counter += 1

            print("New commit detected; rebuilding and restarting.")

            try:
                await mc_queue.put("Updating Repo...")
                await update_git_repo(repo_dir)
            except SubprocessError as e:
                print(f"!WARNING! Failed to update repo: {e}")
                commit_wrapper[3] = f"Unable to update the local repo to {new_commit}"
                continue

            try:
                await update_rust()
            except SubprocessError as e:
                await mc_queue.put("Failed to update rust!")
                print(f"!WARNING! Failed to update rust: {e}")
                commit_wrapper[3] = f"Unable to update rust for new commit {new_commit}"
                continue

            try:
                await mc_queue.put("Building Binary...")
                await build_binary(repo_dir)
            except SubprocessError as e:
                await mc_queue.put("Failed to build binary!")
                print(f"!WARNING! Failed to build binary: {e}")
                commit_wrapper[3] = f"Unable to build commit {new_commit}"
                continue

            while True:
                try:
                    commit, commit_name = await get_repo_description(repo_dir)
                    break
                except SubprocessError as e:
                    await mc_queue.put("Failed to get commit!")
                    commit_wrapper[3] = "The PumpkinMC binary failed to start!"
                    print(
                        f"!WARNING! Failed to get commit: {e}\nSleeping for 10 minutes..."
                    )
                    await asyncio.sleep(10 * 60)
                    continue

            print(f"new commit: {commit}")
            current_log_dir = os.path.join(log_dir, commit)
            if not os.path.isdir(current_log_dir):
                os.mkdir(current_log_dir)

            try_counter = 0
            commit_wrapper[0] = commit
            commit_wrapper[1] = try_counter
            commit_wrapper[2] = commit_name
            continue


async def async_main(
    repo_path: str,
    log_path: str,
    webhook_host: str,
    webhook_port: int,
    mc_host: str,
    mc_port: int,
):
    update_queue = asyncio.Queue()
    mc_queue = asyncio.Queue()
    mc_lock = asyncio.Lock()

    commit_wrapper = ["0", 0, "default", ""]

    webhook_task = asyncio.create_task(
        webhook_runner(
            webhook_host,
            webhook_port,
            commit_wrapper,
            update_queue,
        )
    )
    binary_task = asyncio.create_task(
        binary_runner(
            repo_path,
            log_path,
            commit_wrapper,
            update_queue,
            mc_queue,
            mc_lock,
        )
    )
    mc_task = asyncio.create_task(
        minecraft_runner(
            mc_host,
            mc_port,
            mc_queue,
            mc_lock,
        )
    )

    completed, pending = await asyncio.wait(
        [webhook_task, binary_task, mc_task], return_when=asyncio.FIRST_EXCEPTION
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
            "0.0.0.0",
            25565,
        )
    )


if __name__ == "__main__":
    main()
