"""黑名单逻辑核心模块 - 处理黑名单数据管理和同步"""

import asyncio
from typing import Dict, Optional, Union
from datetime import datetime, timedelta

from .api import PimengAPI
from .cache import BlacklistCache


class BlacklistService:
    """黑名单服务管理器"""
    
    def __init__(self, api: PimengAPI, cache: BlacklistCache, sync_interval: int, logger, handler=None):
        self.api = api
        self.cache = cache
        self.sync_interval = sync_interval
        self.logger = logger
        self.handler = handler
        
        # 黑名单数据
        self.user_blacklist: Dict[str, dict] = {}
        self.group_blacklist: Dict[str, dict] = {}
        
        # 状态
        self.last_sync: Optional[datetime] = None
        self.sync_task: Optional[asyncio.Task] = None
        
        # 查询限流（按用户限流，避免全局限流导致的"互相抢限流"问题）
        self.user_query_times: Dict[str, datetime] = {}  # key: user_id, value: last_query_time
        self.query_cooldown = 5  # 查询冷却时间（秒）
        
        # 查询缓存（短期缓存，避免重复查询API）
        self.query_cache: Dict[str, tuple] = {}  # key: "type_id", value: (result, timestamp)
        self.query_cache_ttl = 300  # 缓存有效期（秒）5分钟
        
        # 同步锁，防止并发同步
        self._sync_lock = asyncio.Lock()
    
    async def initialize(self):
        """初始化服务"""
        await self.sync_blacklist()
        self.sync_task = asyncio.create_task(self._scheduled_sync())
        # 启动缓存清理任务（每5分钟清理一次过期缓存）
        self.cache_cleanup_task = asyncio.create_task(self._scheduled_cache_cleanup())
        
        self.logger.info(f"BlacklistService initialized | Users: {len(self.user_blacklist)} | Groups: {len(self.group_blacklist)} | Sync: {self.sync_interval//60}min")
    
    async def terminate(self):
        """清理资源"""
        if self.sync_task:
            self.sync_task.cancel()
            try:
                await self.sync_task
            except asyncio.CancelledError:
                pass
        
        if hasattr(self, 'cache_cleanup_task'):
            self.cache_cleanup_task.cancel()
            try:
                await self.cache_cleanup_task
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
    
    async def _scheduled_cache_cleanup(self):
        """定时缓存清理"""
        while True:
            try:
                # 每5分钟清理一次过期缓存
                await asyncio.sleep(300)
                self._clean_expired_cache()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Cache cleanup error: {e}")
                await asyncio.sleep(60)
    
    async def sync_blacklist(self, force: bool = False) -> bool:
        """同步云黑库
        
        Args:
            force: 是否强制同步，忽略冷却时间
            
        Returns:
            bool: 同步是否成功
        """
        # 使用锁防止并发同步
        async with self._sync_lock:
            return await self._sync_blacklist_internal(force)
    
    async def _sync_blacklist_internal(self, force: bool = False) -> bool:
        """内部同步方法（已加锁）"""
        if not self.api.bot_token:
            return False
        
        # 检查冷却时间（1分钟）
        if not force and self.last_sync:
            time_since_last_sync = datetime.now() - self.last_sync
            if time_since_last_sync.total_seconds() < 60:
                self.logger.debug(f"同步冷却中，上次同步于 {self.last_sync.strftime('%H:%M:%S')}，{int(60 - time_since_last_sync.total_seconds())}秒后可同步")
                return False
        
        try:
            if self.api.bot_token:
                # 只记录Token是否存在，不输出任何片段（安全考虑）
                token_length = len(self.api.bot_token)
                self.logger.debug(f"开始同步，Token已配置（长度: {token_length}）")
            else:
                self.logger.warning("未配置Bot Token，跳过同步")
                return False
            result = await self.api.get_blacklist()
            
            if not result.get("success"):
                error_msg = result.get('message', 'Unknown')
                self.logger.error(f"Sync failed: {error_msg}")
                
                # 根据错误类型提供建议
                if "401" in error_msg:
                    self.logger.warning("认证失败 (401): 请检查Bot Token是否正确")
                elif "403" in error_msg:
                    self.logger.warning("权限不足 (403): Token可能已过期或无权访问")
                return False
            
            remote_list = result.get("data", {}).get("blacklist", [])
            if not isinstance(remote_list, list):
                self.logger.error("Sync failed: Invalid blacklist format")
                return False
            
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
            
            # 清理 quit_groups 中已经不存在的群组
            if hasattr(self, 'handler') and hasattr(self.handler, 'quit_groups'):
                current_groups = set(self.group_blacklist.keys())
                self.handler.quit_groups = {g for g in self.handler.quit_groups if g in current_groups}
            
            self.last_sync = datetime.now()
            self.logger.info(f"Sync OK | Users: {old_users}->{len(self.user_blacklist)}, Groups: {old_groups}->{len(self.group_blacklist)}")
            return True
            
        except Exception as e:
            self.logger.error(f"Sync exception: {e}")
            return False
    
    def can_query_api(self, user_id: str = None) -> bool:
        """检查是否可以查询API（限流检查）
        
        Args:
            user_id: 用户ID，如果为None则使用全局限流（向后兼容）
        
        Returns:
            bool: 是否可以查询
        """
        # 如果没有提供user_id，使用全局限流（向后兼容）
        if user_id is None:
            # 检查是否有任何用户最近查询过（简单的全局限流）
            if not self.user_query_times:
                return True
            
            # 找到最近的一次查询
            last_query_time = max(self.user_query_times.values())
            time_since_last_query = datetime.now() - last_query_time
            if time_since_last_query.total_seconds() < self.query_cooldown:
                self.logger.debug(f"全局查询限流中，上次查询于 {last_query_time.strftime('%H:%M:%S')}，{int(self.query_cooldown - time_since_last_query.total_seconds())}秒后可查询")
                return False
            return True
        
        # 按用户限流
        if user_id not in self.user_query_times:
            return True
        
        last_query_time = self.user_query_times[user_id]
        time_since_last_query = datetime.now() - last_query_time
        if time_since_last_query.total_seconds() < self.query_cooldown:
            self.logger.debug(f"用户 {user_id} 查询限流中，上次查询于 {last_query_time.strftime('%H:%M:%S')}，{int(self.query_cooldown - time_since_last_query.total_seconds())}秒后可查询")
            return False
        
        return True
    
    def update_query_time(self, user_id: str = None):
        """更新查询时间
        
        Args:
            user_id: 用户ID，如果为None则更新全局限流时间（向后兼容）
        """
        if user_id is None:
            # 如果没有提供user_id，更新所有用户的查询时间（模拟全局限流）
            current_time = datetime.now()
            for uid in list(self.user_query_times.keys()):
                self.user_query_times[uid] = current_time
        else:
            self.user_query_times[user_id] = datetime.now()
    
    def get_cached_query(self, target_id: str, query_type: str) -> Optional[dict]:
        """获取缓存的查询结果"""
        cache_key = f"{query_type}_{target_id}"
        if cache_key in self.query_cache:
            result, timestamp = self.query_cache[cache_key]
            # 检查缓存是否过期
            if (datetime.now() - timestamp).total_seconds() < self.query_cache_ttl:
                self.logger.debug(f"使用缓存查询结果: {cache_key}")
                return result
            else:
                # 缓存过期，删除
                del self.query_cache[cache_key]
        return None
    
    def set_cached_query(self, target_id: str, query_type: str, result: dict):
        """设置查询缓存"""
        cache_key = f"{query_type}_{target_id}"
        self.query_cache[cache_key] = (result, datetime.now())
        self.logger.debug(f"缓存查询结果: {cache_key}")
        
        # 优化：概率性清理过期缓存，而不是每次设置时都清理
        # 当缓存大小较大时增加清理概率
        import random
        cache_size = len(self.query_cache)
        cleanup_probability = min(0.1, cache_size / 1000)  # 最大10%概率
        if random.random() < cleanup_probability:
            self._clean_expired_cache()
    
    def _clean_expired_cache(self):
        """清理过期缓存"""
        now = datetime.now()
        expired_keys = []
        for key, (_, timestamp) in self.query_cache.items():
            if (now - timestamp).total_seconds() >= self.query_cache_ttl:
                expired_keys.append(key)
        
        for key in expired_keys:
            del self.query_cache[key]
        
        if expired_keys:
            self.logger.debug(f"清理了 {len(expired_keys)} 个过期缓存")
    
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
    
    def get_stats(self) -> Dict[str, Union[int, str]]:
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