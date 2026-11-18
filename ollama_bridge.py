#!/usr/bin/env python3
"""ollama_bridge.py — a 40-line translate proxy: POST /api/generate (ollama
shape) → llama-server /completion. Lets SagentCore's raw path drive llama-server
without touching core.py. Process-local tool for the deck build.

2026-10-11 (residenta ring 8.1 cutover): extended without changing the translate
shape —
  * everything that is not /api/generate or /health is proxied verbatim to
    UP (path, method, query, body, status). llama-server's POST /tokenize
    feeds the measured-density budget (token_budget.py) and /v1/chat/
    completions keeps mem0 fact extraction working off the same chat_base_url.
  * temperature is sent ONLY when the caller provides it — the 2026-10-07
    ruling (no client-side temperature override; hosting defaults own
    sampling). The old hard 0.7 default is gone.
  * repeat_penalty is forwarded when present (residenta's wired anti-attractor
    control, 2026-09-08). num_ctx stays dropped: llama-server's window is
    fixed at server start (her :8082 server runs -c 32768 = her declared
    total_context_tokens — the served truth).
"""
import json, httpx, sys
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import Response
import uvicorn

app = FastAPI()
UP = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8056"

@app.post("/api/generate")
async def generate(req: Request):
    payload = await req.json()
    opts = payload.get("options") or {}
    out = {"prompt": payload.get("prompt", ""),
           "n_predict": int(opts.get("num_predict", 2048)),
           "stop": opts.get("stop") or ["<|im_start|>", "<|im_end|>"]}
    if "temperature" in opts:
        out["temperature"] = float(opts["temperature"])
    if "repeat_penalty" in opts:
        out["repeat_penalty"] = float(opts["repeat_penalty"])
    try:
        async with httpx.AsyncClient(timeout=900) as c:
            r = await c.post(f"{UP}/completion", json=out)
            j = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"upstream {UP}: {e}")
    return {"response": j.get("content", ""),
            "done": True,
            "eval_count": j.get("tokens_predicted", 0),
            "prompt_eval_count": j.get("tokens_evaluated", 0),
            # ollama's vocabulary: a natural stop (eos/stop) is "stop";
            # "length" passes through so truncation stays visible.
            "done_reason": ({"eos": "stop"}.get(j.get("stop_type"),
                                                 j.get("stop_type"))
                            or "stop")}

@app.get("/health")
async def health():
    return {"ok": True, "upstream": UP}

# Registered LAST so it only catches what the routes above don't: /tokenize,
# /v1/chat/completions, /v1/models, ... — forwarded verbatim to UP.
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE",
                                        "PATCH", "OPTIONS", "HEAD"])
async def proxy(path: str, req: Request):
    body = await req.body()
    try:
        async with httpx.AsyncClient(timeout=900) as c:
            r = await c.request(req.method, f"{UP}/{path}",
                                params=req.query_params, content=body,
                                headers={"content-type":
                                         req.headers.get("content-type",
                                                         "application/json")})
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"upstream {UP}: {e}")
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type",
                                             "application/json"))

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]))
