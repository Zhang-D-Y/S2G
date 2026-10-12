"""OpenAI-compatible chat call with full transport accounting (HTTP status and error
text are kept, so provider failures stay diagnosable)."""
import json
import os
import time
import urllib.error
import urllib.request


def provider_identity(cfg):
    model = os.environ.get(cfg["model_env"])
    base = os.environ.get(cfg["base_url_env"])
    key = os.environ.get(cfg["api_key_env"])
    return {"model": model, "base_url": base, "key_present": bool(key)}


def chat(cfg, system, user):
    ident = provider_identity(cfg)
    rec = {"model_version": ident["model"], "base_url": ident["base_url"], "http_status": None,
           "error": None, "content": None, "token_usage": None, "elapsed_s": None}
    if not (ident["model"] and ident["base_url"] and ident["key_present"]):
        rec["error"] = (f"provider env incomplete: set {cfg['model_env']}, {cfg['base_url_env']}, "
                        f"{cfg['api_key_env']} (e.g. `source ~/.llm_env`)")
        return rec
    body = json.dumps({"model": ident["model"], "temperature": cfg["temperature"],
                       "max_tokens": cfg["max_output_tokens"],
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": user}]}).encode()
    req = urllib.request.Request(ident["base_url"].rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {os.environ[cfg['api_key_env']]}"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=cfg["request_timeout_s"]) as r:
            rec["http_status"] = r.status
            data = json.loads(r.read().decode())
        rec["content"] = data["choices"][0]["message"].get("content")
        rec["finish_reason"] = data["choices"][0].get("finish_reason")
        rec["token_usage"] = data.get("usage")
    except urllib.error.HTTPError as e:
        rec["http_status"] = e.code
        rec["error"] = f"HTTPError {e.code}: {e.read().decode(errors='replace')[:500]}"
    except Exception as e:  # noqa: BLE001 - transport failures are recorded, not raised
        rec["error"] = f"{type(e).__name__}: {e}"
    rec["elapsed_s"] = round(time.monotonic() - t0, 3)
    return rec


def extract_json(text):
    """First top-level JSON object in the reply (fenced or bare)."""
    if not text:
        return None
    start = text.find("{")
    while start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None
