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

    def get_time(self) -> datetime:
        """获取此时刻折算倍率后的系统虚拟时间"""
        real_elapsed_seconds = time.time() - self.real_start_time
        # 流逝现实的 N 秒 = 虚拟世界里流逝的 N * ratio 分钟
        virtual_elapsed_minutes = real_elapsed_seconds * self.ratio
        return self.virtual_start_time + timedelta(minutes=virtual_elapsed_minutes)
        
    def reset(self):
        """如果需要强行重置系统沙盒时间刻度"""
        self.real_start_time = time.time()
        self.virtual_start_time = datetime.now()
