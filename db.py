#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
@Author sineom
@Date 2024/7/23-09:20
@Email h.sineom@gmail.com
@description  sqlite操作
@Copyright (c) 2022 by sineom, All Rights Reserved.
"""
import datetime
import os
import sqlite3

from common.log import logger


class Db:
    def __init__(self):
        curdir = os.path.dirname(__file__)
        db_path = os.path.join(curdir, "chat.db")
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        c = self.conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS chat_records
                            (sessionid TEXT, msgid INTEGER, user TEXT, content TEXT, type TEXT, timestamp TEXT, is_triggered INTEGER, create_time TEXT,
                            PRIMARY KEY (sessionid, msgid))''')

        # 创建一个总结时间表，记录合适开始了总结的时间
        c.execute('''CREATE TABLE IF NOT EXISTS summary_time
                            (sessionid TEXT, summary_time INTEGER, PRIMARY KEY (sessionid))''')

        # 创建一个关闭保存聊天记录的表
        c.execute('''CREATE TABLE IF NOT EXISTS summary_stop
                            (sessionid TEXT, PRIMARY KEY (sessionid))''')

        # 后期增加了is_triggered字段，这里做个过渡，这段代码某天会删除
        c = c.execute("PRAGMA table_info(chat_records);")
        column_exists = False
        for column in c.fetchall():
            logger.debug("[Summary] column: {}".format(column))
            if column[1] == 'is_triggered':
                column_exists = True
                break
        if not column_exists:
            self.conn.execute("ALTER TABLE chat_records ADD COLUMN is_triggered INTEGER DEFAULT 0;")
            self.conn.execute("UPDATE chat_records SET is_triggered = 0;")

        self.conn.commit()
        # 禁用的群聊
        self.disable_group = self._get_summary_stop()

    def insert_record(self, session_id, msg_id, user, content, msg_type, timestamp, is_triggered=0):
        c = self.conn.cursor()
        # 10位timestamp 转换为易读的时间
        create_time = datetime.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
        logger.debug("[Summary] insert record: {} {} {} {} {} {} {} {}".format(session_id, msg_id, user, content, msg_type,
                                                                            timestamp, is_triggered, create_time))
        c.execute("INSERT OR REPLACE INTO chat_records VALUES (?,?,?,?,?,?,?,?)",
                  (session_id, msg_id, user, content, msg_type, timestamp, is_triggered, create_time))
        self.conn.commit()

    # 根据时间删除记录
    def delete_records(self, start_timestamp):
        try:
            c = self.conn.cursor()
            c.execute('''
                        DELETE FROM chat_records
                        WHERE timestamp < ?
                    ''', start_timestamp,)
            self.conn.commit()
            logger.info("Records older have been cleaned.")
        except Exception as e:
            logger.error(e)

    # 保存总结时间，如果表中不存在则插入，如果存在则更新
    def save_summary_time(self, session_id, summary_time):
        if self.get_summary_time(session_id) is None:
            self._insert_summary_time(session_id, summary_time)
        else:
            self._update_summary_time(session_id, summary_time)

    # 插入总结时间
    def _insert_summary_time(self, session_id, summary_time):
        c = self.conn.cursor()
        logger.debug("[Summary] insert summary time: {} {}".format(session_id, summary_time))
        c.execute("INSERT OR REPLACE INTO summary_time VALUES (?,?)",
                  (session_id, summary_time))
        self.conn.commit()

    # 更新总结时间
    def _update_summary_time(self, session_id, summary_time):
        c = self.conn.cursor()
        logger.debug("[Summary] update summary time: {} {}".format(session_id, summary_time))
        c.execute("UPDATE summary_time SET summary_time = ? WHERE sessionid = ?",
                  (summary_time, session_id))
        self.conn.commit()
    
    # 获取总结时间，如果不存在返回None
    def get_summary_time(self, session_id):
        c = self.conn.cursor()
        c.execute("SELECT summary_time FROM summary_time WHERE sessionid=?", (session_id,))
        row = c.fetchone()
        if row is None:
            return None
        return row[0]

    def get_records(self, session_id, start_timestamp:int = None, limit:int = None, username: list=None) -> list:
        c = self.conn.cursor()
        
        # 构建基础SQL查询
        sql = "SELECT * FROM chat_records WHERE sessionid=?"
        params = [session_id]

        # 添加时间筛选条件
        if start_timestamp:
            sql += " AND timestamp>?"
            params.append(start_timestamp)
        
        # 添加用户名筛选条件
        if username:
            # 将搜索条件按@分割成多个用户名,并去掉@符号
            sql += " AND ("
            sql += " OR ".join(["user LIKE ?" for _ in username])
            sql += ")"
            params.extend(["%" + u + "%" for u in username])
            # 如果没有指定limit，则根据用户数量设置limit
            if limit is None:
                limit = len(username) * 250

        # 添加排序和限制条件
        sql += " ORDER BY timestamp DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)

        c.execute(sql, params)
        return c.fetchall()

    # 删除禁用的群聊
    def delete_summary_stop(self, session_id):
        try:
            c = self.conn.cursor()
            c.execute("DELETE FROM summary_stop WHERE sessionid=?", (session_id,))
            self.conn.commit()
            if session_id in self.disable_group:
                self.disable_group.remove(session_id)
        except Exception as e:
            logger.error(e)

    # 保存禁用的群聊
    def save_summary_stop(self, session_id):
        try:
            c = self.conn.cursor()
            c.execute("INSERT OR REPLACE INTO summary_stop VALUES (?)",
                      (session_id,))
            self.conn.commit()
            self.disable_group.add(session_id)
        except Exception as e:
            logger.error(e)

    # 获取所有禁用的群聊
    def _get_summary_stop(self):
        c = self.conn.cursor()
        c.execute("SELECT sessionid FROM summary_stop")
        return set(c.fetchall())


