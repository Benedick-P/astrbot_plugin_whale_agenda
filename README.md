# 小鲸鱼日程提醒（astrbot_plugin_whale_agenda）

一体化插件：定时提醒 + 主人身份注入 + 日程查询 LLM 工具，共用一份可编辑的日程数据。

原来分散在三个插件里（`astrbot_plugin_whale_reminder`、`astrbot_plugin_whale_schedule_tools`、
`owner_guard`），现在合并成一个，主人 QQ 直接在 WebUI 插件页面绑定。
课表、自习块、三餐、作息全部存在一个 JSON 文件里，并提供只监听本机的 HTTP API 供脚本修改。

## 功能

| 时间 | 提醒 |
|---|---|
| 07:15 | 早间简报：第几周、当天课表与教室、学习块、午休时长、今晚熄灯时间 |
| 课前 30 分钟 | 课程名 + 教室（08:30 前的早课不单独发，已并入简报） |
| 学习块开始 | 名称 + 这一块该怎么做 |
| 三餐 | 时间与提示（早饭并入简报） |
| 熄灯 | `settings.lightout_at` + 当周目标入睡时间 |
| 周日 17:00 | 复盘提醒 |

## 可视化页面（AstrBot WebUI）

插件带一个官方「插件页面」视图，在 WebUI 里打开：

```
#/plugin-view/astrbot_plugin_whale_agenda/schedule
```

路径：WebUI → 插件 → 小鲸鱼日程提醒 → 详情 → 视图 `schedule`。

页面里可以：

- **课表**：按周一到周日排的看板，点任意卡片改时间、教室、周次，列底部「＋ 新增」
- **自习块**：列表编辑，可设「不提醒的周次」（例如第 13-16 周暂停）
- **三餐 / 作息**：直接改时间、文案、各周次的入睡与起床时间
- **设置**：学期第 1 周、作息调整第 1 周、熄灯时间、时区

所有改动即时校验并写回 `schedule.json`，不用重启。页面通过 `views/schedule/`
里的静态文件加载，后端只有 `/{plugin}/data` 和 `/{plugin}/save` 两个接口，
都走 Dashboard 的登录态。

## 安装

1. 把整个目录放进 `data/plugins/astrbot_plugin_whale_agenda/`。
2. 重启 AstrBot，在 WebUI 的插件配置里填 **主人 QQ 号**（必填，留空不发任何消息）。
3. 首次启动会把 `schedule.example.json` 复制成
   `data/plugin_data/astrbot_plugin_whale_agenda/schedule.json`，改这个文件即可。
4. 课表与真实作息不符时，改 `settings.semester_start`（第 1 周周一）和
   `settings.sleep_start`（作息调整第 1 周周一）。

## 数据文件

`plugin_data/astrbot_plugin_whale_agenda/schedule.json`：

```json
{
  "settings": {
    "semester_start": "2026-09-07",
    "sleep_start": "2026-09-07",
    "lightout_at": "23:30",
    "timezone": "Asia/Shanghai"
  },
  "courses": [
    {"day": 0, "start": "08:00", "span": "08:00-09:40",
     "name": "高等数学", "room": "教三 201", "weeks": [[1, 16]]}
  ],
  "blocks": [
    {"day": 1, "start": "19:00", "span": "19:00-20:40",
     "label": "英语", "reason": "背单词 + 跟读", "skip_weeks": [[13, 16]]}
  ],
  "meals": [
    {"at": "11:35", "label": "午饭", "hint": "避开 12:00 高峰"}
  ],
  "sleep_plan": [
    {"from_week": 1, "to_week": 1, "bedtime": "01:50",
     "wake_class": "07:20", "wake_late": "07:50", "nap": 45}
  ]
}
```

- `day`：0=周一 … 6=周日。
- `weeks` / `skip_weeks`：闭区间的周次列表，`[[1, 8], [10, 16]]` 表示第 1-8 周和第 10-16 周。
- `span` 只影响显示，调度用 `start`。
- `sleep_plan` 按「作息调整周次」生效，与 `semester_start` 无关；`wake_class` 用于有 08:30 前早课的日子。
- 改完文件最多 20 秒生效（tick 检测 mtime 自动重载），不用重启。

## HTTP API

只监听 `127.0.0.1`，所有请求必须带 token：

