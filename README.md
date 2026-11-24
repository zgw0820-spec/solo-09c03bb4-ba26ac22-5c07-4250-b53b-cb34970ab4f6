# IPv6 换前缀 · 旧地址到期核查（Flask + SQLite）

园区网管场景：IPv6 分配前缀更换后，追查每台终端**旧前缀地址**在核查时刻是否仍然有效、
到期时刻是多少、依据是哪条路由器通告。数据保存为**一轮核查**（一个 SQLite 文件）。

## 功能与规则口径

- 一轮核查登记两个 UTC 时刻：**快照时刻**与**核查时刻**；登记终端时填写快照时该终端
  单个旧地址的到期 UTC 时刻，**无地址留空**。
- 按终端补报“快照之后、核查之前（含核查时刻）”收到的**旧前缀 PIO**，只接受有效的 SLAAC
  通告：表单只采集 A 标志有效通告的字段（路由器、序号、UTC 接收时刻、有限有效寿命秒数）；
  有效寿命必须是有限整数 `0 .. 4294967294`（`4294967295/0xFFFFFFFF` 为无限寿命，拒绝）。
- 补报**可乱序**，服务端按 `(接收时刻, 序号)` 排序后重放，应用 **RFC 4862 §5.5.3(e)
  未认证通告**规则（另含 §5.5.3(d) 无地址时的形成规则）：

  | 分支 | 条件 | 对有效寿命/到期的动作 |
  |---|---|---|
  | (d) | 当前无该前缀 SLAAC 地址 | 寿命非 0 才形成，到期=接收+寿命；寿命 0 不形成 |
  | (e).1 | 收到寿命 > 7200s，**或** > RemainingLifetime | 到期重设为 接收+收到寿命 |
  | (e).2 | RemainingLifetime ≤ 7200s | 未认证通告被忽略，到期不变（防 DoS 提前到期） |
  | (e).3 | 其余（剩余 >7200s 且收到较短寿命，含寿命 0） | 到期截断为 接收+7200s |

- 结论页只列**核查时刻仍持有旧地址**的终端（到期时刻严格晚于核查时刻；恰好相等视为已到期），
  给出到期时刻与**最后一条改变它的通告**（路由器/序号/接收时刻/寿命）；从未被通告改变则
  注明“快照未改”。终端详情页给出每条通告的规则分支、应用前后到期与文字依据。
- **增量重算**：一次补报只重算实际收到新记录的终端；其他终端可继续补报，其结论行不动。
- **原样重报不重算**：同终端同序号且内容（路由器/接收时刻/寿命）完全相同 → 幂等跳过。
- **同号异内容整批退回**：连同批次内其他终端的新记录一起不落库，返回逐行错误，
  旧结论保持可查。所有写入在单条 `BEGIN IMMEDIATE` 事务内完成（通告 + 结论 + 依据一起生效），
  冲突时带退避重试，失败回滚。
- 所有时刻必须显式带时区（`Z` / `+00:00` 等），内部统一存 UTC 纪元秒，展示为 `...Z`。

## 目录

```
app/            Flask 应用（engine.py 规则引擎，services.py 事务/校验，routes.py 路由）
scripts/        seed_sample.py 样例数据
tests/          pytest：引擎规则单测 + 网页/API/事务端到端测试
Dockerfile, docker-compose.yml, requirements.txt
```

## 用 Docker Compose 启动

```bash
docker compose up --build
# 打开 http://localhost:8000
```

数据存于命名卷 `audit-data`（容器内 `/data/audit.db`，WAL 模式）。
重置演示：网页“清空本轮”，或 `docker compose down -v`。

## 本地直接运行（无 Docker 时）

```bash
pip install -r requirements.txt
python -m scripts.seed_sample     # 可选：灌入样例（库为空时才执行）
python -m app                     # http://localhost:8000
python -m pytest tests/ -q        # 自测
```

## 可试的样例（scripts/seed_sample.py）

快照 `2026-11-20T00:00:00Z`，核查 `2026-11-20T06:00:00Z`，4 台终端、8 条乱序通告：

| 终端 | 快照到期 | 通告效果 | 核查时 |
|---|---|---|---|
| PC-A | 04:00 | 01:00 短寿命 3600s，(e).3 截断到 03:00 | 已到期 |
| PC-B | 02:00 | 两条短寿命均命中 (e).2 被忽略，到期保持 02:00 | 已到期 |
| PC-C | 02:00 | 长寿命 10000s (e).1 重设到 03:46:40；后续短寿命 (e).2 忽略 | 已到期 |
| PC-D | 无地址 | 寿命 0 不形成 → (d) 形成 → (e).1 重设到次日 03:00 | **仍持有** |

## HTTP / JSON 接口

| 方法路径 | 说明 |
|---|---|
| `GET /` | 结论总览（仍持有列表 + 全部终端） |
| `GET /setup` `POST /setup` | 建立本轮核查时刻（仅一次） |
| `POST /terminals` | 登记终端与快照到期（可空） |
| `GET /terminals/<id>` | 单终端规则链依据 |
| `GET /pio` `POST /pio-batch` | 网页补报表单（多行整批） |
| `POST /api/round` `/api/terminals` `/api/pio-batch` `GET /api/results` | 等价 JSON API |

```bash
curl -s localhost:8000/api/results
curl -s -X POST localhost:8000/api/pio-batch \
  -H 'Content-Type: application/json' -d '[
    {"terminal":"PC-A","seq":7,"router":"R1",
     "received_at":"2026-11-20T05:00:00Z","valid_lifetime":3600}]'
```

## 建模边界与说明

- 题目限定“旧前缀”的有效 SLAAC PIO，因此不采集 A 标志、L 标志、前缀本身与 preferred
  lifetime；A=0、链路本地前缀、preferred>valid 等无效通告在录入范围之外（§5.5.3(a)-(c)
  本就静默忽略）。规则引擎只建模 valid lifetime，preferred lifetime 不参与到期结论。
- 寿命 0 的处理按 RFC 文本走分支：无地址时 (d) 不形成；有地址且剩余 >2h 时落入 (e).3
  截断为 2h，剩余 ≤2h 时落入 (e).2 忽略。
- “最后一条改变它的通告”指实际改变到期时刻的最后一条（(e).1/(e).3/(d)）；被忽略的
  (e).2 与计算后到期不变的 (e).1 不计入。
