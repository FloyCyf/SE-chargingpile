"""
扩展调度策略模块 — 可插拔的策略对象

设计要点:
- 每种策略实现 DispatchPolicy.assign(), 返回 Assignment 列表
- 策略对象只做"分配决策", 不直接修改 piles/waiting; 写库由 SmartScheduler._apply_assignments 统一处理
- 默认策略仍是 FIFO (与原 _dispatch_from_waiting_area 行为一致);
  新增 batch_min_total (单次调度总充电时长最短).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class Assignment:
    """一次策略产出的 (桩, 车) 配对"""
    pile_id: str
    pile_type: str
    pile_obj: object          # 实际引用, 调用方用来调 .queue.append
    car: dict                 # 原始 queue_item
    completion_hours: float   # 分配后该车在桩上的完成时刻 (用于调试/统计)


class DispatchPolicy(ABC):
    """调度策略抽象基类"""

    name: str = "abstract"

    @abstractmethod
    def assign(self, piles: List[object], waiting: List[dict],
               current_vtime) -> List[Assignment]:
        """
        输入: piles 全部桩, waiting 等候区全部车(快+慢), 当前虚拟时间
        输出: Assignment 列表, 顺序即为"提交顺序"
        约束:
          - 必须按 (车.charge_type == 桩.type) 匹配 (慢充车不进快充桩)
          - 不能分到 FAULT 桩
          - 不能让任何桩的 len(queue) > max_queue_length
          - 同一辆车不能被分两次
        """


# 工厂缓存 (策略无状态, 单例即可)
_registry: dict = {}


def register_policy(policy_cls):
    """装饰器: 把策略类注册到工厂"""
    _registry[policy_cls.name] = policy_cls
    return policy_cls


def get_policy(name: str) -> DispatchPolicy:
    """根据名称获取策略实例 (单例)"""
    if name not in _registry:
        raise ValueError(f"未知调度策略: {name}; "
                         f"已注册: {sorted(_registry.keys())}")
    return _registry[name]()


def available_policies() -> List[str]:
    return sorted(_registry.keys())


# ---------------------------------------------------------------------------
#  触发子模块注册 (放在最后避免循环引用)
# ---------------------------------------------------------------------------
from .fifo_policy import FIFOPolicy               # noqa: E402,F401
from .batch_min_total_policy import BatchMinTotalPolicy   # noqa: E402,F401
