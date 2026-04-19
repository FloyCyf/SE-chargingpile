# 智能充电桩调度计费系统 API 接口说明

## 1. 接口概览

| 接口路径 | 方法 | 功能描述 |
|---------|------|----------|
| `/api/order/{order_id}` | GET | 获取充电订单详情 |
| `/api/pile/status` | GET | 获取所有充电桩实时状态 |
| `/api/pile/statistics` | GET | 获取统计数据 |
| `/api/queue/list` | GET | 获取排队车辆列表 |
| `/api/pay/{order_id}` | POST | 模拟支付接口 |

## 2. 接口详情

### 2.1 获取充电订单详情

**请求路径**: `/api/order/{order_id}`

**请求方法**: GET

**路径参数**:
| 参数名 | 类型 | 必填 | 描述 |
|-------|------|------|------|
| order_id | string | 是 | 订单编号 |

**返回示例**:

```json
{
  "order_id": "ORD000001",
  "pile_id": "P001",
  "electricity": 25.5,
  "duration": 3600,
  "start_time": "2026-04-19T10:00:00",
  "end_time": "2026-04-19T11:00:00",
  "electricity_fee": 20.4,
  "service_fee": 2.55,
  "total_fee": 22.95,
  "payment_status": "未支付"
}
```

### 2.2 获取所有充电桩实时状态

**请求路径**: `/api/pile/status`

**请求方法**: GET

**返回示例**:

```json
[
  {
    "pile_id": "P001",
    "status": "充电中",
    "current_user": "User123",
    "charging_duration": 1800,
    "charging_electricity": 12.5
  },
  {
    "pile_id": "P002",
    "status": "空闲",
    "current_user": null,
    "charging_duration": 0,
    "charging_electricity": 0
  }
]
```

### 2.3 获取统计数据

**请求路径**: `/api/pile/statistics`

**请求方法**: GET

**返回示例**:

```json
{
  "total_piles": 10,
  "free_piles": 4,
  "charging_piles": 5,
  "fault_piles": 1,
  "today_charging_count": 12,
  "today_electricity": 150.5,
  "today_revenue": 120.4
}
```

### 2.4 获取排队车辆列表

**请求路径**: `/api/queue/list`

**请求方法**: GET

**返回示例**:

```json
[
  {
    "queue_id": "QUE0001",
    "user_id": "User456",
    "requested_electricity": 30.0,
    "queue_duration": 600,
    "queue_status": "排队中"
  },
  {
    "queue_id": "QUE0002",
    "user_id": "User789",
    "requested_electricity": 20.0,
    "queue_duration": 300,
    "queue_status": "已完成"
  }
]
```

### 2.5 模拟支付接口

**请求路径**: `/api/pay/{order_id}`

**请求方法**: POST

**路径参数**:
| 参数名 | 类型 | 必填 | 描述 |
|-------|------|------|------|
| order_id | string | 是 | 订单编号 |

**返回示例**:

```json
{
  "message": "支付成功"
}
```

## 3. 数据库结构

### 3.1 充电订单表 (charging_orders)

| 字段名 | 数据类型 | 描述 |
|-------|---------|------|
| order_id | string | 订单编号（主键） |
| pile_id | string | 桩编号 |
| electricity | float | 电量 |
| duration | integer | 时长（秒） |
| start_time | datetime | 开始时间 |
| end_time | datetime | 结束时间 |
| electricity_fee | float | 电费 |
| service_fee | float | 服务费 |
| total_fee | float | 总费用 |
| payment_status | string | 支付状态 |

### 3.2 充电桩状态表 (pile_status)

| 字段名 | 数据类型 | 描述 |
|-------|---------|------|
| pile_id | string | 桩编号（主键） |
| status | string | 状态：空闲/充电中/故障 |
| current_user | string | 当前用户 |
| charging_duration | integer | 已充时长（秒） |
| charging_electricity | float | 已充电量 |

### 3.3 排队信息表 (queue_info)

| 字段名 | 数据类型 | 描述 |
|-------|---------|------|
| queue_id | string | 排队ID（主键） |
| user_id | string | 用户ID |
| requested_electricity | float | 请求电量 |
| queue_duration | integer | 排队时长（秒） |
| queue_status | string | 排队状态 |

## 4. 启动和运行

1. 安装依赖：
   ```bash
   pip install fastapi uvicorn sqlalchemy
   ```

2. 启动后端服务：
   ```bash
   python main.py
   ```

3. 访问前端页面：
   - 结账大屏：`http://localhost:8000/checkout.html?order_id=ORD000001`
   - 管理员数据看板：`http://localhost:8000/dashboard.html`

4. 访问API文档：
   - Swagger UI：`http://localhost:8000/docs`
   - ReDoc：`http://localhost:8000/redoc`