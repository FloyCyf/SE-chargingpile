"""
单次调度总充电时长最短策略 (Batch Min-Total-Time).

与 FIFO 的核心区别: 不按提交顺序逐车放, 而是从等候区中选一个"批次",
使得 Σ(等待时间 + 充电时间) 最小. 当有多个空位时, 一次性叫多号.

算法 (按车数规模自适应):
  - 阶段 A: 评估总空位, 计算"最多能塞下几辆车"
  - 阶段 B: 若 N ≤ DP_MAX (=10), 用精确 DP 枚举所有 (P+1)^N 种分组,
            找到最小总成本的分配
  - 阶段 C: 若 N > DP_MAX, 退化为最小成本增量贪心:
            每步选择一组 (车, 桩), 使本步加入后 Σ完成时刻增量最小

成本定义 (单桩):
  桩 p 当前剩余 R 小时, 加入 N_p 辆车按 SPT 升序排列:
    1 号车: 完成时刻 R + k[0]/p_power
    i 号车: 完成时刻 R + sum_{j<i} k[j]/p_power
  Σ(完成时刻) = N_p * R + (1/p_power) * Σ_{i=0..N_p-1}((N_p - i) * k[i])

不变量:
  - 一辆车不会被分到两个桩
  - 一根桩不会被分到超过 (max_queue_length - 当前长度) 辆车
  - 故障桩 (status == FAULT) 完全跳过
  - 类型不匹配的车不进错类型桩 (快充车不进慢充桩)
"""
import itertools
from collections import defaultdict
from typing import List

from . import Assignment, DispatchPolicy, register_policy


# 小规模上限: 默认等候区容量为 10; 3 根快充桩时 4^10 约 104 万,
# 对课程项目规模仍可接受, 且能覆盖 G9 验收常见上限.
DP_MAX = 10


def _pile_cost(ks_sorted_asc: List[float], R: float, power: float) -> float:
    """
    单桩成本: 该桩加入 N 辆车后, Σ(完成时刻) (按 SPT 升序排列).
    ks_sorted_asc: 升序排列的 kWh 列表
    R: 桩当前剩余时间 (小时)
    power: 桩功率 (kW)
    """
    if not ks_sorted_asc:
        return 0.0
    n = len(ks_sorted_asc)
    cost = n * R
    for i, k in enumerate(ks_sorted_asc):
        weight = n - i  # 最小的 kWh 拿到最大权重 N
        cost += weight * k / power
    return cost


def _build_assignments_for_partition(partition, cars, pile_info, ks):
    """
    把 partition (dict: pile_idx -> list of car_idx) 展开成 Assignment 列表,
    按车在该桩的 SPT 完成时刻顺序排 (用于前端展示).
    """
    result = []
    n_piles = len(pile_info)
    for p_idx, car_indices in partition.items():
        if p_idx == n_piles:  # "unassigned" 桶 → 跳过
            continue
        pile = pile_info[p_idx]
        # SPT 升序
        sorted_cars = sorted(car_indices, key=lambda i: ks[i])
        cumulative_kwh = 0.0
        for ci in sorted_cars:
            cumulative_kwh += ks[ci]
            completion = pile['R'] + cumulative_kwh / pile['power']
            result.append(Assignment(
                pile_id=pile['pile_id'],
                pile_type=pile['type'],
                pile_obj=pile['pile_obj'],
                car=cars[ci],
                completion_hours=round(completion, 4),
            ))
    return result


def _batch_dp(cars, pile_info, ks, dp_max=DP_MAX):
    """
    精确 DP: 枚举所有 (P+1)^N 分组, 字典序最优
      (1) 最大化已分配车辆数 (=总在桩车辆数)
      (2) 同等数量下, 最小化 Σ 成本.

    N 超过 dp_max 直接返回 None, 调用方退化为贪心.
    """
    N = len(cars)
    P = len(pile_info)
    if N == 0:
        return [], 0.0
    if N > dp_max:
        return None  # 调用方负责回退

    best_assigned = -1        # 已分配数 (主目标: 越大越好)
    best_cost = float('inf')  # 成本 (次目标: 越小越好)
    best_partition = None

    # 第 P 个桶 = "不分配" (留在等候区)
    for assignment in itertools.product(range(P + 1), repeat=N):
        groups = defaultdict(list)
        for car_i, bucket in enumerate(assignment):
            groups[bucket].append(car_i)
        feasible = True
        for p_idx in range(P):
            if len(groups[p_idx]) > pile_info[p_idx]['max_new']:
                feasible = False
                break
        if not feasible:
            continue
        assigned_count = sum(len(groups[p]) for p in range(P))
        cost = 0.0
        for p_idx in range(P):
            ks_on_pile = sorted([ks[i] for i in groups[p_idx]])
            cost += _pile_cost(ks_on_pile,
                               pile_info[p_idx]['R'],
                               pile_info[p_idx]['power'])
        # 字典序: 先看 assigned_count (大优), 再看 cost (小优)
        if (assigned_count > best_assigned
                or (assigned_count == best_assigned and cost < best_cost)):
            best_assigned = assigned_count
            best_cost = cost
            best_partition = dict(groups)

    if best_partition is None or best_assigned < 0:
        return [], 0.0
    return _build_assignments_for_partition(
        best_partition, cars, pile_info, ks), best_cost


