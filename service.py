"""黑名单逻辑核心模块 - 处理黑名单数据管理和同步"""

import asyncio
from typing import Dict, Optional
from datetime import datetime, timedelta

from .api import PimengAPI
from .cache import BlacklistCache


class BlacklistService:
    """黑名单服务管理器"""
    
    def __init__(self, api: PimengAPI, cache: BlacklistCache, sync_interval: int, logger):
        self.api = api
        self.cache = cache
        self.sync_interval = sync_interval
        self.logger = logger
        
        # 黑名单数据
        self.user_blacklist: Dict[str, dict] = {}
        self.group_blacklist: Dict[str, dict] = {}
        
        # 状态
        self.last_sync: Optional[datetime] = None
        self.sync_task: Optional[asyncio.Task] = None
    
    async def initialize(self):
        """初始化服务"""
        await self.sync_blacklist()
        self.sync_task = asyncio.create_task(self._scheduled_sync())
        
        self.logger.info(f"BlacklistService initialized | Users: {len(self.user_blacklist)} | Groups: {len(self.group_blacklist)} | Sync: {self.sync_interval//60}min")
    
    async def terminate(self):
        """清理资源"""
        if self.sync_task:
            self.sync_task.cancel()
            try:
                await self.sync_task
            except asyncio.CancelledError:
                pass
    
    async def _scheduled_sync(self):
        """定时同步"""
        while True:
            try:
                await asyncio.sleep(self.sync_interval)
                await self.sync_blacklist()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Scheduled sync error: {e}")
                await asyncio.sleep(60)
    
    async def sync_blacklist(self):
        """同步云黑库"""
        if not self.api.bot_token:
            return
        
        try:
            result = await self.api.get_blacklist()
            
            if not result.get("success"):
                self.logger.error(f"Sync failed: {result.get('message', 'Unknown')}")
                return
            
            remote_list = result.get("data", {}).get("blacklist", [])
            if not isinstance(remote_list, list):
                self.logger.error("Sync failed: Invalid blacklist format")
                return
            
            old_users = len(self.user_blacklist)
            old_groups = len(self.group_blacklist)
            
            self.user_blacklist.clear()
            self.group_blacklist.clear()
            
            for item in remote_list:
                user_id = str(item.get("user_id", ""))
                if not user_id:
                    continue
                
                data = {
                    "level": item.get("level", 1),
                    "reason": item.get("reason", "Unknown"),
                    "added_at": item.get("added_at", "Unknown"),
                    "added_by": item.get("added_by", "Unknown"),
                }
                
                if item.get("user_type") == "group":
                    self.group_blacklist[user_id] = data
                else:
                    self.user_blacklist[user_id] = data
            
            # 清理过期提醒记录
            current_users = set(self.user_blacklist.keys())
            self.cache.clean_expired_records(current_users)
            
            self.last_sync = datetime.now()
            self.logger.info(f"Sync OK | Users: {old_users}->{len(self.user_blacklist)}, Groups: {old_groups}->{len(self.group_blacklist)}")
            
        except Exception as e:
            self.logger.error(f"Sync exception: {e}")
    
    def is_user_blacklisted(self, user_id: str) -> bool:
        """检查用户是否在黑名单中"""
        return user_id in self.user_blacklist
    
    def is_group_blacklisted(self, group_id: str) -> bool:
        """检查群组是否在黑名单中"""
        return group_id in self.group_blacklist
    
    def get_user_data(self, user_id: str) -> Optional[dict]:
        """获取用户黑名单数据"""
        return self.user_blacklist.get(user_id)
    
    def get_group_data(self, group_id: str) -> Optional[dict]:
        """获取群组黑名单数据"""
        return self.group_blacklist.get(group_id)
    
    def remove_user(self, user_id: str):
        """从用户黑名单中移除"""
        self.user_blacklist.pop(user_id, None)
    
    def remove_group(self, group_id: str):
        """从群组黑名单中移除"""
        self.group_blacklist.pop(group_id, None)
    
    def get_stats(self) -> Dict[str, int]:
        """获取统计信息"""
        return {
            "user_blacklist": len(self.user_blacklist),
            "group_blacklist": len(self.group_blacklist),
            "last_sync": self.last_sync.strftime("%H:%M:%S") if self.last_sync else "Never",
            "next_sync_in": self._get_next_sync_minutes()
        }
    
    def _get_next_sync_minutes(self) -> int:
        """获取下次同步剩余分钟数"""
        if not self.last_sync:
            return 0
        
        next_sync = self.last_sync + timedelta(seconds=self.sync_interval)
        time_diff = (next_sync - datetime.now()).total_seconds()
        return max(0, int(time_diff // 60))