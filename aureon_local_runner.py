# aureon_local_runner.py
#
# Local Aureon runner: a minimal, extensible daemon that exposes a safe command API
# for Aureon/OpenHermes to interact with the local machine.
#
# Capabilities (extensible):
# - open_url      : open a web page in the default browser
# - open_app      : launch a whitelisted local application
# - run_command   : run a whitelisted shell command
# - read_file     : read text from a whitelisted file
# - write_file    : write/append text to a whitelisted file
# - list_dir      : list files in a whitelisted directory
# - macro_store   : store a sequence of high-level steps as a named macro
# - macro_execute : execute a stored macro
#
# Safety:
# - Only listens on localhost (127.0.0.1) by default
# - Requires X-AUREON-TOKEN header for all mutating requests
# - All filesystem access is restricted to ALLOWED_ROOTS
# - All apps/commands are explicit allow-lists (no arbitrary shell execution)
#
# Usage:
#   1. Install dependencies (once):
#        pip install fastapi uvicorn pydantic[dotenv]
#
#   2. Set environment variable AUREON_LOCAL_TOKEN to a strong secret, or edit DEFAULT_TOKEN.
#
#   3. Run:
#        python aureon_local_runner.py
#
#   4. From Aureon/OpenHermes or any HTTP client on the same machine:
#        POST http://127.0.0.1:8765/execute
#        Headers: X-AUREON-TOKEN: <your-secret>
#        Body:
#          {
#            "task_id": "optional-id",
#            "action": "open_url",
#            "params": {"url": "https://github.com"}
#          }
#
# This file is intentionally single-module and self-contained for direct GitHub use.

import os
import sys
import json
import subprocess
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional, Literal

from fastapi import FastAPI, HTTPException, Request, status, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, BaseSettings, Field, ValidationError

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------


class Settings(BaseSettings):
    # Shared secret used by Aureon/OpenHermes to authenticate.
    AUREON_LOCAL_TOKEN: str = Field(
        default="CHANGE_ME_TO_A_LONG_RANDOM_SECRET",
        env="AUREON_LOCAL_TOKEN",
    )

    # Host and port for the local runner.
    HOST: str = "127.0.0.1"
    PORT: int = 8765

    # Whitelisted root directories that the runner is allowed to touch.
    # You can add more paths as needed.
    ALLOWED_ROOTS: List[Path] = [
        Path.cwd(),  # current directory
        Path.home() / "AureonWorkspace",
    ]

    # Whitelisted applications Aureon is allowed to open.
    # Customize for your OS.
    APPS: Dict[str, str] = {
        # Logical name : executable path or command
        "vscode": "code",
        "terminal": "wt" if sys.platform == "win32" else "x-terminal-emulator",
        "browser": "",  # empty uses default webbrowser
        # Example OS-specific paths (comment/uncomment as needed):
        # "chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        # "finder": "open"  # macOS
    }

    # Whitelisted shell commands (non-interactive, safe utilities).
    COMMAND_WHITELIST: List[str] = [
        "git status",
        "git pull",
        "git diff",
        "pip list",
    ]

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()

# Normalize ALLOWED_ROOTS to absolute paths
ALLOWED_ROOTS = [p.expanduser().resolve() for p in settings.ALLOWED_ROOTS]

# In-memory macro store for step sequences
MACROS: Dict[str, List[Dict[str, Any]]] = {}


# ---------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------


def require_auth(request: Request) -> None:
    token = request.headers.get("X-AUREON-TOKEN")
    if not token or token != settings.AUREON_LOCAL_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


def is_path_allowed(path: Path) -> bool:
    """Check whether a file/directory path is under one of the allowed roots."""
    try:
        resolved = path.expanduser().resolve()
    except FileNotFoundError:
        # If it doesn't exist yet, check based on its parent.
        resolved = path.expanduser().resolve().parent

    for root in ALLOWED_ROOTS:
        try:
            if resolved == root or resolved.is_relative_to(root):
                return True
        except AttributeError:
            # Python <3.9 compatibility
            try:
                resolved.relative_to(root)
                return True
            except ValueError:
                continue
    return False


def ensure_allowed_path(path_str: str) -> Path:
    path = Path(path_str)
    if not is_path_allowed(path):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Path not allowed: {path}",
        )
    return path


def run_subprocess(command: str) -> Dict[str, Any]:
    """Run a whitelisted shell command and return output."""
    command = command.strip()
    if command not in settings.COMMAND_WHITELIST:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Command not allowed: {command}",
        )

    try:
        completed = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            check=False,
        )
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Command execution failed: {exc}",
        ) from exc


def open_application(app_name: str) -> Dict[str, Any]:
    app_name = app_name.lower()
    if app_name not in settings.APPS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown app: {app_name}",
        )

    target = settings.APPS[app_name]
    try:
        if app_name == "browser" or target == "":
            webbrowser.open("about:blank")
        else:
            if sys.platform == "win32":
                subprocess.Popen(target)  # noqa: S603,S607
            else:
                subprocess.Popen(target.split())  # noqa: S603,S607
        return {"status": "launched", "app": app_name, "target": target}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to open app {app_name}: {exc}",
        ) from exc


def open_url(url: str) -> Dict[str, Any]:
    try:
        webbrowser.open(url)
        return {"status": "opened", "url": url}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to open url {url}: {exc}",
        ) from exc


def read_file(path_str: str) -> Dict[str, Any]:
    path = ensure_allowed_path(path_str)
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File not found: {path}",
        )
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Not a file: {path}",
        )
    try:
        text = path.read_text(encoding="utf-8")
        return {"path": str(path), "content": text}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to read file: {exc}",
        ) from exc


