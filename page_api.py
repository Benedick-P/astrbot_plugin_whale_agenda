"""AstrBot WebUI 插件页面的后端：读写 schedule.json。

页面在 `views/schedule/`，由 Dashboard 以受限 iframe 加载，前端通过
`window.AstrBotPluginView` bridge 调这里的两个接口（bridge 只支持 GET/POST）。
"""

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from .store import ScheduleError

PLUGIN_NAME = "astrbot_plugin_whale_agenda"


class PageApi:
    """把 Store 暴露给可视化视图。写操作一律走整份校验，避免并发改坏。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin

    def register(self, context) -> None:
        context.register_web_api(
            f"/{PLUGIN_NAME}/data", self.get_data, ["GET"], "读取整份日程数据"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/save", self.save_data, ["POST"], "保存整份日程数据"
        )
        logger.info(
            f"[whale-reminder] 页面 API 已注册：/{PLUGIN_NAME}/data、/{PLUGIN_NAME}/save；"
            "视图 #/plugin-view/astrbot_plugin_whale_agenda/schedule"
        )

    # ------------------------------------------------------------------ 处理

    async def get_data(self):
        store = self.plugin.store
        return json_response(
            {
                "ok": True,
                "data": store.data,
                "path": str(store.path),
                "user": request.username,
            }
        )

    async def save_data(self):
        payload = await request.json(default={})
        data = payload.get("data")
        if not isinstance(data, dict):
            return error_response("请求体缺少 data 字段", status_code=400)
        try:
            saved = self.plugin.store.save(data)
        except ScheduleError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[whale-reminder] 页面保存失败: {exc}")
            return error_response(f"保存失败：{exc}", status_code=500)
        logger.info(
            f"[whale-reminder] 页面保存成功：课程 {len(saved['courses'])}、"
            f"自习块 {len(saved['blocks'])}"
        )
        return json_response({"ok": True, "data": saved})
