#!/usr/bin/env python3
import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sqlite3
import sys
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import Request as UrlRequest, urlopen

from Crypto.Cipher import AES
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field


DATA_DIR = Path("/data")
USERS_DIR = DATA_DIR / "users"
DROP_DIR = Path("/drop")
ISO_PATH = DATA_DIR / "ubuntu-26.04.1-desktop-amd64.iso"
DB_PATH = DATA_DIR / "sessions.sqlite3"
SECRET_PATH = DATA_DIR / "session-secret"
MAX_SESSIONS = min(max(int(os.environ.get("MAX_SESSIONS", "2")), 1), 2)
SESSION_MAX_AGE = min(max(int(os.environ.get("SESSION_MAX_AGE_SECONDS", "3600")), 60), 3600)
DISCONNECT_GRACE = min(max(int(os.environ.get("DISCONNECT_GRACE_SECONDS", "60")), 5), 60)
VM_CPUS = min(max(int(os.environ.get("VM_CPUS", "2")), 1), 2)
VM_RAM_REQUEST = os.environ.get("VM_RAM", "4G")
RAM_MATCH = re.fullmatch(r"([1-9][0-9]*)([GM])", VM_RAM_REQUEST)
if RAM_MATCH is None:
    raise RuntimeError("VM_RAM must be a positive number followed by G or M.")
VM_RAM = VM_RAM_REQUEST if int(RAM_MATCH.group(1)) * (1024 if RAM_MATCH.group(2) == "G" else 1) <= 4096 else "4G"
VM_DISK_GB = min(max(int(os.environ.get("VM_DISK_GB", "32")), 8), 32)
MANAGER_BIND_HOST = os.environ.get("MANAGER_BIND_HOST", "0.0.0.0")
MANAGER_BIND_PORT = int(os.environ.get("MANAGER_BIND_PORT", "6080"))
VNC_BIND_HOST = os.environ.get("VNC_BIND_HOST", "127.0.0.1")
COOKIE_NAME = "lubuntu_session"
COOKIE_LIFETIME = 12 * 60 * 60
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
PASSWORD_MIN_LENGTH = 12


def utc_now():
    return datetime.now(timezone.utc)