def write_file(path_str: str, content: str, append: bool = False) -> Dict[str, Any]:
    path = ensure_allowed_path(path_str)
    try:
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with path.open(mode, encoding="utf-8") as f:
            f.write(content)
        return {"path": str(path), "bytes_written": len(content), "append": append}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to write file: {exc}",
        ) from exc


def list_directory(path_str: str) -> Dict[str, Any]:
    path = ensure_allowed_path(path_str)
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Directory not found: {path}",
        )
    if not path.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Not a directory: {path}",
        )

    try:
        entries = []
        for child in path.iterdir():
            entries.append(
                {
                    "name": child.name,
                    "is_dir": child.is_dir(),
                    "size": child.stat().st_size if child.is_file() else None,
                }
            )
        return {"path": str(path), "entries": entries}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list directory: {exc}",
        ) from exc


# ---------------------------------------------------------------------
# Macro system (high-level step sequences, not raw keylogging)
# ---------------------------------------------------------------------


class MacroStep(BaseModel):
    action: Literal[
        "open_url",
        "open_app",
        "run_command",
        "read_file",
        "write_file",
        "list_dir",
    ]
    params: Dict[str, Any]


def macro_store(name: str, steps: List[MacroStep]) -> Dict[str, Any]:
    MACROS[name] = [s.dict() for s in steps]
    return {"status": "stored", "name": name, "steps": len(steps)}


def macro_execute(name: str) -> Dict[str, Any]:
    if name not in MACROS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Macro not found: {name}",
        )

    results: List[Dict[str, Any]] = []
    for idx, step in enumerate(MACROS[name]):
        action = step["action"]
        params = step.get("params", {})
        results.append(
            {
                "step_index": idx,
                "action": action,
                "result": dispatch_action(action, params, internal_macro=True),
            }
        )
    return {"status": "completed", "name": name, "steps": results}


# ---------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------


class ExecuteRequest(BaseModel):
    task_id: Optional[str] = None
    action: Literal[
        "open_url",
        "open_app",
        "run_command",
        "read_file",
        "write_file",
        "list_dir",
        "macro_store",
        "macro_execute",
        "introspect",
        "ping",
    ]
    params: Dict[str, Any] = Field(default_factory=dict)


class ExecuteResponse(BaseModel):
    task_id: Optional[str]
    action: str
    success: bool
    result: Any
    error: Optional[str] = None


# ---------------------------------------------------------------------
# Action dispatcher
# ---------------------------------------------------------------------


def dispatch_action(action: str, params: Dict[str, Any], internal_macro: bool = False) -> Any:
    if action == "open_url":
        url = params.get("url")
        if not url:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing 'url' param",
            )
        return open_url(url)

    if action == "open_app":
        app_name = params.get("app")
        if not app_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing 'app' param",
            )
        return open_application(app_name)

    if action == "run_command":
        cmd = params.get("command")
        if not cmd:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing 'command' param",
            )
        return run_subprocess(cmd)

    if action == "read_file":
        path = params.get("path")
        if not path:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing 'path' param",
            )
        return read_file(path)

    if action == "write_file":
        path = params.get("path")
        content = params.get("content")
        append = bool(params.get("append", False))
        if path is None or content is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing 'path' or 'content' param",
            )
        return write_file(path, content, append=append)

    if action == "list_dir":
        path = params.get("path")
        if not path:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing 'path' param",
            )
        return list_directory(path)

    if action == "macro_store":
        name = params.get("name")
        steps_raw = params.get("steps")
        if not name or not isinstance(steps_raw, list):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Macro requires 'name' and list 'steps'",
            )
        try:
            steps = [MacroStep(**s) for s in steps_raw]
        except ValidationError as ve:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid macro steps: {ve}",
            ) from ve
        return macro_store(name, steps)

    if action == "macro_execute":
        name = params.get("name")
        if not name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Macro execute requires 'name'",
            )
        return macro_execute(name)

    if action == "introspect":
        return {
            "allowed_roots": [str(p) for p in ALLOWED_ROOTS],
            "apps": settings.APPS,
            "commands": settings.COMMAND_WHITELIST,
            "macros": list(MACROS.keys()),
            "platform": sys.platform,
            "python_version": sys.version,
        }

    if action == "ping":
        return {"status": "ok"}

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unknown action: {action}",
    )


# ---------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------


app = FastAPI(title="Aureon Local Runner", version="1.0.0")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # Only enforce auth for non-GET requests
    if request.method != "GET" and request.url.path != "/ping":
        try:
            require_auth(request)
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
            )
    return await call_next(request)


@app.get("/ping")
def ping() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/execute", response_model=ExecuteResponse)
def execute(req: ExecuteRequest, _: Any = Depends(require_auth)) -> ExecuteResponse:
    try:
        result = dispatch_action(req.action, req.params)
        return ExecuteResponse(
            task_id=req.task_id,
            action=req.action,
            success=True,
            result=result,
        )
    except HTTPException as exc:
        raise exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------


def main() -> None:
    try:
        import uvicorn  # type: ignore[import]
    except ImportError:
        print(
            "Missing dependency 'uvicorn'. Install with:\n"
            "    pip install uvicorn\n",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        f"Aureon Local Runner listening on http://{settings.HOST}:{settings.PORT}\n"
        "Allowed roots:\n  - " + "\n  - ".join(str(p) for p in ALLOWED_ROOTS) + "\n"
        "Use header 'X-AUREON-TOKEN' with your shared secret for POST /execute.\n"
    )
    uvicorn.run(
        "aureon_local_runner:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
```0