def _batch_greedy(cars, pile_info, ks):
    """
    贪心回退: 每一步从所有未分配车辆和可用桩位中选成本增量最小的一对.
    该启发式贴近 "Σ完成时刻最短" 目标, 会自然优先短作业, 避免 LPT
    在本目标下把大车提前造成平均完成时间变差.
    """
    N = len(cars)
    P = len(pile_info)
    if N == 0 or P == 0:
        return [], 0.0

    # 记录每根桩"已虚拟分配"的车辆 idx
    pile_car_set = {p: [] for p in range(P)}
    unassigned = set(range(N))

    while unassigned:
        best_pair = None
        best_p = None
        best_cost_increase = float('inf')

        for ci in sorted(unassigned, key=lambda i: (ks[i], cars[i].get("queue_number", ""))):
            kwh = ks[ci]
            for p_idx, pile in enumerate(pile_info):
                if len(pile_car_set[p_idx]) >= pile['max_new']:
                    continue
                current_ks = sorted([ks[i] for i in pile_car_set[p_idx]])
                current_cost = _pile_cost(
                    current_ks, pile['R'], pile['power'])
                new_ks = sorted(current_ks + [kwh])
                new_cost = _pile_cost(new_ks, pile['R'], pile['power'])
                increase = new_cost - current_cost
                tie_key = (
                    increase,
                    pile['pile_id'],
                    kwh,
                    cars[ci].get("vehicle_id", ""),
                )
                best_key = (
                    best_cost_increase,
                    pile_info[best_p]['pile_id'] if best_p is not None else "",
                    ks[best_pair] if best_pair is not None else 0.0,
                    cars[best_pair].get("vehicle_id", "") if best_pair is not None else "",
                )
                if best_pair is None or tie_key < best_key:
                    best_cost_increase = increase
                    best_pair = ci
                    best_p = p_idx

        if best_pair is None or best_p is None:
            break
        pile_car_set[best_p].append(best_pair)
        unassigned.remove(best_pair)

    # 构造 partition 和 Assignment 列表
    partition = {p: idxs for p, idxs in pile_car_set.items() if idxs}
    total_cost = sum(
        _pile_cost(sorted([ks[i] for i in idxs]),
                   pile_info[p]['R'], pile_info[p]['power'])
        for p, idxs in partition.items())

    return _build_assignments_for_partition(
        partition, cars, pile_info, ks), total_cost


@register_policy
class BatchMinTotalPolicy(DispatchPolicy):
    """单次调度总充电时长最短"""

    name = "batch_min_total"

    def __init__(self, use_dp: bool = True, dp_max: int = DP_MAX):
        self.use_dp = use_dp
        self.dp_max = dp_max

    def assign(self, piles, waiting, current_vtime) -> List[Assignment]:
        # 1) 拆 fast / slow
        fast_piles_raw = [p for p in piles if p.type == "Fast" and p.has_space]
        slow_piles_raw = [p for p in piles if p.type == "Slow" and p.has_space]
        fast_waiting = [c for c in waiting if c.get("charge_type") == "Fast"]
        slow_waiting = [c for c in waiting if c.get("charge_type") == "Slow"]

        all_assignments: List[Assignment] = []

        # 2) 各自跑批优化
        for group_piles, group_waiting in [
            (fast_piles_raw, fast_waiting),
            (slow_piles_raw, slow_waiting),
        ]:
            if not group_piles or not group_waiting:
                continue
            pile_info = [{
                'pile_id': p.pile_id,
                'type': p.type,
                'pile_obj': p,
                'R': p.remaining_time_hours(),
                'power': p.power,
                'max_new': max(0, p.max_queue_length - len(p.queue)),
            } for p in group_piles]
            ks = [c.get("requested_kwh", 0.0) for c in group_waiting]

            if self.use_dp and len(group_waiting) <= self.dp_max:
                result = _batch_dp(group_waiting, pile_info, ks,
                                   dp_max=self.dp_max)
                if result is None:  # 超过 dp_max
                    result = _batch_greedy(group_waiting, pile_info, ks)
            else:
                result = _batch_greedy(group_waiting, pile_info, ks)
            if result:
                all_assignments.extend(result[0])

        # 3) 确定性: 按 (pile_id, completion_hours, vehicle_id) 排序
        all_assignments.sort(key=lambda a: (
            a.pile_id, a.completion_hours, a.car.get("vehicle_id", "")))
        return all_assignments