@contextmanager
def db_connect():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    USERS_DIR.mkdir(parents=True, exist_ok=True)
    DROP_DIR.mkdir(parents=True, exist_ok=True)
    with db_connect() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'stopped',
                started_at TEXT,
                expires_at TEXT,
                updated_at TEXT NOT NULL
            )"""
        )
        connection.execute("UPDATE sessions SET status='stopped', started_at=NULL, expires_at=NULL")

    try:
        fd = os.open(SECRET_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "wb") as secret_file:
        secret_file.write(secrets.token_bytes(32))


def hash_password(password):
    salt = secrets.token_bytes(16)
    hashed = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**15,
        r=8,
        p=1,
        dklen=32,
        maxmem=64 * 1024 * 1024,
    )
    return "scrypt$" + base64.urlsafe_b64encode(salt + hashed).decode("ascii")


def verify_password(password, stored_hash):
    try:
        algorithm, encoded = stored_hash.split("$", 1)
        raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
        salt, expected = raw[:16], raw[16:]
        if algorithm != "scrypt" or len(salt) != 16 or len(expected) != 32:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=2**15,
            r=8,
            p=1,
            dklen=32,
            maxmem=64 * 1024 * 1024,
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def secret_key():
    return SECRET_PATH.read_bytes()


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def issue_cookie(user):
    expires = int(time.time()) + COOKIE_LIFETIME
    payload = f"{user['id']}:{expires}".encode("utf-8")
    signature = hmac.new(secret_key(), payload, hashlib.sha256).digest()
    return b64(payload) + "." + b64(signature)


def decode_cookie(token):
    try:
        payload_part, signature_part = token.split(".", 1)
        payload = base64.urlsafe_b64decode(payload_part + "=" * (-len(payload_part) % 4))
        signature = base64.urlsafe_b64decode(signature_part + "=" * (-len(signature_part) % 4))
        expected = hmac.new(secret_key(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        user_id, expires = payload.decode("utf-8").split(":", 1)
        if int(expires) < int(time.time()):
            return None
        return user_id
    except (ValueError, UnicodeDecodeError, OSError):
        return None


def user_by_id(user_id):
    with db_connect() as connection:
        return connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def user_by_name(username):
    with db_connect() as connection:
        return connection.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def session_by_user(user_id):
    with db_connect() as connection:
        return connection.execute("SELECT * FROM sessions WHERE user_id=?", (user_id,)).fetchone()


def session_response(row):
    if row is None:
        return {"status": "stopped", "expiresAt": None}
    return {"status": row["status"], "expiresAt": row["expires_at"]}


def create_user(username, password, is_admin=False):
    username = username.strip().lower()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("Username must be 1–32 lowercase letters, digits, dots, dashes, or underscores.")
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"Password must be at least {PASSWORD_MIN_LENGTH} characters.")
    user_id = str(uuid.uuid4())
    now = utc_now().isoformat()
    try:
        with db_connect() as connection:
            connection.execute(
                "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, username, hash_password(password), int(is_admin), now),
            )
            connection.execute(
                "INSERT INTO sessions (user_id, status, updated_at) VALUES (?, 'stopped', ?)",
                (user_id, now),
            )
    except sqlite3.IntegrityError as error:
        raise ValueError("That username already exists.") from error
    (DROP_DIR / username).mkdir(parents=True, exist_ok=True)
    return {"id": user_id, "username": username, "is_admin": bool(is_admin)}


def require_csrf(request):
    if request.headers.get("x-requested-with") != "SessionManager":
        raise HTTPException(status_code=403, detail="Missing request validation header.")
    origin = request.headers.get("origin")
    host = request.headers.get("host")
    if not origin or not host or urlsplit(origin).netloc.lower() != host.lower():
        raise HTTPException(status_code=403, detail="Request origin does not match this console.")


def authenticated_user(request):
    user_id = decode_cookie(request.cookies.get(COOKIE_NAME, ""))
    user = user_by_id(user_id) if user_id else None
    if user is None:
        raise HTTPException(status_code=401, detail="Please sign in.")
    return user


def require_admin(user):
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Administrator access required.")


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=1024)


class NewUser(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=12, max_length=1024)


class Heartbeat(BaseModel):
    clientId: str


class VMRuntime:
    def __init__(self, user_id, process, qmp_path, vnc_port, vnc_password, control_dir):
        self.user_id = user_id
        self.process = process
        self.qmp_path = qmp_path
        self.vnc_port = vnc_port
        self.vnc_password = vnc_password
        self.control_dir = control_dir
        self.status = "running"
        self.deadline = time.monotonic() + SESSION_MAX_AGE
        self.client_grace_until = time.monotonic() + DISCONNECT_GRACE
        self.qmp_lock = asyncio.Lock()


runtime_by_user = {}
browser_clients = {}
login_failures = {}


async def read_qmp_reply(reader):
    while True:
        line = await reader.readline()
        if not line:
            raise RuntimeError("QEMU closed its QMP connection.")
        reply = json.loads(line)
        if "return" in reply:
            return reply["return"]
        if "error" in reply:
            raise RuntimeError(reply["error"].get("desc", "QMP command failed."))


async def qmp_command(runtime, command, arguments=None):
    async with runtime.qmp_lock:
        reader, writer = await asyncio.open_unix_connection(runtime.qmp_path)
        try:
            await reader.readline()
            writer.write(b'{"execute":"qmp_capabilities"}\r\n')
            await writer.drain()
            await read_qmp_reply(reader)
            request = {"execute": command}
            if arguments is not None:
                request["arguments"] = arguments
            writer.write(json.dumps(request).encode("utf-8") + b"\r\n")
            await writer.drain()
            return await read_qmp_reply(reader)
        finally:
            writer.close()
            await writer.wait_closed()


def update_session_status(user_id, status, expires_at=None):
    with db_connect() as connection:
        connection.execute(
            "UPDATE sessions SET status=?, started_at=?, expires_at=?, updated_at=? WHERE user_id=?",
            (
                status,
                utc_now().isoformat() if status == "running" else None,
                expires_at,
                utc_now().isoformat(),
                user_id,
            ),
        )


def free_vnc_port():
    for port in range(5900, 6000):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            try:
                candidate.bind((VNC_BIND_HOST, port))
            except OSError:
                continue
            return port
    raise RuntimeError("No free VNC port is available.")


async def qemu_process_is_alive(runtime):
    return runtime.process.returncode is None


async def start_or_resume_vm(user):
    user_id = user["id"]
    runtime = runtime_by_user.get(user_id)
    if runtime and not await qemu_process_is_alive(runtime):
        runtime_by_user.pop(user_id, None)
        browser_clients.pop(user_id, None)
        update_session_status(user_id, "stopped")
        runtime = None
    if runtime and await qemu_process_is_alive(runtime):
        if runtime.status == "paused":
            await qmp_command(runtime, "cont")
            runtime.status = "running"
            runtime.deadline = time.monotonic() + SESSION_MAX_AGE
            runtime.client_grace_until = time.monotonic() + DISCONNECT_GRACE
            expires = (utc_now() + timedelta(seconds=SESSION_MAX_AGE)).isoformat()
            update_session_status(user_id, "running", expires)
        return session_response(session_by_user(user_id))
    if len(runtime_by_user) >= MAX_SESSIONS:
        raise HTTPException(
            status_code=503,
            detail=f"All {MAX_SESSIONS} VM slots are in use. Pause or stop another session first.",
        )
    if not ISO_PATH.is_file():
        raise HTTPException(status_code=503, detail="Ubuntu ISO is missing; restart the manager setup.")

    home = USERS_DIR / user_id
    control_dir = home / "control"
    home.mkdir(parents=True, exist_ok=True)
    control_dir.mkdir(parents=True, exist_ok=True)
    drop_dir = DROP_DIR / user["username"]
    drop_dir.mkdir(parents=True, exist_ok=True)
    disk_path = home / "ubuntu-26.04.qcow2"
    if not disk_path.exists():
        create_disk = await asyncio.create_subprocess_exec(
            "qemu-img", "create", "-f", "qcow2", str(disk_path), f"{VM_DISK_GB}G",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, error = await create_disk.communicate()
        if create_disk.returncode:
            raise HTTPException(status_code=500, detail=f"Could not create the VM disk: {error.decode(errors='replace')}")

    installed_marker = control_dir / "ubuntu-installed"
    boot_args = ["-boot", "order=c,menu=on"]
    if not installed_marker.exists():
        boot_args = ["-boot", "order=c,once=d,menu=on", "-cdrom", str(ISO_PATH)]

    qmp_path = str(home / "qmp.sock")
    try:
        Path(qmp_path).unlink()
    except FileNotFoundError:
        pass
    vnc_port = free_vnc_port()
    vnc_display = vnc_port - 5900
    password_path = control_dir / "vnc-password"
    if password_path.exists():
        vnc_password = password_path.read_text().strip()
    else:
        vnc_password = secrets.token_hex(4)
        password_path.write_text(vnc_password)
        password_path.chmod(0o600)
    log_path = home / "qemu.log"
    qemu_args = [
        "qemu-system-x86_64",
        "-machine", "q35,accel=kvm",
        "-cpu", "host",
        "-smp", str(VM_CPUS),
        "-m", VM_RAM,
        "-rtc", "clock=host,base=utc",
        *boot_args,
        "-drive", f"file={disk_path},if=virtio,format=qcow2,cache=writeback,discard=unmap",
        "-device", "virtio-vga",
        "-device", "qemu-xhci,id=usb",
        "-device", "usb-kbd",
        "-device", "usb-tablet",
        "-netdev", "user,id=net0,net=10.0.2.0/24,dhcpstart=10.0.2.15,dns=10.0.2.3",
        "-device", "e1000,netdev=net0",
        "-virtfs", f"local,path={drop_dir},mount_tag=hostdrop,security_model=none,readonly=on",
        "-virtfs", f"local,path={control_dir},mount_tag=hostcontrol,security_model=none",
        "-virtfs", "local,path=/setup,mount_tag=hostsetup,security_model=none,readonly=on",
        "-display", "none",
        "-vnc", f"{VNC_BIND_HOST}:{vnc_display},password=on",
        "-qmp", f"unix:{qmp_path},server=on,wait=off",
    ]
    with open(log_path, "ab") as log_file:
        process = await asyncio.create_subprocess_exec(
            *qemu_args,
            stdout=log_file,
            stderr=log_file,
        )
    runtime = VMRuntime(user_id, process, qmp_path, vnc_port, vnc_password, control_dir)
    runtime_by_user[user_id] = runtime
    for _ in range(100):
        if process.returncode is not None:
            runtime_by_user.pop(user_id, None)
            tail = log_path.read_text(errors="replace")[-2000:]
            raise HTTPException(status_code=500, detail=f"QEMU exited during startup. {tail}")
        if Path(qmp_path).exists():
            break
        await asyncio.sleep(0.1)
    else:
        process.terminate()
        runtime_by_user.pop(user_id, None)
        raise HTTPException(status_code=500, detail="QEMU did not open its control socket.")
    try:
        await qmp_command(runtime, "query-status")
        await qmp_command(
            runtime,
            "set_password",
            {"protocol": "vnc", "password": runtime.vnc_password},
        )
    except Exception as error:
        process.terminate()
        runtime_by_user.pop(user_id, None)
        tail = log_path.read_text(errors="replace")[-2000:]
        raise HTTPException(status_code=500, detail=f"Could not initialize QEMU control: {error}. {tail}") from error

    expires = (utc_now() + timedelta(seconds=SESSION_MAX_AGE)).isoformat()
    update_session_status(user_id, "running", expires)
    browser_clients.pop(user_id, None)
    return session_response(session_by_user(user_id))


async def pause_vm(user_id):
    runtime = runtime_by_user.get(user_id)
    if not runtime or not await qemu_process_is_alive(runtime):
        runtime_by_user.pop(user_id, None)
        update_session_status(user_id, "stopped")
        return
    if runtime.status == "running":
        await qmp_command(runtime, "stop")
    runtime.status = "paused"
    runtime.deadline = 0
    browser_clients.pop(user_id, None)
    update_session_status(user_id, "paused")


async def reap_sessions():
    while True:
        await asyncio.sleep(2)
        now = time.monotonic()
        for user_id, runtime in list(runtime_by_user.items()):
            if runtime.process.returncode is not None:
                runtime_by_user.pop(user_id, None)
                browser_clients.pop(user_id, None)
                update_session_status(user_id, "stopped")
                continue
            logout_marker = runtime.control_dir / "logout"
            if logout_marker.exists():
                logout_marker.unlink()
                try:
                    await pause_vm(user_id)
                except Exception as error:
                    print(f"Could not pause {user_id} after Linux logout: {error}", file=sys.stderr)
                continue
            if runtime.status != "running":
                continue
            if now >= runtime.deadline:
                try:
                    await pause_vm(user_id)
                except Exception as error:
                    print(f"Could not pause expired session {user_id}: {error}", file=sys.stderr)
                continue
            if now < runtime.client_grace_until:
                continue
            clients = browser_clients.get(user_id, {})
            active_clients = {
                client_id: last_seen
                for client_id, last_seen in clients.items()
                if now - last_seen <= DISCONNECT_GRACE
            }
            browser_clients[user_id] = active_clients
            if not active_clients:
                try:
                    await pause_vm(user_id)
                except Exception as error:
                    print(f"Could not pause disconnected session {user_id}: {error}", file=sys.stderr)


@asynccontextmanager
async def lifespan(_app):
    init_db()
    if not ISO_PATH.is_file():
        print(f"Warning: Ubuntu ISO not found at {ISO_PATH}", file=sys.stderr)
    reaper_task = asyncio.create_task(reap_sessions())
    try:
        yield
    finally:
        reaper_task.cancel()
        await asyncio.gather(reaper_task, return_exceptions=True)
        for runtime in list(runtime_by_user.values()):
            if runtime.process.returncode is None:
                runtime.process.terminate()
        await asyncio.gather(
            *(runtime.process.wait() for runtime in runtime_by_user.values()),
            return_exceptions=True,
        )


app = FastAPI(lifespan=lifespan)
failed_login_attempts = {}


@app.get("/", response_class=HTMLResponse)
async def home():
    return FileResponse("/app/manager.html")


def guacamole_data(user, runtime):
    try:
        key = bytes.fromhex(os.environ["GUAC_JSON_SECRET"])
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=503, detail="Guacamole authentication is not configured.") from error
    if len(key) != 16:
        raise HTTPException(status_code=503, detail="Guacamole authentication key must be 128 bits.")
    now_ms = int(time.time() * 1000)
    auth_data = json.dumps(
        {
            "username": user["username"],
            "expires": str(now_ms + 60_000),
            "connections": {
                "Ubuntu desktop": {
                    "protocol": "vnc",
                    "parameters": {
                        "hostname": "host.docker.internal",
                        "port": str(runtime.vnc_port),
                        "password": runtime.vnc_password,
                        "color-depth": "24",
                        "cursor": "remote",
                    },
                }
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(key, auth_data, hashlib.sha256).digest()
    signed = signature + auth_data
    padding_size = AES.block_size - len(signed) % AES.block_size
    padded = signed + bytes([padding_size]) * padding_size
    encrypted = AES.new(key, AES.MODE_CBC, iv=bytes(AES.block_size)).encrypt(padded)
    return quote(base64.b64encode(encrypted).decode("ascii"), safe="")


def guacamole_client_url(user, runtime):
    form = urlencode({"data": unquote(guacamole_data(user, runtime))}).encode("ascii")
    request = UrlRequest(
        "http://172.30.0.3:8080/guacamole/api/tokens",
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            auth = json.load(response)
    except HTTPError as error:
        raise HTTPException(status_code=502, detail="Guacamole rejected desktop authentication.") from error
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=502, detail="Guacamole could not be reached to open the desktop.") from error

    token = auth.get("authToken")
    if not isinstance(token, str) or not token:
        raise HTTPException(status_code=502, detail="Guacamole did not return a desktop access token.")
    connection_id = base64.urlsafe_b64encode(b"Ubuntu desktop\0c\0json").decode("ascii").rstrip("=")
    return f"/guacamole/?token={quote(token, safe='')}#/client/{connection_id}"


@app.get("/console", response_class=HTMLResponse)
async def console(request: Request):
    user = authenticated_user(request)
    runtime = runtime_by_user.get(user["id"])
    if runtime is None or runtime.status != "running" or runtime.process.returncode is not None:
        raise HTTPException(status_code=409, detail="Start or resume your desktop before opening the console.")
    guac_url = guacamole_client_url(user, runtime)
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ubuntu desktop</title>
<style>html,body,iframe{{width:100%;height:100%;margin:0;border:0;overflow:hidden;background:#111}}#bar{{position:fixed;z-index:2;top:.5rem;right:.5rem;display:flex;gap:.5rem}}a,button{{padding:.55rem .8rem;border:0;border-radius:.35rem;background:#1f2937;color:white;font:14px system-ui,sans-serif;text-decoration:none;cursor:pointer}}</style></head>
<body><nav id="bar"><button id="fullscreen">Fullscreen</button><a href="/">Session dashboard</a></nav>
<iframe id="desktop" src="{guac_url}" allow="fullscreen; clipboard-read; clipboard-write" allowfullscreen title="Ubuntu remote desktop"></iframe>
<script>
const clientId=crypto.randomUUID();
const heartbeat=()=>fetch('/api/heartbeat',{{method:'POST',credentials:'same-origin',headers:{{'Content-Type':'application/json','X-Requested-With':'SessionManager'}},body:JSON.stringify({{clientId}})}}).catch(()=>{{}});
heartbeat();setInterval(heartbeat,10000);
document.getElementById('fullscreen').addEventListener('click',async()=>{{const frame=document.getElementById('desktop');if(document.fullscreenElement)await document.exitFullscreen();else await frame.requestFullscreen();}});
</script></body></html>"""
    )


