"""
FIFO 调度策略 — 等候区顺序遍历, 每辆车放到"完成时刻最早"的桩.

与 src/core/scheduler.py:216-253 的 _find_optimal_pile 算法保持一致:
  - 候选桩: 同类型 且 has_space (非 FAULT 且 len(queue) < max_queue_length)
  - 桩的总时间 = pile.remaining_time_hours() + car.requested_kwh / pile.power
  - 容忍阈值 6 分钟: 差距在 6 min 内时按队列长度打破平局 (负载均衡)
"""
from typing import List

from . import Assignment, DispatchPolicy, register_policy


# 与 SmartScheduler._PILE_SELECTION_TOLERANCE 保持一致 (6 min)
_PILE_SELECTION_TOLERANCE = 6.0 / 60.0  # 0.10 h


def _select_best_pile(piles, charge_type, requested_kwh, virtual_queue_lens):
    """
    挑选"完成时刻最早"的桩 (内联自 scheduler._find_optimal_pile).
    virtual_queue_lens: dict {id(pile): virtual_len} 用于判断该桩是否还有空位.
    返回 (pile, completion_time) 或 None.
    """
    candidates = []
    for p in piles:
        if p.type != charge_type:
            continue
        original_len = len(p.queue)
        virtual_len = virtual_queue_lens.get(id(p), original_len)
        if p.status == "FAULT" or virtual_len >= p.max_queue_length:
            continue
        candidates.append((p, virtual_len))

    if not candidates:
        return None

    best_pile = None
    best_time = float('inf')
    best_queue_len = float('inf')
    for pile, vlen in candidates:
        # 用虚拟长度重算 R (越粗略越保守)
        # 简单近似: 不重算 R, 沿用 pile.remaining_time_hours()
        wait_time = pile.remaining_time_hours()
        own_time = requested_kwh / pile.power
        total = wait_time + own_time
        qlen = vlen

        if total < best_time - _PILE_SELECTION_TOLERANCE:
            best_time = total
            best_queue_len = qlen
            best_pile = pile
        elif total <= best_time + _PILE_SELECTION_TOLERANCE:
            if qlen < best_queue_len:
                best_time = total
                best_queue_len = qlen
                best_pile = pile
    return best_pile, best_time


@register_policy
class FIFOPolicy(DispatchPolicy):
    """FIFO 顺序调度: 严格按 waiting 列表顺序, 逐车放最优桩"""

    name = "fifo"

    def assign(self, piles, waiting, current_vtime) -> List[Assignment]:
        assignments: List[Assignment] = []
        # 维护一份 "本轮已占用" 的逻辑副本, 不真的改 piles
        virtual_queue_lens = {id(p): len(p.queue) for p in piles}

        for car in waiting:
            charge_type = car.get("charge_type", "Slow")
            requested_kwh = car.get("requested_kwh", 0.0)
            result = _select_best_pile(
                piles, charge_type, requested_kwh, virtual_queue_lens)
            if result is None:
                continue
            pile, completion = result
            virtual_queue_lens[id(pile)] += 1
            assignments.append(Assignment(
                pile_id=pile.pile_id,
                pile_type=pile.type,
                pile_obj=pile,
                car=car,
                completion_hours=round(completion, 4),
            ))
        return assignments
