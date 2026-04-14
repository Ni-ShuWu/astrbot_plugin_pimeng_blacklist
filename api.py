"""网络请求模块 - 处理与皮梦云黑库API的通信"""

import asyncio
import http.client
import json
import urllib.parse


class PimengAPI:
    """皮梦云黑库API客户端"""
    
    def __init__(self, api_base: str, bot_token: str, request_timeout: int, logger):
        self.api_base = api_base.rstrip("/")
        self.bot_token = bot_token
        self.request_timeout = request_timeout
        self.logger = logger
        
        # 解析api_base获取scheme、主机名和路径
        parsed = urllib.parse.urlparse(self.api_base)
        self.scheme = parsed.scheme or "https"
        self.host = parsed.netloc or "cloudblack-api.07210700.xyz"
        self.base_path = parsed.path or ""
    
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
        # 根据scheme选择HTTP或HTTPS连接
        if self.scheme == "http":
            conn = http.client.HTTPConnection(self.host, timeout=self.request_timeout)
        else:
            conn = http.client.HTTPSConnection(self.host, timeout=self.request_timeout)
        
        try:
            headers = {
                "Authorization": self.bot_token if self.bot_token else "",
                "User-Agent": "PimengBlacklist/2.8.0",
                "Accept": "application/json",
            }
            
            if method.upper() != "GET":
                headers["Content-Type"] = "application/json"
                payload = json.dumps(data) if data else None
            else:
                payload = None
            
            # 构建完整的请求路径，避免双斜杠
            base_path = self.base_path.rstrip("/")
            endpoint = endpoint.lstrip("/")
            full_path = f"/{base_path}/{endpoint}" if base_path else f"/{endpoint}"
            
            conn.request(method, full_path, payload, headers)
            res = conn.getresponse()
            response_data = res.read().decode("utf-8")
            
            if res.status == 200:
                try:
                    api_response = json.loads(response_data)
                    # 确保返回结构一致
                    if "success" not in api_response:
                        api_response["success"] = True
                    return api_response
                except json.JSONDecodeError:
                    return {"success": False, "message": f"JSON解析错误: {response_data[:100]}"}
            else:
                # 添加详细的错误信息
                error_msg = f"HTTP {res.status}"
                if response_data:
                    try:
                        error_data = json.loads(response_data)
                        if "message" in error_data:
                            error_msg = f"HTTP {res.status}: {error_data['message']}"
                        elif "error" in error_data:
                            error_msg = f"HTTP {res.status}: {error_data['error']}"
                    except json.JSONDecodeError:
                        error_msg = f"HTTP {res.status}: {response_data[:200]}"
                return {"success": False, "message": error_msg}
                
        except (http.client.HTTPException, OSError, TimeoutError) as e:
            # 网络相关异常
            return {"success": False, "message": f"网络错误: {str(e)}"}
        except json.JSONDecodeError as e:
            # JSON解析异常
            return {"success": False, "message": f"响应解析错误: {str(e)}"}
        except Exception as e:
            # 其他未知异常，记录日志但不暴露细节
            self.logger.error(f"API请求未知异常: {type(e).__name__}: {str(e)}")
            return {"success": False, "message": "内部错误，请查看日志"}
        finally:
            conn.close()