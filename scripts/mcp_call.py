#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP 直连客户端（麦当劳 MCP / Streamable HTTP）

用途：不依赖任何 IDE 的 MCP 宿主，直接对 https://mcp.mcd.cn 发 JSON-RPC 请求。
这样做的理由：Skill 里所有关于"能不能预约 / 算价 / 下单"的结论，
都必须能被复现，而不是靠读 README 推测。

用法：
  python mcp_call.py list                          # 列出全部工具
  python mcp_call.py schema create-order           # 看某个工具的参数
  python mcp_call.py call now-time-info            # 调用无参工具
  python mcp_call.py call query-meals '{"storeCode":"...","orderType":2,"beType":2,"beCode":"...","reservationDate":"2026-10-21 07:30"}'

环境变量：
  MCD_TOKEN  麦当劳 MCP Token（也可写在同目录 .token 文件中）
"""
import json
import os
import sys
import urllib.error
import urllib.request

ENDPOINT = "https://mcp.mcd.cn"
HERE = os.path.dirname(os.path.abspath(__file__))


def load_token() -> str:
    tok = os.environ.get("MCD_TOKEN", "").strip()
    if tok:
        return tok
    p = os.path.join(HERE, ".token")
    if os.path.exists(p):
        return open(p, encoding="utf-8").read().strip()
    raise SystemExit("缺少 MCP Token：请设置环境变量 MCD_TOKEN 或创建 .token 文件")


def rpc(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {load_token()}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode("utf-8", "replace")
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:800]}")

    # Streamable HTTP 可能以 SSE 形式返回，取最后一条 data 行
    if "event-stream" in ctype:
        payloads = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
        if not payloads:
            raise SystemExit(f"空 SSE 响应: {raw[:400]}")
        raw = payloads[-1]
    return json.loads(raw)


def unwrap(resp: dict) -> dict:
    """把 MCP 的 content[].text 解包成业务 JSON。

    实测发现：mcd-mcp 返回的 text 是「字段说明 Markdown + 原始 JSON」拼接体，
    形如 ``## Original Response\\n{"success":true,...}``。
    直接 json.loads 会失败，而让模型去读 Markdown 又无法程序化断言，
    所以这里优先截取 ``## Original Response`` 之后的 JSON，
    保证上层拿到的是结构化的 data，而不是一段需要再解释的自然语言。
    """
    if "error" in resp:
        return {"_rpc_error": resp["error"]}
    result = resp.get("result", {})
    if isinstance(result, dict) and "content" in result:
        texts = [c.get("text", "") for c in result["content"] if c.get("type") == "text"]
        joined = "\n".join(texts)
        for marker in ("## Original Response", "## 原始响应", "Original Response"):
            if marker in joined:
                joined = joined.split(marker, 1)[1]
                break
        # 实测：部分工具（如 query-store-coupons）把 JSON 直接拼在 Markdown 末尾，
        # 而说明文字里含 {data[].title} 这类模板花括号，会让「找第一个 {」的朴素做法解析失败。
        # 因此优先定位业务响应体的起始标记。
        for anchor in ('{"success"', '{"code"', '{\n  "success"'):
            pos = joined.find(anchor)
            if pos >= 0:
                try:
                    obj, _ = json.JSONDecoder().raw_decode(joined[pos:])
                    return obj
                except json.JSONDecodeError:
                    pass
        idx = joined.find("{")
        if idx >= 0:
            try:
                obj, _ = json.JSONDecoder().raw_decode(joined[idx:])
                return obj
            except json.JSONDecodeError:
                pass
        try:
            return json.loads(joined)
        except json.JSONDecodeError:
            return {"_raw_text": joined}
    return result


def call_tool(name: str, args: dict, save: bool = True) -> dict:
    resp = rpc("tools/call", {"name": name, "arguments": args})
    out = unwrap(resp)
    if save:
        d = os.path.join(os.path.dirname(HERE), "evidence")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    return out


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    cmd = sys.argv[1]

    if cmd == "list":
        tools = rpc("tools/list", {})["result"]["tools"]
        for t in tools:
            print(f"{t['name']:<26} {(t.get('description') or '')[:90]}")
        print(f"\n共 {len(tools)} 个工具")

    elif cmd == "schema":
        tools = {t["name"]: t for t in rpc("tools/list", {})["result"]["tools"]}
        t = tools[sys.argv[2]]
        print(json.dumps(t.get("inputSchema", {}), ensure_ascii=False, indent=2))

    elif cmd == "call":
        name = sys.argv[2]
        args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        print(json.dumps(call_tool(name, args), ensure_ascii=False, indent=2)[:6000])

    else:
        raise SystemExit(f"未知命令: {cmd}")


if __name__ == "__main__":
    main()
