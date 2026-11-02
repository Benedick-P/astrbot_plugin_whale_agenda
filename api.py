"""本机 HTTP API：用 curl / 脚本改日程，不用登服务器改 JSON。

只监听 127.0.0.1，且所有请求都要带 token（`X-Api-Token` 头或 `?token=`），
避免浏览器里随便一个页面就能改写你的课表。

接口一览：
    GET    /api/health                     健康检查
    GET    /api/schedule                   整份日程
    PUT    /api/schedule                   整份替换
    GET    /api/schedule/courses           课程列表
    POST   /api/schedule/courses           新增课程
    PATCH  /api/schedule/courses/{id}      改一节课的字段
    DELETE /api/schedule/courses/{id}      删一节课
    GET    /api/schedule/blocks            自习块列表（同上四种方法）
    GET    /api/schedule/settings          学期起点、熄灯时间等
    PUT    /api/schedule/settings          改设置
    GET    /api/schedule/meals             三餐
    PUT    /api/schedule/meals             整表替换三餐
    GET    /api/schedule/sleep_plan        作息阶段表
    PUT    /api/schedule/sleep_plan        整表替换作息阶段
    POST   /api/reload                     从磁盘重读（外部改文件后用）

课程对象：
    {"day": 0, "start": "08:00", "span": "08:00-09:40",
     "name": "高等数学", "room": "教三 201", "weeks": [[1, 16]]}
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from astrbot.api import logger

from .store import ScheduleError


class _Handler(BaseHTTPRequestHandler):
    server_version = "WhaleSchedule/1.0"
    store = None
    token = ""

    # ------------------------------------------------------------------ 基础

    def log_message(self, fmt, *args):        # 别把每条请求刷进系统日志
        return

    def _auth_ok(self) -> bool:
        header = self.headers.get("X-Api-Token") or ""
        query = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        return bool(self.token) and (header == self.token or query == self.token)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ScheduleError(f"请求体不是合法 JSON：{exc}") from exc

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _data(self) -> dict:
        return json.loads(json.dumps(self.store.data))   # 深拷贝，改坏了也不污染内存

    # ------------------------------------------------------------------ 路由

    def _dispatch(self, method: str) -> None:
        if not self._auth_ok():
            self._send(401, {"error": "缺少或错误的 token（X-Api-Token 头或 ?token=）"})
            return

        path = urlparse(self.path).path.rstrip("/") or "/"
        parts = [p for p in path.split("/") if p]

        try:
            if parts[:1] != ["api"]:
                self._send(404, {"error": "未知路径"})
                return
            tail = parts[1:]

            if tail == ["health"]:
                self._send(200, {"ok": True, "courses": len(self.store.courses),
                                 "blocks": len(self.store.blocks)})
                return

            if tail == ["reload"] and method == "POST":
                self.store.load()
                self._send(200, {"ok": True, "reloaded": True})
                return

            if tail == ["schedule"]:
                if method == "GET":
                    self._send(200, self._data())
                elif method == "PUT":
                    self._send(200, self.store.save(self._body()))
                else:
                    self._send(405, {"error": f"{method} 不支持"})
                return

            table = tail[1] if len(tail) > 1 and tail[0] == "schedule" else None
            if table in ("courses", "blocks", "meals", "sleep_plan", "settings"):
                self._table(method, table, tail[2:])
                return

            self._send(404, {"error": "未知路径"})
        except ScheduleError as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[whale-reminder] API 失败 {method} {self.path}: {exc}")
            self._send(500, {"error": f"内部错误：{exc}"})

    def _table(self, method: str, table: str, rest: list) -> None:
        data = self._data()

        if table == "settings":
            if method == "GET":
                self._send(200, data.get("settings") or {})
            elif method == "PUT":
                data.setdefault("settings", {}).update(self._body())
                self.store.save(data)
                self._send(200, self.store.settings)
            else:
                self._send(405, {"error": "settings 只支持 GET/PUT"})
            return

        items = data.setdefault(table, [])

        if not rest:                                   # 集合级
            if method == "GET":
                self._send(200, {"count": len(items), "items": items})
            elif method == "POST":
                if table in ("meals", "sleep_plan"):
                    self._send(405, {"error": f"{table} 请用 PUT 整表替换"})
                    return
                body = self._body()
                body.pop("id", None)
                items.append(body)
                saved = self.store.save(data)
                self._send(201, {"ok": True, "items": saved[table]})
            elif method == "PUT":
                body = self._body()
                new_items = body.get("items", body if isinstance(body, list) else [])
                if not isinstance(new_items, list):
                    raise ScheduleError("PUT 需要 {\"items\": [...]} 或直接传数组")
                data[table] = new_items
                saved = self.store.save(data)
                self._send(200, {"ok": True, "items": saved[table]})
            else:
                self._send(405, {"error": f"{method} 不支持"})
            return

        item_id = rest[0]
        index = next((i for i, item in enumerate(items) if item.get("id") == item_id), None)
        if index is None:
            self._send(404, {"error": f"找不到 id={item_id}"})
            return

        if method == "GET":
            self._send(200, items[index])
        elif method == "PATCH":
            body = self._body()
            body.pop("id", None)
            items[index].update(body)
            saved = self.store.save(data)
            self._send(200, {"ok": True, "item": next(
                i for i in saved[table] if i.get("id") == item_id)})
        elif method == "DELETE":
            removed = items.pop(index)
            self.store.save(data)
            self._send(200, {"ok": True, "removed": removed})
        else:
            self._send(405, {"error": f"{method} 不支持"})

    # ------------------------------------------------------------------ 方法绑定

    def do_GET(self):        # noqa: N802
        self._dispatch("GET")

    def do_POST(self):       # noqa: N802
        self._dispatch("POST")

    def do_PUT(self):        # noqa: N802
        self._dispatch("PUT")

    def do_PATCH(self):      # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self):     # noqa: N802
        self._dispatch("DELETE")


class ApiServer:
    """把 ThreadingHTTPServer 包一层，方便插件 start/stop。"""

    def __init__(self, store, host: str, port: int, token: str):
        self.store = store
        self.token = token
        handler = type("Handler", (_Handler,), {"store": store, "token": token})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       name="whale-schedule-api", daemon=True)

    @property
    def address(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[whale-reminder] 停止 API 失败: {exc}")
