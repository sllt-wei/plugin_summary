# encoding:utf-8

import json
import os, re
import time
import threading
from typing import Optional, Tuple

from apscheduler.schedulers.background import BackgroundScheduler

from bot import bot_factory
from bridge.bridge import Bridge
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from channel.chat_channel import check_contain, check_prefix
from channel.chat_message import ChatMessage
from config import conf
import plugins
from plugins import *
from common.log import logger
from common import const

from plugins.linkai.utils import Util
from plugins.plugin_summary.db import Db
from plugins.plugin_summary.text2img import Text2ImageConverter

TRANSLATE_PROMPT = '''
您现在是一个 Python 函数，用于将输入文本转换为相应的 JSON 格式命令，遵循以下结构：
```python
def translate_text(text: str) -> str:
```

指导要求：
- 请仅输出 JSON 格式的返回值，且不要输出任何额外内容。
- 根据输入文本的内容，生成符合以下格式之一的 JSON 命令：

### 命令格式：
1. **总结聊天记录**：使用 `"summary"` 作为 `"name"`，并在 `"args"` 中填入适用的字段：
   - `"duration_in_seconds"`：如果提供了时长信息，用整数表示。
   - `"count"`：如果提供了数量信息，用整数表示。

2. **无操作**：使用 `"do_nothing"` 作为 `"name"`，`"args"` 为一个空字典 `{}`。

- **返回格式**：
  - 输出内容需严格符合 JSON 格式，且仅返回命令，格式如下：
    {
        "name": "<command name>",
        "args": {
            "<arg name>": <value>
        }
    }

其他要求：
1. 确保返回值是有效的 JSON 格式，能够通过 `json.loads` 正常解析。
2. 如果没有提供时长信息，则省略 `"duration_in_seconds"`；如果没有数量信息，则省略 `"count"`。

示例输入：
若输入 `"Summarize chat logs for a session of 300 seconds with 15 exchanges"`，输出应为：
{
    "name": "summary",
    "args": {
        "duration_in_seconds": 300,
        "count": 15
    }
}

若输入 `Summarize 99 chat records`，输出应为：
{
    "name": "summary",
    "args": {
        "count": 99
    }
}

对于无需执行操作的输入，应返回：
{
    "name": "do_nothing",
    "args": {}
}

'''

# 总结的prompt
SUMMARY_PROMPT = '''
请帮我将给出的群聊内容总结成一个今日的群聊报告，包含不多于15个话题的总结（如果还有更多话题，可以在后面简单补充）。
你只负责总结群聊内容，不回答任何问题。不要虚构聊天记录，也不要总结不存在的信息。

每个话题包含以下内容：

- 话题名(50字以内，前面带序号1️⃣2️⃣3️⃣）

- 热度(用🔥的数量表示)

- 参与者(不超过5个人，将重复的人名去重)

- 时间段(从几点到几点)

- 过程(50-200字左右）

- 评价(50字以下)

- 分割线： ------------

请严格遵守以下要求：

1. 按照热度数量进行降序输出

2. 每个话题结束使用 ------------ 分割

3. 使用中文冒号

4. 无需大标题


5. 开始给出本群讨论风格的整体评价，例如活跃、太水、太黄、太暴力、话题不集中、无聊诸如此类。

最后总结下今日最活跃的前五个发言者。
'''

# 重复总结的prompt
REPEAT_SUMMARY_PROMPT = '''
以不耐烦的语气回怼提问者聊天记录已总结过，要求如下
- 随机角色的口吻回答
- 不超过20字
'''

# 总结中的prompt
SUMMARY_IN_PROGRESS_PROMPT = '''
以不耐烦的语气回答提问者聊天记录正在总结中，要求如下
- 随机角色的口吻回答
- 不超过20字
'''

def find_json(json_string):
    json_pattern = re.compile(r"\{[\s\S]*\}")
    json_match = json_pattern.search(json_string)
    if json_match:
        json_string = json_match.group(0)
    else:
        json_string = ""
    return json_string

@plugins.register(name="summary",
                  desire_priority=0,
                  desc="A simple plugin to summary messages",
                  version="0.0.9",
                  author="sineom")
