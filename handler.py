"""事件处理模块 - 处理消息拦截、踢人权限检查等事件"""

from datetime import datetime
from typing import Optional, Set

from astrbot.api.event import AstrMessageEvent

from .service import BlacklistService
from .cache import BlacklistCache


# 尝试导入 AiocqhttpMessageEvent 用于权限检查
try:
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
except ImportError:
    AiocqhttpMessageEvent = None


class EventHandler:
    """事件处理器"""
    
    def __init__(self, service: BlacklistService, cache: BlacklistCache, enable_auto_kick: bool, logger):
        self.service = service
        self.cache = cache
        self.enable_auto_kick = enable_auto_kick
        self.logger = logger
        
        # Bot加入群聊关键词
        self.BOT_JOIN_KEYWORDS = ["邀请", "加入了群聊", "加入了", "加入群", "加群"]
        
        # 避免重复退群的集合
        self.quit_groups: Set[str] = set()
    
    async def handle_message(self, event: AstrMessageEvent, context) -> Optional[str]:
        """处理消息事件 - 返回需要发送的消息，或None表示不发送"""
        user_id = str(event.get_sender_id())
        
        # 判断是否为群聊
        is_group = hasattr(event, 'get_group_id') and event.get_group_id() is not None
        
        # 检查用户黑名单
        if self.service.is_user_blacklisted(user_id):
            user_data = self.service.get_user_data(user_id)
            level = user_data.get("level", 1) if user_data else 1
            
            # 判断是否主动唤醒bot（检查是否@bot或使用唤醒词）
            is_wake_up = False
            if hasattr(event, 'is_wake_up'):
                is_wake_up = event.is_wake_up()
            
            # 私聊模式：全部拦截，仅主动唤醒时每天提醒一次
            if not is_group:
                now = datetime.now()
                last_warn = self.cache.get_private_warn_time(user_id)
                
                # 检查是否需要提醒（仅主动唤醒时）
                should_warn = False
                if is_wake_up:
                    # 检查是否在同一天提醒过
                    if last_warn is None or last_warn.date() != now.date():
                        # 首次提醒或新的一天，提醒
                        should_warn = True
                        self.cache.set_private_warn_time(user_id, now)
                        self.logger.info(f"Private Warn | User: {user_id} | Level: {level}")
                
                if should_warn:
                    # 发送提醒
                    return (
                        f"⚠️ 您已被列入云黑名单。\n"
                        f"━━━━━━━━━━━━━━\n"
                        f"违规等级: {level}\n"
                        f"原因: {user_data.get('reason', '未知') if user_data else '未知'}\n"
                        f"━━━━━━━━━━━━━━\n"
                        f"详情与申诉: https://云黑.皮梦.wtf"
                    )
                
                # 停止事件传播，拦截消息
                event.stop_event()
                return None
        
        # 检查群组黑名单和踢人逻辑
        if is_group:
            group_id = str(event.get_group_id())
            
            # 检查群组是否在黑名单中（直接退群，不检查等级）
            if self.service.is_group_blacklisted(group_id):
                # 检查是否已经退过群
                if group_id in self.quit_groups:
                    self.logger.debug(f"Already quit group, ignoring | Group: {group_id}")
                    event.stop_event()
                    return None
                
                group_data = self.service.get_group_data(group_id)
                group_level = group_data.get("level", 1) if group_data else 1
                
                # Bot在黑名单群组中，自动退群
                self.logger.info(f"Bot in Blacklisted Group | Group: {group_id} | Level: {group_level}")
                
                # 标记为已退群
                self.quit_groups.add(group_id)
                
                # 尝试退群
                try:
                    if hasattr(context, 'quit_group'):
                        await context.quit_group(group_id)
                        self.logger.info(f"Bot Quit Group | Group: {group_id}")
                except Exception as e:
                    self.logger.error(f"Quit Group Failed | Group: {group_id} | Error: {e}")
                    # 退群失败，从集合中移除，允许重试
                    self.quit_groups.discard(group_id)
                
                # 停止事件传播
                event.stop_event()
                return None
            
            # 检查群组中的黑名单用户（等级≥3 时踢出）
            if self.service.is_user_blacklisted(user_id) and self.enable_auto_kick:
                user_data = self.service.get_user_data(user_id)
                user_level = user_data.get("level", 1) if user_data else 1
                
                # 仅等级≥3 时踢出（云黑有 Bot 白名单，无需检查是否踢自己）
                if user_level >= 3:
                    # 检查 Bot 和对方的管理员权限
                    can_kick = await self._check_kick_permission(event, group_id, user_id)
                    
                    if can_kick:
                        # 尝试踢出黑名单用户
                        try:
                            # 优先使用 bot 方法踢人
                            if hasattr(event, 'bot') and hasattr(event.bot, 'set_group_kick'):
                                await event.bot.set_group_kick(
                                    group_id=int(group_id),
                                    user_id=int(user_id)
                                )
                                self.logger.info(f"Kicked blacklisted user | User: {user_id} | Level: {user_level} | Group: {group_id}")
                                return (
                                    f"⚠️ 已踢出云黑用户\n"
                                    f"━━━━━━━━━━━━━━\n"
                                    f"用户：{user_id}\n"
                                    f"等级：{user_level}\n"
                                    f"原因：{user_data.get('reason', '未知') if user_data else '未知'}"
                                )
                            elif hasattr(context, 'kick_group_member'):
                                await context.kick_group_member(group_id, user_id)
                                self.logger.info(f"Kicked blacklisted user | User: {user_id} | Level: {user_level} | Group: {group_id}")
                                return (
                                    f"⚠️ 已踢出云黑用户\n"
                                    f"━━━━━━━━━━━━━━\n"
                                    f"用户：{user_id}\n"
                                    f"等级：{user_level}\n"
                                    f"原因：{user_data.get('reason', '未知') if user_data else '未知'}"
                                )
                        except Exception as e:
                            self.logger.error(f"Kick failed | User: {user_id} | Group: {group_id} | Error: {e}")
                    
                    # 停止事件传播，拦截消息
                    event.stop_event()
                    return None
        
        return None
    
    async def handle_member_join(self, event: AstrMessageEvent, context) -> bool:
        """处理成员加入群组事件 - 返回True表示处理了事件"""
        group_id = str(event.get_group_id())
        message_str = getattr(event, 'message_str', '') or ''
        
        # 检查是否为Bot加入事件
        if not any(kw in message_str.lower() for kw in self.BOT_JOIN_KEYWORDS):
            return False
        
        # 检查群组是否在黑名单中
        if not self.service.is_group_blacklisted(group_id):
            return False
        
        # 检查是否已经退过群
        if group_id in self.quit_groups:
            self.logger.debug(f"Already quit group, ignoring join event | Group: {group_id}")
            return True
        
        group_data = self.service.get_group_data(group_id)
        level = group_data.get("level", 1) if group_data else 1
        
        self.logger.info(f"Bot joined blacklisted group | Group: {group_id} | Level: {level}")
        
        # 标记为已退群
        self.quit_groups.add(group_id)
        
        # 尝试退群
        try:
            if hasattr(context, 'quit_group'):
                await context.quit_group(group_id)
                self.logger.info(f"Bot quit group | Group: {group_id}")
                return True
        except Exception as e:
            self.logger.error(f"Quit group failed | Group: {group_id} | Error: {e}")
            # 退群失败，从集合中移除，允许重试
            self.quit_groups.discard(group_id)
        
        return False
    
    async def _check_kick_permission(self, event: AstrMessageEvent, group_id: str, user_id: str) -> bool:
        """检查 Bot 是否有权限踢出对方
        
        返回 True 表示可以踢出，False 表示不能踢出
        检查逻辑：
        1. Bot 必须是管理员或群主
        2. 对方不能是管理员或群主
        """
        # 检查平台是否为 aiocqhttp
        if event.get_platform_name() != "aiocqhttp":
            # 非 aiocqhttp 平台，默认不能踢出（安全第一）
            self.logger.warning(f"非 aiocqhttp 平台，无法踢人 | Platform: {event.get_platform_name()}")
            return False
        
        # 检查是否为 AiocqhttpMessageEvent 类型
        if AiocqhttpMessageEvent is None or not isinstance(event, AiocqhttpMessageEvent):
            # 类型不匹配，默认不能踢出（安全第一）
            self.logger.warning(f"非 AiocqhttpMessageEvent 类型，无法踢人 | Type: {type(event)}")
            return False
        
        try:
            client = event.bot
            # 修复：从 event.message_obj.self_id 获取 Bot ID
            bot_id = None
            
            # 优先从 event.message_obj.self_id 获取
            if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'self_id'):
                bot_id = event.message_obj.self_id
            
            # 如果还是为空，尝试从 event.self_id 获取（兼容旧版本）
            if not bot_id:
                bot_id = getattr(event, 'self_id', None)
            
            # 如果还是为空，尝试从 context 获取
            if not bot_id and hasattr(self, 'context'):
                bot_id = self.context.get_bot_id() if hasattr(self.context, 'get_bot_id') else None
            
            # 如果还是为空，尝试从 event.bot.self_id 获取
            if not bot_id and hasattr(event, 'bot'):
                bot_id = getattr(event.bot, 'self_id', None)
            
            # 如果 bot_id 为空，直接返回 False
            if not bot_id:
                self.logger.warning(f"无法获取 Bot ID | Group: {group_id} | Event type: {type(event)}")
                return False
            
            # 获取 Bot 和对方的群成员信息
            bot_info = await client.api.call_action(
                'get_group_member_info',
                user_id=int(bot_id),
                group_id=int(group_id),
                no_cache=True
            )
            user_info = await client.api.call_action(
                'get_group_member_info',
                user_id=int(user_id),
                group_id=int(group_id),
                no_cache=True
            )
            
            bot_role = bot_info.get("role", "member")
            user_role = user_info.get("role", "member")
            
            # Bot 必须是管理员或群主
            if bot_role not in ["admin", "owner"]:
                self.logger.warning(f"Bot 不是管理员，无法踢人 | Bot: {bot_id} | Role: {bot_role} | Group: {group_id}")
                return False
            
            # 对方不能是管理员或群主
            if user_role in ["admin", "owner"]:
                self.logger.warning(f"对方是管理员/群主，无法踢出 | User: {user_id} | Role: {user_role} | Group: {group_id}")
                return False
            
            # 可以踢出
            return True
            
        except (ValueError, TypeError, Exception) as e:
            self.logger.warning(f"群成员信息获取失败：{str(e)} | Group: {group_id} | User: {user_id}")
            return False