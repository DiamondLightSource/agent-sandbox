"""``pi-local``: point Pi's ``lllm2`` provider at a local model server.

With no model given, ask the server (llama.cpp's ``/v1/models`` and
``/props``) for its one loaded model and its per-slot context. The other
providers, and the user's own lllm2 settings, are kept. Pi reads
``~/.pi/agent/models.json``; it is written 0600 and replaced atomically.
"""

import json
import os
import re
import sys
import urllib.request
from typing import cast

from ..tools import write_atomic

DEFAULT_PORT = "1920"
USAGE = "Usage: claude-sandbox pi-local [--port PORT] or pi-local MODEL CONTEXT [PORT]"


def fetch(url: str) -> object:
    """The JSON at ``url`` on the loopback, bypassing any proxy; None on failure."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=3) as response:
            return cast(object, json.loads(response.read()))
    except (OSError, ValueError):
        return None


def _object(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def discover(port: str) -> tuple[str, int] | None:
    """The one loaded model and its context, or None."""
    data = _object(fetch(f"http://127.0.0.1:{port}/v1/models")).get("data")
    listed = cast(list[object], data) if isinstance(data, list) else []
    model = _object(listed[0]).get("id") if len(listed) == 1 else None
    if not isinstance(model, str) or not model:
        return None
    props = _object(fetch(f"http://127.0.0.1:{port}/props"))
    n_ctx = _object(props.get("default_generation_settings")).get("n_ctx")
    if isinstance(n_ctx, bool) or not isinstance(n_ctx, int | float):
        return None
    if n_ctx != int(n_ctx):
        return None
    return model, int(n_ctx)


def _fail(message: str, code: int) -> int:
    print(message, file=sys.stderr)
    return code


def _alt(value: object, default: object) -> object:
    """jq's ``value // default``: only null and false give way."""
    return default if value is None or value is False else value


def configure(config: dict[str, object], model: str, context: int, port: str) -> None:
    """jq's update of ``.providers.lllm2``, keeping what the user set."""
    providers = _object(config.get("providers"))
    config["providers"] = providers
    old = _object(providers.get("lllm2"))
    models = old.get("models")
    listed = cast(list[object], models) if isinstance(models, list) else []
    same = [m for m in map(_object, listed) if m.get("id") == model]
    entry = dict(same[0] if same else {})
    entry.update(
        id=model,
        name=f"Local (lllm2): {model}",
        contextWindow=context,
        maxTokens=min(context // 4, 32000),
    )
    compat: dict[str, object] = {
        "supportsDeveloperRole": False,
        "supportsReasoningEffort": False,
    }
    compat.update(_object(old.get("compat")))
    lllm2 = dict(old)
    lllm2.update(
        baseUrl=f"http://127.0.0.1:{port}/v1",
        api="openai-completions",
        apiKey=_alt(old.get("apiKey"), "local"),
        compat=compat,
        models=[entry],
    )
    providers["lllm2"] = lllm2


def _usage_error(message: str) -> int:
    return _fail(message, 2)


def pi_local(args: list[str]) -> int:
    """Configure Pi from ``[]``, ``[--port PORT]`` or ``[MODEL CONTEXT [PORT]]``."""
    model = context = ""
    port = os.environ.get("CLAUDE_SANDBOX_LOCAL_MODEL_PORT") or DEFAULT_PORT
    discover_model = not args
    if len(args) == 2 and args[0] == "--port":
        port, discover_model = args[1], True
    elif len(args) in (2, 3):
        model, context, port = (*args, port)[:3]
    elif args:
        return _usage_error(USAGE)
    if not re.fullmatch(r"[1-9][0-9]{0,4}", port) or int(port) > 65535:
        return _usage_error("claude-sandbox: local model port must be 1–65535.")
    if discover_model:
        found = discover(port)
        if found is None:
            return _fail(
                "claude-sandbox: could not discover one loaded model and its context on"
                f" port {port}; existing Pi configuration kept. Start a model in lllm2,"
                " or use pi-local MODEL CONTEXT [PORT].",
                1,
            )
        model, context = found[0], str(found[1])
    if not model or not re.fullmatch(r"[1-9][0-9]{2,6}", context) or int(context) < 512:
        return _usage_error(
            "claude-sandbox: supply a model ID and context of 512–9999999 tokens."
        )
    home = os.environ.get("HOME") or os.path.expanduser("~")
    directory = f"{home}/.pi/agent"
    path = f"{directory}/models.json"
    config: dict[str, object] = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                loaded = cast(object, json.load(f))
        except ValueError:
            loaded = None
        config = _object(loaded)
        if not isinstance(loaded, dict) or not isinstance(
            _alt(config.get("providers"), {}), dict
        ):
            return _fail(
                f"claude-sandbox: {path} is not a JSON object; left as it is.", 1
            )
    configure(config, model, int(context), port)
    umask = os.umask(0o077)
    try:
        os.makedirs(directory, exist_ok=True)
    finally:
        os.umask(umask)
    text = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    write_atomic(path, text.encode(), 0o600)
    print(f"Configured Pi's lllm2 provider at http://127.0.0.1:{port}/v1.")
    print("Select lllm2 in Pi's /model picker, or launch pi --model lllm2.")
    print(
        "The relay setting in /etc/claude-sandbox.conf must match port"
        f" {port} (shipped default: 1920)."
    )
    return 0