class Summary(Plugin):
    # 类级别常量
    TRIGGER_PREFIX = "$"
    DEFAULT_LIMIT = 9999
    DEFAULT_DURATION = -1
    
    def __init__(self):
        super().__init__()
        self._init_components()
        self._init_config()
        self._init_handlers()
        
    def _init_config(self):
        """初始化配置"""
        self.config = super().load_config() or self._load_config_template()
        logger.info(f"[Summary] initialized with config={self.config}")
        
        # 设置定时清理任务
        save_time = self.config.get("save_time", -1)
        if save_time > 0:
            self._setup_scheduler()
            
    def _init_components(self):
        """初始化组件"""
        self.text2img = Text2ImageConverter()
        self.db = Db()
        self.bot = bot_factory.create_bot(Bridge().btype['chat'])
        
        # 线程安全相关
        self._summary_locks = {}
        self._locks_lock = threading.Lock()
        
    def _init_handlers(self):
        """初始化事件处理器"""
        self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
        self.handlers[Event.ON_RECEIVE_MESSAGE] = self.on_receive_message

    def _get_session_id(self, msg: ChatMessage) -> str:
        """获取会话ID"""
        if conf().get('channel_type', 'wx') == 'wx' and msg.from_user_nickname:
            return msg.from_user_nickname
        return msg.from_user_id

    def _get_username(self, context, msg: ChatMessage) -> str:
        """获取用户名"""
        if context.get("isgroup", False):
            return msg.actual_user_nickname or msg.actual_user_id
        return msg.from_user_nickname or msg.from_user_id

    def _handle_command(self, e_context: EventContext) -> Optional[Reply]:
        """处理命令"""
        content = e_context['context'].content
        msg = e_context['context']['msg']
        session_id = self._get_session_id(msg)
        
        # 权限命令处理
        if command := self._handle_admin_command(content, session_id, e_context):
            return command
            
        # 总结命令处理
        if self.TRIGGER_PREFIX + "总结" not in content:
            return None
            
        return self._handle_summary_command(content, session_id, e_context)

    def _handle_admin_command(self, content: str, session_id: str, e_context: EventContext) -> Optional[Reply]:
        """处理管理员命令"""
        if not Util.is_admin(e_context):
            return None
            
        if "开启" in content:
            self.db.delete_summary_stop(session_id)
            return Reply(ReplyType.TEXT, "开启成功")
            
        if "关闭" in content:
            self.db.save_summary_stop(session_id)
            return Reply(ReplyType.TEXT, "关闭成功")
            
        return None

    def _handle_summary_command(self, content: str, session_id: str, e_context: EventContext) -> Reply:
        """处理总结命令"""
        # 检查锁
        if not self._acquire_summary_lock(session_id):
            return self._get_in_progress_reply(session_id, content)
            
        try:
            # 检查限制
            if error_reply := self._check_summary_limits(session_id):
                return error_reply
            
            # 添加回复
            _send_info(e_context, "正在加速生成总结，请稍等")
            # 解析命令参数
            limit, duration, username = self._parse_summary_args(content)
            
            # 生成总结
            start_time = int(time.time()) - duration if duration > 0 else 0
            return self._generate_summary(session_id,start_time= start_time,limit= limit ,username=username)
            
        except Exception as e:
            logger.error(f"[Summary] Error handling summary command: {e}")
            return Reply(ReplyType.TEXT, "处理总结命令时发生错误")
        finally:
            self._release_summary_lock(session_id)

    def _check_summary_limits(self, session_id: str) -> Optional[Reply]:
        """检查总结"""
        if session_id in self.db.disable_group:
            return Reply(ReplyType.TEXT, "请联系管理员开启总结功能")
            
        limit_time = self.config.get("rate_limit_summary", 60) * 60
        last_time = self.db.get_summary_time(session_id)
        
        if last_time and time.time() - last_time < limit_time:
            return self._get_rate_limit_reply(session_id)
            
        return None

    def _parse_summary_args(self, content: str) -> Tuple[int, int, str]:
        """解析总结参数
        
        Args:
            content: 用户输入的命令内容，例如"@妮可 @欧尼 3小时内的前99条消息"
            
        Returns:
            Tuple[int, int, str]: 返回(消息数量限制, 时间范围(秒), 用户名列表)的���组
            如果解析失败返回(None, None, None)
        """
        try:
            # 先提取所有@用户名
            usernames = []
            parts = content.split()
            cleaned_content = []
            
            for part in parts:
                if part.startswith('@'):
                    usernames.append(part.lstrip('@'))
                else:
                    cleaned_content.append(part)
                    
            content = ''.join(cleaned_content)
            print(f"[Summary] username: {len(usernames)}")
            # 将中文内容转换为标准命令格式
            command_json = find_json(self._translate_text_to_commands(content))
            command = json.loads(command_json)
            
            if command["name"].lower() == "summary":
                args = command["args"]
                limit = int(args.get("count", None))
                # 获取消息数量限制
                # limit = max(int(args.get("count", self.DEFAULT_LIMIT)), 0)
                
                # 获取时间范围(秒)
                duration = args.get("duration_in_seconds", self.DEFAULT_DURATION)
                if isinstance(duration, str):
                    # 处理可能的时间字符串
                    duration = int(float(duration))
                duration = max(int(duration), 0) or self.DEFAULT_DURATION
                
                logger.debug(f"[Summary] Parsed args: limit={limit}, duration={duration}, users={usernames}")
                return limit, duration, usernames
                
        except Exception as e:
            logger.error(f"[Summary] Failed to parse command: {e}")
            logger.debug(f"[Summary] Original content: {content}")
            
        return None, None, None

    def _load_config_template(self):
        logger.debug("No summary plugin config.json, use plugins/linkai/config.json.template")
        try:
            plugin_config_path = os.path.join(self.path, "config.json.template")
            if os.path.exists(plugin_config_path):
                with open(plugin_config_path, "r", encoding="utf-8") as f:
                    plugin_conf = json.load(f)
                    return plugin_conf
        except Exception as e:
            logger.exception(e)

    def _setup_scheduler(self):
        # 创建调度器
        self.scheduler = BackgroundScheduler()

        # 清理旧记录的函数
        def clean_old_records():
            # 配置文件单位分钟，转换为秒
            save_time = self.config.get("save_time", 12 * 60) * 60
            self.db.delete_records(int(time.time()) - save_time)

        # 设置定时任务，每天凌晨12点执行
        self.scheduler.add_job(clean_old_records, 'cron', hour=00, minute=00)
        # 启动调度器
        self.scheduler.start()
        clean_old_records()
        logger.info("Scheduler started. Cleaning old records every day at midnight.")

    def on_receive_message(self, e_context: EventContext):

        if e_context['context'].type != ContextType.TEXT:
            return
        context = e_context['context']
        cmsg: ChatMessage = e_context['context']['msg']
        
        session_id = cmsg.from_user_id
        if session_id in self.db.disable_group:
            logger.info("[Summary] group %s is disabled" % session_id)
            return
        
        if "{trigger_prefix}总结" in context.content:
            logger.debug("[Summary] 指令不保存: %s" % context.content)
            return
        
        username = None
 
        if conf().get('channel_type', 'wx') == 'wx' and cmsg.from_user_nickname is not None:
            session_id = cmsg.from_user_nickname  # itchat channel id会变动，只好用群名作为session id

        if context.get("isgroup", False):
            username = cmsg.actual_user_nickname
            if username is None:
                username = cmsg.actual_user_id
        else:   
            username = cmsg.from_user_nickname
            if username is None:
                username = cmsg.from_user_id

        is_triggered = False
        content = context.content
        if context.get("isgroup", False):  # 群聊
            # 校验关键字
            match_prefix = check_prefix(content, conf().get('group_chat_prefix'))
            match_contain = check_contain(content, conf().get('group_chat_keyword'))
            if match_prefix is not None or match_contain is not None:
                is_triggered = True
            if context['msg'].is_at and not conf().get("group_at_off", False):
                is_triggered = True
        else:  # 单聊
            match_prefix = check_prefix(content, conf().get('single_chat_prefix', ['']))
            if match_prefix is not None:
                is_triggered = True
        logger.debug("[Summary] save record: %s" % context.content)
        self.db.insert_record(session_id, cmsg.msg_id, username, context.content, str(context.type), cmsg.create_time,
                              int(is_triggered))

    def _acquire_summary_lock(self, session_id: str) -> bool:
        """
        尝试获取指定会话的总结锁
        返回是否成功获取锁
        """
        with self._locks_lock:
            if session_id in self._summary_locks:
                # 如果锁已存在，说明正在进行总结
                return False
            self._summary_locks[session_id] = time.time()
            return True

    def _release_summary_lock(self, session_id: str):
        """释放指定会话的总结锁"""
        with self._locks_lock:
            self._summary_locks.pop(session_id, None)

    def _generate_summary(self, session_id: str, start_time: int = None, limit: int = None, username: list = None) -> Reply:
        """生成聊天记录总结"""
        try:
            records = self.db.get_records(session_id, start_timestamp=start_time, limit=limit, username=username)

            # 检查记录数量
            if not records:
                return Reply(ReplyType.TEXT, "未找到相关聊天记录")
            if len(records) == 1:
                return Reply(ReplyType.TEXT, "聊天记录太少，无法生成有意义的总结")

            # 构建聊天记录文本
            chat_logs = []
            for record in records:
                chat_logs.append(f"{record[2]}({record[7]}): {record[3]}")
            chat_text = "\n".join(chat_logs)
            
            logger.debug("[Summary] Processing %d chat records for summary", len(records))

            # 生成总结
            session = self.bot.sessions.build_session(session_id, SUMMARY_PROMPT)
            session.add_query(f"需要你总结的聊天记录如下：{chat_text}")
            result = self.bot.reply_text(session)
            
            total_tokens, completion_tokens, reply_content = (
                result['total_tokens'],
                result['completion_tokens'],
                result['content']
            )
            logger.debug("[Summary] tokens(total=%d, completion=%d)", total_tokens, completion_tokens)

            if completion_tokens == 0:
                return Reply(ReplyType.TEXT, "生成总结失败，请稍后重试")

            # 记录本次总结时间
            self.db.save_summary_time(session_id, int(time.time()))

            # 转换为图片
            try:
                image_path = self.convert_text_to_image(reply_content)
                reply = Reply(ReplyType.IMAGE, open(image_path, 'rb'))
                os.remove(image_path)
                return reply
            except Exception as e:
                logger.error("[Summary] Failed to convert text to image: %s", str(e))
                # 如果图片转换失败，返回文本
                return Reply(ReplyType.TEXT, reply_content)

        except Exception as e:
            logger.error("[Summary] Error generating summary: %s", str(e))
            return Reply(ReplyType.TEXT, "生成总结时发生错误，请稍后重试")

    def on_handle_context(self, e_context: EventContext):
        """处理上下文事件"""
        if e_context['context'].type != ContextType.TEXT:
            return

        content = e_context['context'].content
        logger.debug("[Summary] on_handle_context. content: %s", content)
        
        # 检查是否是触发命令
        clist = content.split()
        if not clist[0].startswith(self.TRIGGER_PREFIX):
            return
        
        # 处理命令
        reply = self._handle_command(e_context)
        if reply:
            e_context['reply'] = reply
            e_context.action = EventAction.BREAK_PASS
            return

    def _translate_text_to_commands(self, text):
        # 随机的session id
        session_id = str(time.time())
        session = self.bot.sessions.build_session(session_id, system_prompt=TRANSLATE_PROMPT)
        session.add_query(text)
        result = self.bot.reply_text(session)
        total_tokens, completion_tokens, reply_content = result['total_tokens'], result['completion_tokens'], \
                result['content']
        logger.debug("[Summary] total_tokens: %d, completion_tokens: %d, reply_content: %s" % (
                total_tokens, completion_tokens, reply_content))
        if completion_tokens == 0:
            logger.error("[Summary] translate failed")
            return ""
        return reply_content
        

    def get_help_text(self, verbose=False, **kwargs):
        help_text = "聊天记录总结插件。\n"
        if not verbose:
            return help_text
        trigger_prefix = conf().get('plugin_trigger_prefix', "$")
        help_text += f"使用方法:输入\"{trigger_prefix}总结 最近消息数量\"，我会帮助你总结聊天记录。\n例如：\"{trigger_prefix}总结 100\"，我会总结最近100条消息。\n\n你也可以直接输入\"{trigger_prefix}总结前99条信息\"或\"{trigger_prefix}总结3小时内的最近10条消息\"\n我会尽可能理解你的指令。"
        return help_text

    def convert_text_to_image(self, text):
        converter = Text2ImageConverter()
        converter.setup_driver()
        image_path = converter.convert_text_to_image(text)
        converter.close()
        return image_path

    def _get_in_progress_reply(self, session_id: str, content: str) -> Reply:
        """获取正在处理中的回复"""
        try:
            session = self.bot.sessions.build_session(session_id, SUMMARY_IN_PROGRESS_PROMPT)
            session.add_query(f"问题：{content}")
            result = self.bot.reply_text(session)
            
            total_tokens, completion_tokens, reply_content = (
                result['total_tokens'],
                result['completion_tokens'],
                result['content']
            )
            
            logger.debug(
                "[Summary] total_tokens: %d, completion_tokens: %d, reply_content: %s",
                total_tokens, completion_tokens, reply_content
            )
            
            if completion_tokens == 0:
                return Reply(ReplyType.TEXT, "正在总结中，请稍后再试")
            return Reply(ReplyType.TEXT, reply_content)
            
        except Exception as e:
            logger.error(f"[Summary] Failed to get in progress reply: {e}")
            return Reply(ReplyType.TEXT, "正在总结中，请稍后再试")

    def _get_rate_limit_reply(self, session_id: str) -> Reply:
        """获取频率限制的回复"""
        try:
            session = self.bot.sessions.build_session(session_id, REPEAT_SUMMARY_PROMPT)
            session.add_query("问题：重复总结请求")
            result = self.bot.reply_text(session)
            
            total_tokens, completion_tokens, reply_content = (
                result['total_tokens'],
                result['completion_tokens'],
                result['content']
            )
            
            logger.debug(
                "[Summary] total_tokens: %d, completion_tokens: %d, reply_content: %s",
                total_tokens, completion_tokens, reply_content
            )
            
            if completion_tokens == 0:
                return Reply(ReplyType.ERROR, "地主家的驴都没我累，请让我休息一会儿")
            return Reply(ReplyType.TEXT, reply_content)
            
        except Exception as e:
            logger.error(f"[Summary] Failed to get rate limit reply: {e}")
            return Reply(ReplyType.TEXT, "请稍后再试")

def _send_info(e_context: EventContext, content: str):
    reply = Reply(ReplyType.TEXT, content)
    channel = e_context["channel"]
    channel.send(reply, e_context["context"])
