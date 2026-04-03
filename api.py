"""网络请求模块 - 处理与皮梦云黑库API的通信"""

import asyncio
import http.client
import json
import urllib.parse
from typing import Dict, Optional
from datetime import datetime


class PimengAPI:
    """皮梦云黑库API客户端"""
    
    def __init__(self, api_base: str, bot_token: str, request_timeout: int, logger):
        self.api_base = api_base.rstrip("/")
        self.bot_token = bot_token
        self.request_timeout = request_timeout
        self.logger = logger
        
        # 解析api_base获取主机名
        parsed = urllib.parse.urlparse(self.api_base)
        self.host = parsed.netloc or "cloudblack-api.07210700.xyz"
    
    async def check_blacklist(self, user_id: str, user_type: str = "user") -> dict:
        """检查用户/群组是否在黑名单中"""
        return await self._make_request("POST", "/api/bot/check", {
            "user_id": user_id,
            "user_type": user_type
        })
    
    async def add_to_blacklist(self, user_id: str, user_type: str, reason: str, level: int) -> dict:
        """添加到黑名单"""
        return await self._make_request("POST", "/api/bot/add", {
            "user_id": user_id,
            "user_type": user_type,
            "reason": reason,
            "level": level
        })
    
    async def remove_from_blacklist(self, user_id: str, user_type: str, reason: str) -> dict:
        """从黑名单移除"""
        return await self._make_request("POST", "/api/bot/delete", {
            "user_id": user_id,
            "user_type": user_type,
            "reason": reason
        })
    
    async def get_blacklist(self) -> dict:
        """获取黑名单列表"""
        return await self._make_request("GET", "/api/bot/getlist")
    
    async def _make_request(self, method: str, endpoint: str, data: dict = None) -> dict:
        """发送API请求（带重试）"""
        # 重试逻辑
        for attempt in range(2):
            result = await asyncio.to_thread(self._make_sync_request, method, endpoint, data)
            
            if result.get("success"):
                return result
            
            if attempt == 0:
                self.logger.warning(f"API request failed, retrying: {result.get('message')}")
                await asyncio.sleep(1)
        
        return result
    
    def _make_sync_request(self, method: str, endpoint: str, data: dict = None) -> dict:
        """同步HTTP请求"""
        conn = http.client.HTTPSConnection(self.host, timeout=self.request_timeout)
        try:
            headers = {
                "Authorization": self.bot_token,
                "User-Agent": "PimengBlacklist/2.6.0",
                "Accept": "application/json",
            }
            
            if method.upper() != "GET":
                headers["Content-Type"] = "application/json"
                payload = json.dumps(data) if data else None
            else:
                payload = None
            
            conn.request(method, endpoint, payload, headers)
            res = conn.getresponse()
            response_data = res.read().decode("utf-8")
            
            if res.status == 200:
                try:
                    return {"success": True, **json.loads(response_data)}
                except json.JSONDecodeError:
                    return {"success": False, "message": f"JSON解析错误"}
            else:
                return {"success": False, "message": f"HTTP {res.status}"}
                
        except Exception as e:
            return {"success": False, "message": str(e)}
        finally:
            conn.close()