@app.post("/api/login")
async def login(request: Request, credentials: Credentials):
    require_csrf(request)
    username = credentials.username.strip().lower()
    key = (request.client.host if request.client else "unknown", username)
    attempts = login_failures.get(key, [])
    now = time.monotonic()
    attempts = [attempt for attempt in attempts if now - attempt < 600]
    if len(attempts) >= 5:
        raise HTTPException(status_code=429, detail="Too many sign-in attempts. Try again in 10 minutes.")
    user = user_by_name(username)
    if user is None or not verify_password(credentials.password, user["password_hash"]):
        attempts.append(now)
        login_failures[key] = attempts
        raise HTTPException(status_code=401, detail="Incorrect username or password.")
    login_failures.pop(key, None)
    response = JSONResponse({"username": user["username"], "isAdmin": bool(user["is_admin"])})
    response.set_cookie(
        COOKIE_NAME,
        issue_cookie(user),
        max_age=COOKIE_LIFETIME,
        httponly=True,
        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https",
        samesite="strict",
        path="/",
    )
    return response


@app.get("/api/me")
async def me(request: Request):
    user = authenticated_user(request)
    row = session_by_user(user["id"])
    runtime = runtime_by_user.get(user["id"])
    return {
        "username": user["username"],
        "isAdmin": bool(user["is_admin"]),
        "session": session_response(row),
        "running": bool(runtime and runtime.process.returncode is None),
        "maxAgeSeconds": SESSION_MAX_AGE,
        "disconnectGraceSeconds": DISCONNECT_GRACE,
    }


