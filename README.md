# Campaign Audience Engine

促销活动的定向人群与优惠凭证服务：活动生命周期、人群圈选、凭证发放/核销、
推荐归因、批量发放与权限审计，全部在同一服务内，并保证并发与异常场景下的状态一致。

## 运行

```bash
uv sync
uv run python main.py          # 启动服务（CAMPAIGN_DB 指定数据库路径，默认 campaign.db）
uv run pytest tests/ -q        # 运行测试
```

身份通过请求头传递：`X-Actor: <主体>`，`X-Roles: admin,operator,agent,viewer`。

## 活动状态机

```
draft ──► scheduled ──► active ──► paused
  │                     ▲  │          │
  └──────────► active ──┘  │          │
                           ▼          ▼
                          ended ◄─────┘
```

| 状态 | 语义 |
|---|---|
| `draft` | 草稿，可预约（scheduled）或直接发布（active） |
| `scheduled` | 已预约，`scheduled_at` 到点后自动进入 active |
| `active` | 进行中，唯一允许发放凭证的状态 |
| `paused` | 暂停发放，可恢复 active 或结束 |
| `ended` | 终态，不可再流转 |

- 非法流转返回 `INVALID_STATE_TRANSITION`（HTTP 409），message 含当前/目标状态；
- 预约自动激活是**惰性**的：读取活动或启动服务时按当前时间与持久化的
  `scheduled_at` 校正，重启后状态与预约时间不会错乱；
- 状态变更带乐观锁，并发流转只有一个成功，其余收到 `CONFLICT`。

## 人群规则

规则为 JSON 树，可组合用户属性与历史行为：

```json
{"all": [
  {"attr": "age", "op": "gte", "value": 18},
  {"any": [
    {"behavior": "purchase", "op": "gte", "value": 2, "within_days": 30},
    {"attr": "vip", "op": "eq", "value": true}
  ]}
]}
```

- 操作符：`eq/ne/gt/gte/lt/lte/in`；行为条件按时间窗内事件计数；
- **确定性**：求值是纯函数，圈选按 user_id 排序输出，同规则同数据结果完全一致；
- **版本化**：`PUT /rules/{id}` 产生新版本，旧版本永久保留；
  发放记录绑定规则版本（`rule_version`），规则改动不影响已发凭证，
  新旧结果都可用 `POST /audience/explain` 解释到叶子条件。

## 约束配置

创建活动时配置：

| 字段 | 语义 |
|---|---|
| `total_quota` | 活动总量，耗尽后发放返回 `QUOTA_EXHAUSTED`，批量任务自动停止 |
| `per_user_limit` | 每人限领，超出返回 `PER_USER_LIMIT_REACHED` |
| `audience_rule_id/version` | 绑定人群规则，发放时可按规则校验资格 |

一致性保证：

- **幂等**：发放/核销都需 `idempotency_key`，重复提交返回首次结果，不产生第二条记录；
- **不超发**：额度扣减为单条条件 UPDATE，与凭证插入同事务，并发/多实例下总额精确；
- **核销唯一**：状态条件 UPDATE，同一凭证不可能核销两次，重复核销返回 `ALREADY_REDEEMED`。

## 推荐归因

- `POST /referrals` 登记关系：自我推荐（`REFERRAL_SELF`）与环形推荐
  （`REFERRAL_CYCLE`）被拒绝；一个被推荐人只能归属一个推荐人；
- `POST /conversions` 上报转化：事件可乱序/迟到到达——事件先到则关系建立时
  补归因，关系先到则事件到达即归因，两种顺序结果一致；
- 同一事件只归因一次（`event_id` 主键），同一次推荐只归因一次
  （`(referrer, referee)` 唯一）。

## 批量发放与恢复

`POST /campaigns/{id}/batches` 创建并执行批量任务：

- 每个用户的发放在独立事务中提交，游标（`committed`）随每条提交推进；
- 中途失败（如额度耗尽）：已提交部分完整保留，无部分写入，任务标记
  `failed` 并记录原因；
- `POST /batches/{id}/resume` 从上次已提交位置继续，幂等键由
  `(batch_id, seq)` 派生，重跑不会重复发放。

## 权限与审计

| 动作 | 允许角色 |
|---|---|
| 发布/暂停/结束活动、发放、批量 | `admin`, `operator` |
| 代客核销 | `admin`, `agent` |

越权返回 `FORBIDDEN`（HTTP 403）。所有鉴权判定（含拒绝）写入 `audit_log`，
`GET /audit` 可追溯。

## 错误码

`INVALID_STATE_TRANSITION` `CAMPAIGN_NOT_FOUND` `CAMPAIGN_NOT_ACTIVE`
`RULE_NOT_FOUND` `RULE_INVALID` `VOUCHER_NOT_FOUND` `QUOTA_EXHAUSTED`
`PER_USER_LIMIT_REACHED` `ALREADY_REDEEMED` `USER_NOT_IN_AUDIENCE`
`REFERRAL_SELF` `REFERRAL_CYCLE` `REFERRAL_DUPLICATE`
`FORBIDDEN` `UNAUTHENTICATED` `VALIDATION` `CONFLICT`

## 存储与并发

SQLite（WAL + busy_timeout），多步写均在 `BEGIN IMMEDIATE` 事务内；
请求级连接，天然支持多实例共用同一数据库文件。
