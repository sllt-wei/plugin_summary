'''
Author: sineom h.sineom@gmail.com
Date: 2024-11-11 17:42:22
LastEditors: sineom h.sineom@gmail.com
LastEditTime: 2024-11-11 17:42:25
FilePath: /plugin_summary/text2img.py
Description: 

Copyright (c) 2024 by sineom, All Rights Reserved. 
'''
import os
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException, WebDriverException
import time
import base64
import logging

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class Text2ImageConverter:
    def __init__(self):
        self.driver = None
        self.url = 'https://www.text2image.online/zh-cn/'
        self.output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'images')
        
    def setup_driver(self):
        """初始化浏览器驱动"""
        try:
            self.driver = webdriver.Chrome()
            self.driver.maximize_window()  # 最大化窗口，提高元素可见性
        except WebDriverException as e:
            logger.error(f"Failed to initialize Chrome driver: {e}")
            raise

    def convert_text_to_image(self, text):
        """将文本转换为图片"""
        try:
            # 打开网页
            self.driver.get(self.url)
            
            # 等待页面完全加载
            WebDriverWait(self.driver, 10).until(
                lambda driver: driver.execute_script("return document.readyState") == "complete"
            )
            time.sleep(1)  # 额外等待以确保 JavaScript 完全执行
            
            logger.info("Website loaded successfully")

            # 先处理底部链接下拉菜单
            try:
                select_element = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "div.cell:nth-child(12) select"))
                )
                # 使用 JavaScript 来设置选择值，避免直接点击可能引起的问题
                self.driver.execute_script("""
                    let select = arguments[0];
                    select.value = 'N';  // 假设 '1' 是"隐藏"选项的值
                    select.dispatchEvent(new Event('change'));
                """, select_element)
                logger.info("Bottom link hidden")
            except TimeoutException:
                logger.warning("Bottom link dropdown menu not found")

            # 等待文本框加载并确保它是可交互的
            text_box = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, 'textarea'))
            )
            logger.info("Text box found and clickable")

            # 使用更可靠的方式清空文本框
            try:
                # 先尝试使用 JavaScript 清空
                self.driver.execute_script("""
                    let textarea = arguments[0];
                    textarea.value = '';
                    textarea.dispatchEvent(new Event('input'));
                    textarea.dispatchEvent(new Event('change'));
                """, text_box)
                
                # 再次检查并确保文本框为空
                if text_box.get_attribute('value').strip():
                    # 如果还有内容，使用键盘快捷键清空
                    text_box.click()
                    time.sleep(0.5)
                    text_box.send_keys(Keys.CONTROL + "a")
                    text_box.send_keys(Keys.DELETE)
                    time.sleep(0.5)
                
                logger.info("Textarea cleared successfully")
            except Exception as e:
                logger.warning(f"Error clearing textarea: {e}")
                # 如果清空失败，我们仍然继续执行，因为接下来的输入可能会覆盖现有内容

            # 输入新文本
            # 使用 JavaScript 设置文本，这样更可靠
            self.driver.execute_script("arguments[0].value = arguments[1];", text_box, text)
            # 触发必要的事件
            self.driver.execute_script("""
                let textarea = arguments[0];
                textarea.dispatchEvent(new Event('input'));
                textarea.dispatchEvent(new Event('change'));
            """, text_box)
            logger.info("Text input completed")

            # 等待图片生成
            img_element = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'img[alt="Image"]'))
            )
            # 确保图片已经更新（等待src属性变化）
            time.sleep(2)
            logger.info("Image generated")

            # 获取并保存图片
            img_base64_data = img_element.get_attribute('src').split(',')[1]
            img_data = base64.b64decode(img_base64_data)
            
            # 确保输出目录存在
            os.makedirs(self.output_dir, exist_ok=True)
            
            # 生成唯一的文件名
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            image_path = os.path.join(self.output_dir, f'image_{timestamp}.png')
            
            with open(image_path, 'wb') as f:
                f.write(img_data)
            logger.info(f"Image saved to {image_path}")
            
            return image_path

        except Exception as e:
            logger.error(f"Error during conversion: {e}")
            raise
        
    def close(self):
        """关闭浏览器"""
        if self.driver:
            try:
                self.driver.quit()
                logger.info("Browser closed successfully")
            except Exception as e:
                logger.error(f"Error closing browser: {e}")

def main():
    converter = Text2ImageConverter()
    try:
        converter.setup_driver()
        
        # 示例文本
        text = """
        本次总结了17条消息。
        - 妮可被要求开启总结
        - 一系列数字和乱码信息被发送
        - 提及"额外腐恶费"和"发热"
        - 请求妮可进行总结"""
        
        image_path = converter.convert_text_to_image(text)
        print(f"Image generated successfully at: {image_path}")
        
    except Exception as e:
        logger.error(f"Process failed: {e}")
    finally:
        converter.close()

if __name__ == "__main__":
    main()