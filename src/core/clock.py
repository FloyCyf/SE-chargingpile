import time
from datetime import datetime, timedelta

class VirtualClock:
    """系统全局虚拟时钟（单例化管理）"""
    _instance = None

    def __new__(cls, config=None):
        if cls._instance is None:
            cls._instance = super(VirtualClock, cls).__new__(cls)
            cls._instance._init(config)
        return cls._instance

    def _init(self, config):
        if config:
            self.ratio = config['simulation'].get('virtual_minutes_per_real_second', 1)
        else:
            self.ratio = 1
        self.real_start_time = time.time()
        self.virtual_start_time = datetime.now()
        self.running = False

    def get_time(self) -> datetime:
        """获取此时刻折算倍率后的系统虚拟时间"""
        if not self.running:
            return self.virtual_start_time
        real_elapsed_seconds = time.time() - self.real_start_time
        # 流逝现实的 N 秒 = 虚拟世界里流逝的 N * ratio 分钟
        virtual_elapsed_minutes = real_elapsed_seconds * self.ratio
        return self.virtual_start_time + timedelta(minutes=virtual_elapsed_minutes)
        
    def set_time(self, dt: datetime):
        """将虚拟时间设定到指定时刻（重置基准点，倍率不变）"""
        self.real_start_time = time.time()
        self.virtual_start_time = dt

    def set_ratio(self, ratio: float):
        """修改时间推进倍率（先冻结当前虚拟时间，再以新倍率继续推进）"""
        current_vtime = self.get_time()
        self.real_start_time = time.time()
        self.virtual_start_time = current_vtime
        self.ratio = ratio

    def start(self):
        """开始虚拟时间流逝。"""
        if not self.running:
            self.real_start_time = time.time()
            self.running = True

    def pause(self):
        """暂停虚拟时间流逝，并固定在暂停瞬间。"""
        if self.running:
            self.virtual_start_time = self.get_time()
            self.real_start_time = time.time()
            self.running = False

    def reset(self):
        """重置虚拟时间为当前真实时间（倍率不变）"""
        self.real_start_time = time.time()
        self.virtual_start_time = datetime.now()
        self.running = False
