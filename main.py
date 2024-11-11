# encoding:utf-8

import json
import os, re
import time

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

from plugins.plugin_summary.db import Db
from plugins.plugin_summary.text2img import Text2ImageConverter

TRANSLATE_PROMPT = '''
You are now the following python function: 
```# {{translate text to commands}}"
        def translate_text(text: str) -> str:
```
Only respond with your `return` value, Don't reply anything else.

Commands:
{{Summary chat logs}}: "summary", args: {{("duration_in_seconds"): <integer>, ("count"): <integer>}}
{{Do Nothing}}:"do_nothing",  args:  {{}}

argument in brackets means optional argument.

You should only respond in JSON format as described below.
Response Format: 
{{
    "name": "command name", 
    "args": {{"arg name": "value"}}
}}
Ensure the response can be parsed by Python json.loads.

Input: {input}
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
                  version="0.0.4",
                  author="sineom")
class Summary(Plugin):
    def __init__(self):
        super().__init__()
        self.config = super().load_config()
        self.text2img = Text2ImageConverter()
        if not self.config:
            # 未加载到配置，使用模板中的配置
            self.config = self._load_config_template()
        logger.info(f"[summary] inited, config={self.config}")
        self.db = Db()
        save_time = self.config.get("save_time", -1)
        if save_time > 0:
            self._setup_scheduler()
        btype = Bridge().btype['chat']
        if btype not in [const.OPEN_AI, const.CHATGPT, const.CHATGPTONAZURE, const.LINKAI, const.MOONSHOT]:
            raise Exception("[Summary] init failed, not supported bot type")
        self.bot = bot_factory.create_bot(Bridge().btype['chat'])
        self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
        self.handlers[Event.ON_RECEIVE_MESSAGE] = self.on_receive_message
        logger.info("[Summary] inited")

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
        context = e_context['context']
        cmsg: ChatMessage = e_context['context']['msg']
        username = None
        session_id = cmsg.from_user_id
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

        self.db.insert_record(session_id, cmsg.msg_id, username, context.content, str(context.type), cmsg.create_time,
                              int(is_triggered))
        # logger.debug("[Summary] {}:{} ({})" .format(username, context.content, session_id))

    def _check_tokens(self, records, max_tokens=5200):
        query = ""
        for record in records[::-1]:
            username = record[2]
            content = record[3]
            is_triggered = record[6]
            if record[4] in [str(ContextType.IMAGE), str(ContextType.VOICE)]:
                content = f"[{record[4]}]"

            sentence = ""
            sentence += f'{username}' + ": \"" + content + "\""
            if is_triggered:
                sentence += " <T>"
            query += "\n\n" + sentence
        prompt = ("你是一位群聊机器人，需要对聊天记录进行简明扼要的总结，用列表的形式输出。\n聊天记录格式：["
                  "x]是emoji表情或者是对图片和声音文件的说明，消息最后出现<T>表示消息触发了群聊机器人的回复，内容通常是提问，若带有特殊符号如#和$"
                  "则是触发你无法感知的某个插件功能，聊天记录中不包含你对这类消息的回复，可降低这些消息的权重。请不要在回复中包含聊天记录格式中出现的符号。\n")

        firstmsg_id = records[0][1]
        session = self.bot.sessions.build_session(firstmsg_id, prompt)

        session.add_query("需要你总结的聊天记录如下：%s" % query)
        if session.calc_tokens() > max_tokens:
            # logger.debug("[Summary] summary failed, tokens: %d" % session.calc_tokens())
            return None
        return session

    def _split_messages_to_summarys(self, records, max_tokens_persession=3600, max_summarys=8):
        summarys = []
        count = 0
        self.bot.args["max_tokens"] = 400
        while len(records) > 0 and len(summarys) < max_summarys:
            session = self._check_tokens(records, max_tokens_persession)
            last = 0
            if session is None:
                left, right = 0, len(records)
                while left < right:
                    mid = (left + right) // 2
                    logger.debug("[Summary] left: %d, right: %d, mid: %d" % (left, right, mid))
                    session = self._check_tokens(records[:mid], max_tokens_persession)
                    if session is None:
                        right = mid - 1
                    else:
                        left = mid + 1
                session = self._check_tokens(records[:left - 1], max_tokens_persession)
                last = left
                logger.debug("[Summary] summary %d messages" % left)
            else:
                last = len(records)
                logger.debug("[Summary] summary all %d messages" % (len(records)))
            if session is None:
                logger.debug("[Summary] summary failed, session is None")
                break
            logger.debug("[Summary] session query: %s, prompt_tokens: %d" % (session.messages, session.calc_tokens()))
            result = self.bot.reply_text(session)
            total_tokens, completion_tokens, reply_content = result['total_tokens'], result['completion_tokens'], \
                result['content']
            logger.debug("[Summary] total_tokens: %d, completion_tokens: %d, reply_content: %s" % (
                total_tokens, completion_tokens, reply_content))
            if completion_tokens == 0:
                if len(summarys) == 0:
                    return count, reply_content
                else:
                    break
            summary = reply_content
            summarys.append(summary)
            records = records[last:]
            count += last
        return count, summarys

    def on_handle_context(self, e_context: EventContext):

        if e_context['context'].type != ContextType.TEXT:
            return

        content = e_context['context'].content
        logger.debug("[Summary] on_handle_context. content: %s" % content)
        trigger_prefix = conf().get('plugin_trigger_prefix', "$")
        clist = content.split()
        if clist[0].startswith(trigger_prefix):
            limit = 99
            duration = -1
            msg: ChatMessage = e_context['context']['msg']
            session_id = msg.from_user_id
            if conf().get('channel_type', 'wx') == 'wx' and msg.from_user_nickname is not None:
                session_id = msg.from_user_nickname  # itchat channel id会变动，只好用名字作为session id

            # 开启指令
            if "开启" in clist[0]:
                self.db.save_summary_stop(session_id)
                reply = Reply(ReplyType.TEXT, "开启成功")
                e_context['reply'] = reply
                e_context.action = EventAction.BREAK_PASS
                return

            # 关闭指令
            if "关闭" in clist[0]:
                self.db.delete_summary_stop(session_id)
                reply = Reply(ReplyType.TEXT, "关闭成功")
                e_context['reply'] = reply
                e_context.action = EventAction.BREAK_PASS
                return

            if "总结" in clist[0]:
                # 如果当前群聊在黑名单中，则不允许总结
                if session_id in self.db.disable_group:
                    logger.info("[Summary] summary stop")
                    reply = Reply(ReplyType.TEXT, "我不想总结了")
                    e_context['reply'] = reply
                    e_context.action = EventAction.BREAK_PASS
                    return

                limit_time = self.config.get("rate_limit_summary", 60) * 60
                last_time = self.db.get_summary_time(session_id)
                if last_time is not None and time.time() - last_time < limit_time:
                    logger.info("[Summary] rate limit")
                    reply = Reply(ReplyType.TEXT, "我有些累了，请稍后再试")
                    e_context['reply'] = reply
                    e_context.action = EventAction.BREAK_PASS
                    return
                flag = False
                if clist[0] == trigger_prefix + "总结":
                    flag = True
                    if len(clist) > 1:
                        try:
                            limit = int(clist[1])
                            logger.debug("[Summary] limit: %d" % limit)
                        except Exception as e:
                            flag = False
                if not flag:
                    text = content.split(trigger_prefix, maxsplit=1)[1]
                    try:
                        command_json = find_json(self._translate_text_to_commands(text))
                        command = json.loads(command_json)
                        name = command["name"]
                        if name.lower() == "summary":
                            limit = int(command["args"].get("count", 99))
                            if limit < 0:
                                limit = 299
                            duration = int(command["args"].get("duration_in_seconds", -1))
                            logger.debug("[Summary] limit: %d, duration: %d seconds" % (limit, duration))
                    except Exception as e:
                        logger.error("[Summary] translate failed: %s" % e)
                        return
            else:
                return

            start_time = int(time.time())
            if duration > 0:
                start_time = start_time - duration
            else:
                start_time = 0

            records = self.db.get_records(session_id, start_time, limit)
            for i in range(len(records)):
                record = list(records[i])
                content = record[3]
                clist = re.split(r'\n- - - - - - - - -.*?\n', content)
                if len(clist) > 1:
                    record[3] = clist[1]
                    records[i] = tuple(record)
            if len(records) <= 1:
                reply = Reply(ReplyType.INFO, "无聊天记录可供总结")
                e_context['reply'] = reply
                e_context.action = EventAction.BREAK_PASS
                return

            max_tokens_persession = 4800

            count, summarys = self._split_messages_to_summarys(records, max_tokens_persession)
            if count == 0:
                if isinstance(summarys, str):
                    reply = Reply(ReplyType.ERROR, summarys)
                else:
                    reply = Reply(ReplyType.ERROR, "总结聊天记录失败")
                e_context['reply'] = reply
                e_context.action = EventAction.BREAK_PASS
                return

            if len(summarys) == 1:
                image_path = self.text2img.convert_text_to_image(summarys[0])
                reply = Reply(ReplyType.IMAGE, image_path)
                e_context['reply'] = reply
                e_context.action = EventAction.BREAK_PASS
                # 删除文件
                os.remove(image_path)
                self.db.save_summary_time(session_id, int(time.time()))
                return

            self.bot.args["max_tokens"] = None
            query = ""
            for i, summary in enumerate(reversed(summarys)):
                query += summary + "\n----------------\n\n"
            prompt = "你是一位群聊机器人，聊天记录已经在你的大脑中被你总结成多段摘要总结，你需要对它们进行摘要总结，最后输出一篇完整的摘要总结，用列表的形式输出。\n"
            logger.debug("[Summary] query: %s" % query)

            session = self.bot.sessions.build_session(session_id, prompt)
            session.add_query(query)
            result = self.bot.reply_text(session)
            total_tokens, completion_tokens, reply_content = result['total_tokens'], result['completion_tokens'], \
                result['content']
            logger.debug("[Summary] total_tokens: %d, completion_tokens: %d, reply_content: %s" % (
                total_tokens, completion_tokens, reply_content))
            if completion_tokens == 0:
                reply = Reply(ReplyType.ERROR, "合并摘要失败，" + reply_content + "\n原始多段摘要如下：\n" + query)
            else:
                reply = Reply(ReplyType.TEXT, f"本次总结了{count}条消息。\n\n" + reply_content)
            e_context['reply'] = reply
            e_context.action = EventAction.BREAK_PASS  # 事件结束，并跳过处理context的默认逻辑
            self.db.save_summary_time(session_id, int(time.time()))

    def _translate_text_to_commands(self, text):
        # 随机的session id
        session_id = str(time.time())
        session = self.bot.sessions.build_session(session_id, system_prompt=TRANSLATE_PROMPT)
        session.add_query(text)
        content = self.bot.reply_text(session)
        logger.debug("_translate_text_to_commands: %s" % content)
        return content

    def get_help_text(self, verbose=False, **kwargs):
        help_text = "聊天记录总结插件。\n"
        if not verbose:
            return help_text
        trigger_prefix = conf().get('plugin_trigger_prefix', "$")
        help_text += f"使用方法:输入\"{trigger_prefix}总结 最近消息数量\"，我会帮助你总结聊天记录。\n例如：\"{trigger_prefix}总结 100\"，我会总结最近100条消息。\n\n你也可以直接输入\"{trigger_prefix}总结前99条信息\"或\"{trigger_prefix}总结3小时内的最近10条消息\"\n我会尽可能理解你的指令。"
        return help_text