```bash
TOKEN=$(cat data/plugin_data/astrbot_plugin_whale_agenda/api_token.txt)
curl -H "X-Api-Token: $TOKEN" http://127.0.0.1:6190/api/schedule
```

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET/PUT | `/api/schedule` | 整份读取 / 整份替换 |
| GET/POST | `/api/schedule/courses` | 课程列表 / 新增一节课 |
| PATCH/DELETE | `/api/schedule/courses/{id}` | 改 / 删某节课 |
| GET/POST/PATCH/DELETE | `/api/schedule/blocks…` | 自习块，同上 |
| GET/PUT | `/api/schedule/settings` | 学期起点、熄灯时间等 |
| GET/PUT | `/api/schedule/meals` | 三餐整表 |
| GET/PUT | `/api/schedule/sleep_plan` | 作息阶段整表 |
| POST | `/api/reload` | 外部改完文件后立即重载 |

新增一节课：

```bash
curl -X POST http://127.0.0.1:6190/api/schedule/courses \
  -H "X-Api-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"day":3,"start":"14:00","span":"14:00-15:40","name":"概率论","room":"教二 305","weeks":[[1,16]]}'
```

校验失败返回 400 和具体原因，文件保持原样；写入是原子替换，不会写出半个 JSON。

## 主人绑定

WebUI 插件页面 →「设置」→ 填 **主人 QQ** → 自动保存。绑定后：

- 提醒只发这个号（`{platform_id}:FriendMessage:{owner_qq}`）
- 主人的对话带身份注入，人格卡里“对主人温顺”的条款才会生效
- 三个日程查询工具只对主人开放，别人问会得到「这是主人的私人日程」

没绑定时会回退到插件配置里的 `owner_qq`；两处都空则不发提醒、也不开放工具。

## 三个 LLM 工具

| 工具 | 参数 | 用途 |
|---|---|---|
| `whale_schedule_today` | 无 | 今天概览与下一件事 |
| `whale_schedule_upcoming` | `hours`（1-168，默认 12） | 接下来 N 小时，跨天按日期分段 |
| `whale_schedule_day` | `day`（今天/明天/下周三/2026-10-30） | 指定某天的完整安排 |

工具只读，不能改提醒状态；改课表走页面或 API。

## 身份注入与压行

- 每轮 LLM 请求前把「发言人 QQ + 昵称 + 是否主人」拼进 system prompt；
- 回复里的换行改成标点，读起来是一行。

两项都能在插件配置里单独关掉（`enable_owner_guard`、`flatten_replies`）。

## 防撞车

和其他“识别主人 / 注入身份 / 压行”的插件共存时：

| 冲突 | 处理 |
|---|---|
| 重复注入身份 | 本插件用自己的哨兵段，重复调用只替换不叠加 |
| 别的模块已写身份块 | 保留它的内容，追加本插件段落，并记一条告警（同一条只记一次） |
| 别人把另一个 QQ 当主人 | 告警指出两个号，但不覆盖——以本插件绑定为准 |
| 同名 LLM 工具被重复注册 | 启动时扫描注册表，发现多提供方就告警 |

告警前缀统一是 `[whale-safety]`，在日志里 `grep whale-safety` 就能看到全部冲突记录。
配置项 `safety_guard` 关掉后只停止冲突检测与告警，身份注入照常工作（注入始终去重，不会叠加）。

## 只发私聊

发送目标固定为 `{platform_id}:FriendMessage:{owner_qq}`，不读消息事件里的会话，
所以结构上不可能发进群。群聊里发命令一律拒绝。

## 命令（仅主人私聊）

`提醒` 看状态 · `提醒 测试` 立刻发一条 · `提醒 静默 2` · `提醒 开` / `提醒 关` ·
`提醒 周次 9` 临时强制周次（`提醒 周次 自动` 恢复）

## 分享要注意

- 插件目录**不含任何个人数据**：示例课表在 `schedule.example.json`，真实日程在
  `plugin_data/`，分发时不用清理。
- 对方首次启动会把示例复制成自己的 `schedule.json`，直接改就行。
- API token 每台机器自动生成，互不相同；`api_host` 保持 `127.0.0.1` 最安全。
- 配套插件 `astrbot_plugin_whale_schedule_tools` 把日程包成 LLM 工具，让模型能在对话里
  查“今天/明天/某天”的安排；它不是必需的，但配上体验更完整。
