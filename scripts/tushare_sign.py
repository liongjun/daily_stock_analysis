#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tushare Pro 每日签到（网页接口版）

签到接口: POST https://tushare.pro/wctapi/user_center/sign
认证方式: 网页登录态 Cookie（API Token 不能用于该接口）

环境变量:
  TUSHARE_COOKIE 从浏览器复制的一整串 Cookie
"""

import json
import os
import sys
import urllib.request
import urllib.error

BASE = "https://tushare.pro/wctapi"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
TIMEOUT = 30


def request(path, method="GET", payload=None, cookie=""):
    url = f"{BASE}{path}"
    data = None
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://tushare.pro/home/",
        "Origin": "https://tushare.pro",
        "Cookie": cookie,
    }
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"code": -1, "message": f"非 JSON 响应: {raw[:200]}", "data": None}


def get_score(cookie):
    r = request("/user_center/score", cookie=cookie)
    if r.get("code") == 0 and isinstance(r.get("data"), dict):
        return r["data"].get("score") or r["data"].get("scores")
    return None


def main():
    cookie = (os.environ.get("TUSHARE_COOKIE") or "").strip()
    if not cookie:
        print("[错误] 未配置 TUSHARE_COOKIE")
        return 2

    before = get_score(cookie)
    if before is None:
        r = request("/user_center/score", cookie=cookie)
        if r.get("code") == 40201:
            print("[失败] 登录态已失效(40201)，需要重新复制 Cookie")
            return 1
        print(f"[警告] 读取积分失败: {r.get('message')}")

    r = request("/user_center/sign", method="POST", payload={}, cookie=cookie)
    code = r.get("code")
    msg = r.get("message") or ""
    data = r.get("data") or {}
    if isinstance(data, dict):
        msg = data.get("message") or msg

    if code == 40201:
        print("[失败] 登录态已失效(40201)，需要重新复制 Cookie")
        return 1
    if code != 0:
        print(f"[失败] 签到请求出错: code={code} message={msg}")
        return 1

    success = data.get("success") if isinstance(data, dict) else None
    after = get_score(cookie)
    print(f"[结果] {msg or '无返回文案'}")
    print(f"[积分] {before} -> {after}")
    if success is False:
        # 例如“今天已经签到过了”，不算失败
        print("[说明] 接口返回 success=false，通常表示今日已签到")
    return 0


if __name__ == "__main__":
    sys.exit(main())