@app.post("/api/logout")
async def logout(request: Request):
    require_csrf(request)
    user = authenticated_user(request)
    await pause_vm(user["id"])
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict")
    return response


@app.post("/api/session/start")
async def start_session(request: Request):
    require_csrf(request)
    user = authenticated_user(request)
    row = session_by_user(user["id"])
    if row and row["status"] in ("running", "paused"):
        return await start_or_resume_vm(user)
    return await start_or_resume_vm(user)


@app.post("/api/session/pause")
async def pause_session(request: Request):
    require_csrf(request)
    user = authenticated_user(request)
    await pause_vm(user["id"])
    return {"session": session_response(session_by_user(user["id"]))}


@app.post("/api/session/resume")
async def resume_session(request: Request):
    require_csrf(request)
    user = authenticated_user(request)
    return {"session": await start_or_resume_vm(user)}


@app.post("/api/heartbeat")
async def heartbeat(request: Request, body: Heartbeat):
    user = authenticated_user(request)
    if not re.fullmatch(r"[a-zA-Z0-9-]{8,64}", body.clientId):
        raise HTTPException(status_code=400, detail="Invalid console client ID.")
    runtime = runtime_by_user.get(user["id"])
    if runtime and runtime.status == "running":
        browser_clients.setdefault(user["id"], {})[body.clientId] = time.monotonic()
    return {"ok": True}


