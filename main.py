from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
import asyncio
import http.client
import json
import urllib.parse
from typing import Dict, Set, Optional
from datetime import datetime, timedelta
from functools import wraps

__version__ = "2.6.0"

# 常量定义
LEVEL_NAMES = {1: "轻微", 2: "一般", 3: "平台", 4: "严重"}
LEVEL_EMOJIS = {1: "🟢", 2: "🟡", 3: "🔴", 4: "⛔"}
BOT_JOIN_KEYWORDS = ["邀请", "加入了群聊", "加入了", "加入群", "加群"]


def require_op(func):
    """管理员权限检查装饰器"""
    @wraps(func)
    async def wrapper(self, event: AstrMessageEvent, *args, **kwargs):
        if not self._check_op(event):
            yield event.plain_result("❌ 权限不足，仅管理员可用。")
            return
        async for result in func(self, event, *args, **kwargs):
            yield result
    return wrapper


def require_token(func):
    """Bot Token检查装饰器"""
    @wraps(func)
    async def wrapper(self, event: AstrMessageEvent, *args, **kwargs):
        if not self.bot_token:
            yield event.plain_result("❌ 未配置 Bot Token。")
            return
        async for result in func(self, event, *args, **kwargs):
            yield result
    return wrapper


@register(
    "pimeng_blacklist",
    "N(Ni-ShuWu),P(Pimeng's)",
    "基于皮梦云黑库接入插件，可查询用户是否在黑名单中",
    __version__
)
class PimengBlacklistPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.logger = logger
        
        # 配置读取
        self.api_base = config.get("api_base", "https://cloudblack-api.07210700.xyz").rstrip("/")
        self.bot_token = config.get("bot_token", "")
        self.sync_interval = max(60, min(config.get("sync_interval", 300), 3600))
        self.clean_cache_max = max(100, config.get("clean_cache_max", 10000))
        self.enable_auto_kick = config.get("enable_auto_kick", True)
        self.request_timeout = max(1, min(config.get("request_timeout", 10), 30))
        
        # 状态
        self.last_sync: Optional[datetime] = None
        self.sync_task: Optional[asyncio.Task] = None
        
        # 黑名单数据
        self.user_blacklist: Dict[str, dict] = {}
        self.group_blacklist: Dict[str, dict] = {}
        self.clean_cache: Set[str] = set()
        
        # 提醒记录
        self.private_warned: Dict[str, datetime] = {}
        
        # 退群失败记录（避免高频重复尝试）
        self._quit_group_failed: Set[str] = set()
    
    async def initialize(self):
        """初始化"""
        if not self.bot_token:
            self.logger.warning("Bot Token not configured! Plugin will work in read-only mode.")
        
        await self._sync_blacklist()
        self.sync_task = asyncio.create_task(self._scheduled_sync())
        
        self.logger.info(f"PimengBlacklist v{__version__} | Users: {len(self.user_blacklist)} | Groups: {len(self.group_blacklist)} | Sync: {self.sync_interval//60}min")
    
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
                await self._sync_blacklist()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Scheduled sync error: {e}")
                await asyncio.sleep(60)
    
    async def _sync_blacklist(self):
        """同步云黑库"""
        if not self.bot_token:
            return
        
        try:
            result = await self._api_request("GET", "/api/bot/getlist")
            
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
            self.clean_cache.clear()
            self._quit_group_failed.clear()  # 清空退群失败记录
            
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
            self._clean_expired_records(current_users)
            
            self.last_sync = datetime.now()
            self.logger.info(f"Sync OK | Users: {old_users}->{len(self.user_blacklist)}, Groups: {old_groups}->{len(self.group_blacklist)}")
            
        except Exception as e:
            self.logger.error(f"Sync exception: {e}")
    
    def _clean_expired_records(self, current_users: Set[str]):
        """清理过期的提醒记录"""
        expired = set(self.private_warned.keys()) - current_users
        for user_id in expired:
            self.private_warned.pop(user_id, None)
    
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def blacklist_interceptor(self, event: AstrMessageEvent):
        """拦截云黑用户"""
        user_id = str(event.get_sender_id())
        
        # 检查干净缓存
        if user_id in self.clean_cache:
            return
        
        # 检查用户黑名单
        if user_id in self.user_blacklist:
            await self._handle_blacklisted_user(event, user_id)
            return
        
        # 检查群组黑名单
        if self._is_group_event(event):
            group_id = str(event.get_group_id())
            if group_id in self.group_blacklist:
                await self._handle_blacklisted_group(event, group_id)
                return
        
        # 加入干净缓存
        self._add_to_clean_cache(user_id)
    
    def _is_group_event(self, event: AstrMessageEvent) -> bool:
        """判断是否为群聊事件"""
        return hasattr(event, 'get_group_id') and event.get_group_id() is not None
    
    async def _handle_blacklisted_user(self, event: AstrMessageEvent, user_id: str):
        """处理黑名单用户 - 修复：统一拦截逻辑"""
        data = self.user_blacklist[user_id]
        level = data.get("level", 1)
        is_group = self._is_group_event(event)
        
        # 私聊模式：发送提醒（仅主动唤醒时每天一次）
        if not is_group:
            is_wake_up = getattr(event, 'is_wake_up', lambda: False)()
            
            if is_wake_up and self._should_warn_private(user_id):
                # 修复：使用 async for 消费生成器
                async for result in self._send_blacklist_warning(event, level, data):
                    yield result
        
        # 统一拦截：无论群聊还是私聊，都终止事件
        event.stop_event()
    
    async def _send_blacklist_warning(self, event: AstrMessageEvent, level: int, data: dict):
        """发送黑名单警告消息"""
        yield event.plain_result(
            f"⚠️ 您已被列入云黑名单。\n"
            f"━━━━━━━━━━━━━━\n"
            f"违规等级: {level}\n"
            f"原因: {data.get('reason', '未知')}\n"
            f"━━━━━━━━━━━━━━\n"
            f"详情与申诉: https://云黑.皮梦.wtf"
        )
    
    def _should_warn_private(self, user_id: str) -> bool:
        """检查是否应该发送私聊提醒"""
        now = datetime.now()
        last_warn = self.private_warned.get(user_id)
        
        if last_warn is None or last_warn.date() != now.date():
            self.private_warned[user_id] = now
            return True
        return False
    
    async def _handle_blacklisted_group(self, event: AstrMessageEvent, group_id: str):
        """处理黑名单群组 - 修复：避免高频重复退群"""
        # 如果已经尝试过退群且失败，不再重复尝试
        if group_id in self._quit_group_failed:
            event.stop_event()
            return
        
        data = self.group_blacklist[group_id]
        level = data.get("level", 1)
        
        self.logger.info(f"Bot in blacklisted group | Group: {group_id} | Level: {level}")
        
        # 尝试退群
        if hasattr(self.context, 'quit_group'):
            success, error = await self._safe_execute(
                self.context.quit_group, group_id
            )
            if success:
                self.logger.info(f"Bot quit group | Group: {group_id}")
            else:
                # 记录退群失败，避免高频重复尝试
                self._quit_group_failed.add(group_id)
                self.logger.error(f"Quit group failed | Group: {group_id} | Error: {error}")
        
        event.stop_event()
    
    def _add_to_clean_cache(self, user_id: str):
        """添加用户到干净缓存"""
        if len(self.clean_cache) >= self.clean_cache_max:
            self.clean_cache = set(list(self.clean_cache)[self.clean_cache_max // 2:])
        self.clean_cache.add(user_id)
    
    async def _safe_execute(self, func, *args, **kwargs):
        """安全执行异步函数"""
        try:
            result = await func(*args, **kwargs)
            return result, None
        except Exception as e:
            return None, str(e)
    
    def _check_op(self, event: AstrMessageEvent) -> bool:
        """检查是否为管理员"""
        return getattr(event, 'is_admin', lambda: False)()
    
    @filter.command("bl_status")
    @require_op
    async def cmd_status(self, event: AstrMessageEvent):
        """查看同步状态 - 修复：时间计算"""
        if not self.last_sync:
            yield event.plain_result("❌ 首次同步尚未完成")
            return
        
        next_sync = self.last_sync + timedelta(seconds=self.sync_interval)
        time_diff = (next_sync - datetime.now()).total_seconds()
        # 修复：使用 total_seconds() 避免负数问题
        time_left = max(0, int(time_diff // 60))
        
        yield event.plain_result(
            f"🛡️ 皮梦云黑库 v{__version__}\n"
            f"━━━━━━━━━━━━━━\n"
            f"👤 用户黑名单: {len(self.user_blacklist)}\n"
            f"👥 群组黑名单: {len(self.group_blacklist)}\n"
            f"🗑️ 干净缓存: {len(self.clean_cache)}\n"
            f"🕐 上次同步: {self.last_sync.strftime('%H:%M:%S')}\n"
            f"⏰ 下次同步: {time_left}分钟后\n"
            f"🔄 同步间隔: {self.sync_interval // 60}分钟"
        )
    
    @filter.command("bl_sync")
    @require_op
    async def cmd_sync(self, event: AstrMessageEvent):
        """强制同步"""
        yield event.plain_result("🔄 正在同步云黑库...")
        await self._sync_blacklist()
        yield event.plain_result(
            f"✅ 同步完成\n"
            f"👤 用户: {len(self.user_blacklist)}\n"
            f"👥 群组: {len(self.group_blacklist)}"
        )
    
    @filter.command("bl_check")
    async def cmd_check(self, event: AstrMessageEvent, target: str = None, user_type: str = "user"):
        """检查黑名单状态 - 修复：API失败时正确处理"""
        # 参数处理
        if target is not None:
            target = str(target)
            if not target.isdigit():
                yield event.plain_result("❌ 参数错误：ID必须是数字")
                return
        
        if user_type not in ("user", "group"):
            yield event.plain_result("❌ 参数错误：user_type必须是'user'或'group'")
            return
        
        target_id = str(target or event.get_sender_id())
        blacklist = self.group_blacklist if user_type == "group" else self.user_blacklist
        type_name = "群组" if user_type == "group" else "用户"
        
        # 本地检查
        if target_id in blacklist:
            data = blacklist[target_id]
            level = data.get("level", 1)
            yield event.plain_result(
                f"⚠️ {type_name}已被拉黑（本地）\n"
                f"━━━━━━━━━━━━━━\n"
                f"ID: {target_id}\n"
                f"等级: {LEVEL_EMOJIS.get(level, '⚪')} {level} ({LEVEL_NAMES.get(level, '未知')})\n"
                f"原因: {data.get('reason', '未知')}\n"
                f"添加时间: {data.get('added_at', '未知')}\n"
                f"添加者: {data.get('added_by', '未知')}"
            )
            return
        
        # API实时检查
        result = await self._api_check(target_id, user_type)
        
        # 修复：先检查API调用是否成功
        if not result.get("success"):
            error_msg = result.get('message', '未知错误')
            yield event.plain_result(f"❌ 查询失败: {error_msg}")
            return
        
        # API调用成功，检查是否在黑名单中
        if result.get("in_blacklist"):
            data = result.get("data", {})
            yield event.plain_result(
                f"⚠️ {type_name}已被拉黑（实时）\n"
                f"ID: {target_id}\n"
                f"等级: {data.get('level', 1)}\n"
                f"⚠️ 不在本地缓存，将自动同步"
            )
        else:
            yield event.plain_result(f"✅ {type_name} {target_id} 未被拉黑")
    
    @filter.command("bl_add")
    @require_op
    @require_token
    async def cmd_add(self, event: AstrMessageEvent, user_id: str = None, reason: str = None, level: int = 1):
        """添加到黑名单 - 修复：等级范围统一为1-4"""
        if not user_id or not reason:
            yield event.plain_result("❌ 参数错误：需要提供user_id和reason")
            return
        
        user_id = str(user_id)
        
        try:
            level = int(level)
        except (ValueError, TypeError):
            yield event.plain_result("❌ 参数错误：level必须是数字")
            return
        
        if not user_id.isdigit():
            yield event.plain_result("❌ 参数错误：user_id必须是数字")
            return
        
        # 修复：等级范围统一为1-4，与LEVEL_NAMES一致
        if not 1 <= level <= 4:
            yield event.plain_result("❌ 参数错误：level必须在1-4之间")
            return
        
        # 等级4需要在管理面板操作
        if level == 4:
            yield event.plain_result("❌ 等级4需要在管理面板操作: https://云黑.皮梦.wtf/admin")
            return
        
        result = await self._api_request("POST", "/api/bot/add", {
            "user_id": user_id,
            "user_type": "user",
            "reason": reason,
            "level": level
        })
        
        if result.get("success"):
            self.clean_cache.discard(user_id)
            await self._sync_blacklist()
            yield event.plain_result(
                f"✅ 已添加到黑名单\n"
                f"用户: {user_id}\n"
                f"等级: {LEVEL_EMOJIS.get(level, '⚪')} {level}\n"
                f"原因: {reason}"
            )
        else:
            yield event.plain_result(f"❌ 添加失败: {result.get('message', '未知错误')}")
    
    @filter.command("bl_remove")
    @require_op
    @require_token
    async def cmd_remove(self, event: AstrMessageEvent, user_id: str = None, reason: str = ""):
        """从黑名单移除"""
        if not user_id:
            yield event.plain_result("❌ 参数错误：需要提供user_id")
            return
        
        user_id = str(user_id)
        
        if not user_id.isdigit():
            yield event.plain_result("❌ 参数错误：user_id必须是数字")
            return
        
        result = await self._api_request("POST", "/api/bot/delete", {
            "user_id": user_id,
            "user_type": "user",
            "reason": reason or "管理员移除"
        })
        
        if result.get("success"):
            self.user_blacklist.pop(user_id, None)
            self.clean_cache.discard(user_id)
            self.private_warned.pop(user_id, None)
            yield event.plain_result(f"✅ 已从黑名单移除: {user_id}")
        else:
            yield event.plain_result(f"❌ 移除失败: {result.get('message', '未知错误')}")
    
    @filter.command("bl_list")
    @require_op
    async def cmd_list(self, event: AstrMessageEvent, page: int = 1):
        """查看黑名单列表"""
        try:
            page = int(page)
        except (ValueError, TypeError):
            yield event.plain_result("❌ 参数错误：page必须是数字")
            return
        
        if page < 1:
            yield event.plain_result("❌ 参数错误：page必须大于0")
            return
        
        # 合并用户和群组黑名单
        all_items = [
            (uid, data, "用户") 
            for uid, data in self.user_blacklist.items()
        ] + [
            (uid, data, "群组") 
            for uid, data in self.group_blacklist.items()
        ]
        
        if not all_items:
            yield event.plain_result("✅ 黑名单为空")
            return
        
        per_page = 15
        total = len(all_items)
        pages = (total + per_page - 1) // per_page
        page = min(page, pages)
        
        start = (page - 1) * per_page
        page_items = all_items[start:start + per_page]
        
        lines = [f"📋 黑名单 ({total}) 第{page}/{pages}页", "━━━━━━━━━━━━━━"]
        
        for uid, data, type_name in page_items:
            level = data.get("level", 1)
            emoji = LEVEL_EMOJIS.get(level, "⚪")
            reason = data.get('reason', 'N/A')[:12]
            lines.append(f"{emoji} [{type_name[0]}] {uid} | L{level} | {reason}...")
        
        lines.append("━━━━━━━━━━━━━━")
        lines.append(f"使用 /bl_list <页码> 查看更多")
        
        yield event.plain_result("\n".join(lines))
    
    @filter.command("bl_help")
    async def cmd_help(self, event: AstrMessageEvent):
        """显示帮助"""
        is_op = self._check_op(event)
        
        lines = [
            f"🛡️ 皮梦云黑库 v{__version__}",
            "━━━━━━━━━━━━━━",
            "📋 命令列表",
            "━━━━━━━━━━━━━━",
            "/bl_check [ID] [user/group] - 检查黑名单状态",
        ]
        
        if is_op:
            lines.extend([
                "━━━━━━━━━━━━━━",
                "🔐 管理员命令",
                "/bl_status - 查看同步状态",
                "/bl_sync - 强制同步",
                "/bl_add <QQ> <原因> [等级] - 添加黑名单",
                "/bl_remove <QQ> [原因] - 移除黑名单",
                "/bl_list [页码] - 查看列表",
            ])
        
        lines.extend([
            "━━━━━━━━━━━━━━",
            "📝 参数说明",
            "• ID: QQ号或群号",
            "• user/group: 查询类型，默认user",
            "• 等级: 1-轻微 2-一般 3-平台 4-严重（等级4需面板操作）",
            "━━━━━━━━━━━━━━",
            "🌐 申诉: https://云黑.皮梦.wtf",
            f"👤 身份: {'管理员' if is_op else '用户'}",
        ])
        
        yield event.plain_result("\n".join(lines))
    
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_member_join(self, event: AstrMessageEvent):
        """处理成员加入群组事件"""
        group_id = str(event.get_group_id())
        message_str = getattr(event, 'message_str', '') or ''
        
        # 检查是否为Bot加入事件
        if not any(kw in message_str.lower() for kw in BOT_JOIN_KEYWORDS):
            return
        
        # 检查群组是否在黑名单中
        if group_id not in self.group_blacklist:
            return
        
        data = self.group_blacklist[group_id]
        level = data.get("level", 1)
        
        self.logger.info(f"Bot joined blacklisted group | Group: {group_id} | Level: {level}")
        
        # 尝试退群
        if hasattr(self.context, 'quit_group'):
            success, error = await self._safe_execute(
                self.context.quit_group, group_id
            )
            if success:
                self.logger.info(f"Bot quit group | Group: {group_id}")
            else:
                self.logger.error(f"Quit group failed | Group: {group_id} | Error: {error}")
    
    async def _api_check(self, user_id: str, user_type: str = "user") -> dict:
        """API检查黑名单"""
        return await self._api_request("POST", "/api/bot/check", {
            "user_id": user_id,
            "user_type": user_type
        })
    
    async def _api_request(self, method: str, endpoint: str, data: dict = None) -> dict:
        """发送API请求（带重试）- 修复：使用配置的api_base"""
        # 修复：解析配置的api_base获取主机名
        parsed = urllib.parse.urlparse(self.api_base)
        host = parsed.netloc or "cloudblack-api.07210700.xyz"
        
        def _make_request():
            conn = http.client.HTTPSConnection(host, timeout=self.request_timeout)
            try:
                headers = {
                    "Authorization": self.bot_token,
                    "User-Agent": f"PimengBlacklist/{__version__}",
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
        
        # 重试逻辑
        for attempt in range(2):
            result = await asyncio.to_thread(_make_request)
            
            if result.get("success"):
                return result
            
            if attempt == 0:
                self.logger.warning(f"API request failed, retrying: {result.get('message')}")
                await asyncio.sleep(1)
        
        return result
