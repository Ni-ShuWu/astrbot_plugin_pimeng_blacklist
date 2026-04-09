"""皮梦云黑库插件主入口 - 整合各个模块"""

import asyncio
from astrbot.api.event import filter, AstrMessageEvent # type: ignore
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from functools import wraps

from .api import PimengAPI
from .cache import BlacklistCache
from .service import BlacklistService
from .handler import EventHandler

__version__ = "2.8.0"

# 常量定义
LEVEL_NAMES = {1: "轻微", 2: "一般", 3: "平台", 4: "严重"}
LEVEL_EMOJIS = {1: "🟢", 2: "🟡", 3: "🔴", 4: "⛔"}


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
        if not self.api.bot_token:
            yield event.plain_result("❌ 未配置 Bot Token。")
            return
        async for result in func(self, event, *args, **kwargs):
            yield result
    return wrapper


@register(
    "astrbot_plugin_pimeng_blacklist",
    "N(Ni-ShuWu),P(Pimeng's)",
    "基于皮梦云黑库接入插件，可查询用户是否在黑名单中",
    __version__
)
class PimengBlacklistPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.logger = logger
        
        # 配置读取
        api_base = config.get("api_base", "https://cloudblack-api.07210700.xyz")
        bot_token = config.get("bot_token", "")
        sync_interval = max(60, min(config.get("sync_interval", 300), 3600))
        enable_auto_kick = config.get("enable_auto_kick", True)
        request_timeout = max(1, min(config.get("request_timeout", 10), 30))
        
        # 初始化各个模块
        self.api = PimengAPI(api_base, bot_token, request_timeout, self.logger)
        self.cache = BlacklistCache()
        self.handler = EventHandler(None, self.cache, enable_auto_kick, self.logger)
        self.service = BlacklistService(self.api, self.cache, sync_interval, self.logger, self.handler)
        self.handler.service = self.service
    
    async def initialize(self):
        """初始化"""
        if not self.api.bot_token:
            self.logger.warning("Bot Token not configured! Plugin will work in read-only mode.")
        
        await self.service.initialize()
    
    async def terminate(self):
        """清理资源"""
        await self.service.terminate()
    
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def blacklist_interceptor(self, event: AstrMessageEvent):
        """拦截云黑用户"""
        message = await self.handler.handle_message(event, self.context)
        if message:
            yield event.plain_result(message)
    
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_member_join(self, event: AstrMessageEvent):
        """处理成员加入群组事件"""
        await self.handler.handle_member_join(event, self.context)
    
    def _check_op(self, event: AstrMessageEvent) -> bool:
        """检查是否为管理员"""
        return getattr(event, 'is_admin', lambda: False)()
    
    @filter.command("bl_status")
    @require_op
    async def cmd_status(self, event: AstrMessageEvent):
        """查看同步状态"""
        stats = self.service.get_stats()
        cache_stats = self.cache.get_cache_stats()
        
        yield event.plain_result(
            f"🛡️ 皮梦云黑库 v{__version__}\n"
            f"━━━━━━━━━━━━━━\n"
            f"👤 用户黑名单: {stats['user_blacklist']}\n"
            f"👥 群组黑名单: {stats['group_blacklist']}\n"
            f"📝 提醒记录: {cache_stats['private_warned_size']}\n"
            f"🕐 上次同步: {stats['last_sync']}\n"
            f"⏰ 下次同步: {stats['next_sync_in']}分钟后\n"
            f"🔄 同步间隔: {self.service.sync_interval // 60}分钟"
        )
    
    @filter.command("bl_sync")
    @require_op
    async def cmd_sync(self, event: AstrMessageEvent):
        """强制同步（忽略冷却时间）"""
        yield event.plain_result("🔄 正在强制同步云黑库...")
        await self.service.sync_blacklist(force=True)
        yield event.plain_result(
            f"✅ 同步完成\n"
            f"👤 用户: {len(self.service.user_blacklist)}\n"
            f"👥 群组: {len(self.service.group_blacklist)}"
        )
    
    @filter.command("bl_check")
    async def cmd_check(self, event: AstrMessageEvent, target: str = None, user_type: str = None):
        """检查黑名单状态 - 不指定类型时同时查询用户和群组"""
        # 参数处理
        if target is not None:
            target = str(target)
            if not target.isdigit():
                yield event.plain_result("❌ 参数错误：ID 必须是数字")
                return
        
        # 如果没有指定类型，同时查询用户和群组
        if user_type is None:
            target_id = str(target or event.get_sender_id())
            results = []
            
            # 查询用户
            if self.service.is_user_blacklisted(target_id):
                data = self.service.get_user_data(target_id)
                level = data.get("level", 1) if data else 1
                results.append(
                    f"⚠️ 用户已被拉黑（本地）\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"ID: {target_id}\n"
                    f"等级：{LEVEL_EMOJIS.get(level, '⚪')} {level} ({LEVEL_NAMES.get(level, '未知')})\n"
                    f"原因：{data.get('reason', '未知') if data else '未知'}\n"
                    f"添加时间：{data.get('added_at', '未知') if data else '未知'}\n"
                    f"添加者：{data.get('added_by', '未知') if data else '未知'}"
                )
            else:
                result = await self.api.check_blacklist(target_id, "user")
                if not result.get("success"):
                    results.append(f"❌ 用户查询失败：{result.get('message', '未知错误')}")
                elif result.get("in_blacklist"):
                    data = result.get("data", {})
                    results.append(
                        f"⚠️ 用户已被拉黑（实时）\n"
                        f"ID: {target_id}\n"
                        f"等级：{data.get('level', 1)}\n"
                        f"⚠️ 不在本地缓存，正在同步..."
                    )
                    # 触发增量同步
                    asyncio.create_task(self.service.sync_blacklist())
                else:
                    results.append(f"✅ 用户 {target_id} 未被拉黑")
            
            # 查询群组
            if self.service.is_group_blacklisted(target_id):
                data = self.service.get_group_data(target_id)
                level = data.get("level", 1) if data else 1
                results.append(
                    f"⚠️ 群组已被拉黑（本地）\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"ID: {target_id}\n"
                    f"等级：{LEVEL_EMOJIS.get(level, '⚪')} {level} ({LEVEL_NAMES.get(level, '未知')})\n"
                    f"原因：{data.get('reason', '未知') if data else '未知'}\n"
                    f"添加时间：{data.get('added_at', '未知') if data else '未知'}\n"
                    f"添加者：{data.get('added_by', '未知') if data else '未知'}"
                )
            else:
                result = await self.api.check_blacklist(target_id, "group")
                if not result.get("success"):
                    results.append(f"❌ 群组查询失败：{result.get('message', '未知错误')}")
                elif result.get("in_blacklist"):
                    data = result.get("data", {})
                    results.append(
                        f"⚠️ 群组已被拉黑（实时）\n"
                        f"ID: {target_id}\n"
                        f"等级：{data.get('level', 1)}\n"
                        f"⚠️ 不在本地缓存，正在同步..."
                    )
                    # 触发增量同步
                    asyncio.create_task(self.service.sync_blacklist())
                else:
                    results.append(f"✅ 群组 {target_id} 未被拉黑")
            
            # 返回结果
            yield event.plain_result("\n\n".join(results))
            return
        
        # 指定了类型，按原逻辑处理
        if user_type not in ("user", "group"):
            yield event.plain_result("❌ 参数错误：user_type 必须是'user'或'group'")
            return
        
        target_id = str(target or event.get_sender_id())
        type_name = "群组" if user_type == "group" else "用户"
        
        # 本地检查
        if user_type == "group":
            is_blacklisted = self.service.is_group_blacklisted(target_id)
            data = self.service.get_group_data(target_id) if is_blacklisted else None
        else:
            is_blacklisted = self.service.is_user_blacklisted(target_id)
            data = self.service.get_user_data(target_id) if is_blacklisted else None
        
        if is_blacklisted:
            level = data.get("level", 1) if data else 1
            yield event.plain_result(
                f"⚠️ {type_name}已被拉黑（本地）\n"
                f"━━━━━━━━━━━━━━\n"
                f"ID: {target_id}\n"
                f"等级：{LEVEL_EMOJIS.get(level, '⚪')} {level} ({LEVEL_NAMES.get(level, '未知')})\n"
                f"原因：{data.get('reason', '未知') if data else '未知'}\n"
                f"添加时间：{data.get('added_at', '未知') if data else '未知'}\n"
                f"添加者：{data.get('added_by', '未知') if data else '未知'}"
            )
            return
        
        # API 实时检查
        result = await self.api.check_blacklist(target_id, user_type)
        
        # 先检查 API 调用是否成功
        if not result.get("success"):
            error_msg = result.get('message', '未知错误')
            yield event.plain_result(f"❌ 查询失败：{error_msg}")
            return
        
        # API 调用成功，检查是否在黑名单中
        if result.get("in_blacklist"):
            data = result.get("data", {})
            level = data.get('level', 1)
            yield event.plain_result(
                f"⚠️ {type_name}已被拉黑（实时）\n"
                f"━━━━━━━━━━━━━━\n"
                f"ID: {target_id}\n"
                f"等级：{LEVEL_EMOJIS.get(level, '⚪')} {level} ({LEVEL_NAMES.get(level, '未知')})\n"
                f"原因：{data.get('reason', '未知')}\n"
                f"添加时间：{data.get('added_at', '未知')}\n"
                f"添加者：{data.get('added_by', '未知')}\n"
                f"⚠️ 不在本地缓存，正在同步..."
            )
            # 触发增量同步
            asyncio.create_task(self.service.sync_blacklist())
        else:
            yield event.plain_result(f"✅ {type_name} {target_id} 未被拉黑")
    
    @filter.command("bl_add")
    @require_op
    @require_token
    async def cmd_add(self, event: AstrMessageEvent, user_id: str = None, reason: str = None, level: int = 1, user_type: str = "user"):
        """添加到黑名单 - 支持用户和群聊，默认用户"""
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
        
        # 检查 user_type 参数
        if user_type not in ("user", "group"):
            yield event.plain_result("❌ 参数错误：user_type必须是'user'或'group'")
            return
        
        # 等级范围统一为1-4
        if not 1 <= level <= 4:
            yield event.plain_result("❌ 参数错误：level必须在1-4之间")
            return
        
        # 等级4需要在管理面板操作
        if level == 4:
            yield event.plain_result("❌ 等级4需要在管理面板操作: https://云黑.皮梦.wtf/admin")
            return
        
        result = await self.api.add_to_blacklist(user_id, user_type, reason, level)
        
        if result.get("success"):
            await self.service.sync_blacklist()
            
            type_name = "用户" if user_type == "user" else "群组"
            yield event.plain_result(
                f"✅ 已添加到黑名单\n"
                f"类型: {type_name}\n"
                f"ID: {user_id}\n"
                f"等级: {LEVEL_EMOJIS.get(level, '⚪')} {level}\n"
                f"原因: {reason}"
            )
        else:
            yield event.plain_result(f"❌ 添加失败: {result.get('message', '未知错误')}")
    
    @filter.command("bl_remove")
    @require_op
    @require_token
    async def cmd_remove(self, event: AstrMessageEvent, user_id: str = None, reason: str = "", user_type: str = "user"):
        """从黑名单移除 - 支持用户和群聊，默认用户"""
        if not user_id:
            yield event.plain_result("❌ 参数错误：需要提供user_id")
            return
        
        user_id = str(user_id)
        
        if not user_id.isdigit():
            yield event.plain_result("❌ 参数错误：user_id必须是数字")
            return
        
        # 检查 user_type 参数
        if user_type not in ("user", "group"):
            yield event.plain_result("❌ 参数错误：user_type必须是'user'或'group'")
            return
        
        result = await self.api.remove_from_blacklist(user_id, user_type, reason or "管理员移除")
        
        if result.get("success"):
            # 根据类型从对应的黑名单中移除
            if user_type == "user":
                self.service.remove_user(user_id)
                self.cache.remove_private_warn(user_id)
            else:
                self.service.remove_group(user_id)
            
            type_name = "用户" if user_type == "user" else "群组"
            yield event.plain_result(f"✅ 已从黑名单移除: {type_name} {user_id}")
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
            for uid, data in self.service.user_blacklist.items()
        ] + [
            (uid, data, "群组") 
            for uid, data in self.service.group_blacklist.items()
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
                "/bl_add <ID> <原因> [等级] [user/group] - 添加黑名单",
                "/bl_remove <ID> [原因] [user/group] - 移除黑名单",
                "/bl_list [页码] - 查看列表",
            ])
        
        lines.extend([
            "━━━━━━━━━━━━━━",
            "📝 参数说明",
            "━━━━━━━━━━━━━━",
            "• ID: QQ号或群号",
            "• user/group: 查询/操作类型，默认user",
            "• 等级: 1-轻微 2-一般 3-平台 4-严重（等级4需面板操作）",
            "━━━━━━━━━━━━━━",
            "🌐 申诉: https://云黑.皮梦.wtf",
            f"👤 身份: {'管理员' if is_op else '用户'}",
        ])
        
        yield event.plain_result("\n".join(lines))