# 小组 API 接口标准化协作规约

为了杜绝在后续“计费算法”、“智能排队”分工开发中出现端点错搭、参数不认、甩锅推诿的问题。本开发小组在编码第一准则上将严格执行本协定约束下的大一统标准开发行为！

## 1. 放弃手工文档，拥抱自动化契约

本团队**严禁任何成员使用 Markdown 甚至 Word 开源撰写冗长的死板 API 接口文档**。
所有的参数限制、必需非必需要求、缺省类型反馈，一律强制写入 FastAPI 的标准接口入口，并使用其最核心特权—— **Swagger UI 提供在线唯一事实沟通真理**。

* 任何同学完成了一个增量接口，只需要告诉前端同学：“我在 `http://127.0.0.1:8000/docs` 里把描述更新了”。
* 前端与调试人员只准以该接口动态文档提供的 Schema 骨架进行发包。

## 2. 强格式化类型对象输入 (Input Pydantic Schema)

不允许任何后端路由里面出现直接解析 `request.json()` 中字典键值的野路子！
所有的表单接口入参，必须在 `src/api/schemas.py` 里建立派生于 `pydantic.BaseModel` 的结构体并附带严厉注解，否则拒收 Pull Request：

```python
# 示例：【强制】正确示范
class ChargingRequest(BaseModel):
    vehicle_id: str = Field(..., example="京A88888", description="物理终端登记牌车号")
    charge_type: ChargeType = Field(..., description="快速/常规模式")
    current_soc: float = Field(..., ge=0.0, lt=1.0, description="当前容量比")
```

## 3. “三体结构”通用全局返回壳 (Output Protocol)

这是保障客户端无论在报错、成功、溢出下都能顺利解析并弹窗的至高保障。
组内所有人写出的接口返回值，必须抛弃原始数据体，包裹在这个标准化 JSON 三体响应格式外壳发送下流回客户端：

```json
{
    "code": 200,                        // 【必须】系统大类业务防线状态码 (如200正常, 400校验不过)
    "status": "success",                // 【必须】成功与否的高亮标识 ("success" | "rejected" | "error")
    "message": "京A88888 调度成功",      // 【必须】可以直接供前端大屏无脑原样 alert 或者气泡抛出的文字描述
    "data": {                           // 【随业务分配】当接口需要带出内容（如费用列表、账单字段等）一律缩进塞在这个嵌套内
        "order_id": "8488eef...w32",
        "detail": { ... }
    }
}
```

凡在 `routes.py` 等边缘处理出口未经上述格式标准私自返回随意内容的接口代码，一律按劣质质量判定。全量代码以此为纲。
