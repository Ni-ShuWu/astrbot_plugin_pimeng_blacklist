"""缓存管理模块 - 处理提醒记录"""

from typing import Dict, Set, Optional
from datetime import datetime


class BlacklistCache:
    """黑名单缓存管理器"""
    
    def __init__(self):
        self.private_warned: Dict[str, datetime] = {}
    
    def clean_expired_records(self, current_users: Set[str]):
        """清理过期的提醒记录"""
        expired = set(self.private_warned.keys()) - current_users
        for user_id in expired:
            self.private_warned.pop(user_id, None)
    
    def get_private_warn_time(self, user_id: str) -> Optional[datetime]:
        """获取用户的私聊提醒时间"""
        return self.private_warned.get(user_id)
    
    def set_private_warn_time(self, user_id: str, time: datetime):
        """设置用户的私聊提醒时间"""
        self.private_warned[user_id] = time
    
    def remove_private_warn(self, user_id: str):
        """移除用户的私聊提醒记录"""
        self.private_warned.pop(user_id, None)
    
    def get_cache_stats(self) -> Dict[str, int]:
        """获取缓存统计信息"""
        return {
            "private_warned_size": len(self.private_warned)
        }