@app.get("/api/admin/users")
async def admin_users(request: Request):
    admin = authenticated_user(request)
    require_admin(admin)
    with db_connect() as connection:
        rows = connection.execute(
            """SELECT users.username, users.created_at, sessions.status
               FROM users JOIN sessions ON users.id=sessions.user_id
               ORDER BY users.username"""
        ).fetchall()
    return {"users": [dict(row) for row in rows]}


@app.post("/api/admin/users")
async def add_user(request: Request, new_user: NewUser):
    require_csrf(request)
    admin = authenticated_user(request)
    require_admin(admin)
    try:
        created = create_user(new_user.username, new_user.password)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"username": created["username"]}


def create_first_admin(username):
    init_db()
    with db_connect() as connection:
        if connection.execute("SELECT 1 FROM users WHERE is_admin=1 LIMIT 1").fetchone():
            raise SystemExit("An administrator already exists.")
    import getpass

    password = getpass.getpass("Admin password (12+ characters): ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match.")
    try:
        admin = create_user(username, password, is_admin=True)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(f"Created administrator account {admin['username']}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ubuntu VM session manager")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="Run the session manager")
    admin_parser = subparsers.add_parser("create-admin", help="Create the first administrator")
    admin_parser.add_argument("--username", required=True)
    args = parser.parse_args()
    if args.command == "create-admin":
        create_first_admin(args.username)
    else:
        import uvicorn

        uvicorn.run(
            app,
            host=MANAGER_BIND_HOST,
            port=MANAGER_BIND_PORT,
            proxy_headers=True,
            forwarded_allow_ips="*",
        )
