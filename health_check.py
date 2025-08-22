#!/usr/bin/env python3
"""
Health Check Script for X to Telegram Bot
ตรวจสอบสถานะการทำงานของ Bot
"""

import os
import asyncio
import aiohttp
import tweepy
from telegram import Bot
from datetime import datetime
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class BotHealthChecker:
    def __init__(self):
        self.telegram_token = os.getenv('TELEGRAM_BOT_TOKEN')
        self.telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID')
        self.openai_api_key = os.getenv('OPENAI_API_KEY')
        
        # X API credentials
        self.x_bearer_token = os.getenv('X_BEARER_TOKEN_1')
        self.x_consumer_key = os.getenv('X_CONSUMER_KEY_1')
        self.x_consumer_secret = os.getenv('X_CONSUMER_SECRET_1')
        self.x_access_token = os.getenv('X_ACCESS_TOKEN_1')
        self.x_access_token_secret = os.getenv('X_ACCESS_TOKEN_SECRET_1')
        
    async def check_telegram_connection(self):
        """ตรวจสอบการเชื่อมต่อ Telegram"""
        try:
            bot = Bot(token=self.telegram_token)
            me = await bot.get_me()
            logger.info(f"✅ Telegram Bot: {me.username}")
            return True
        except Exception as e:
            logger.error(f"❌ Telegram connection failed: {str(e)}")
            return False
            
    def check_x_api_connection(self):
        """ตรวจสอบการเชื่อมต่อ X API"""
        try:
            client = tweepy.Client(
                bearer_token=self.x_bearer_token,
                consumer_key=self.x_consumer_key,
                consumer_secret=self.x_consumer_secret,
                access_token=self.x_access_token,
                access_token_secret=self.x_access_token_secret
            )
            
            # ทดสอบการเชื่อมต่อ
            user = client.get_user(username='glassnode')
            if user.data:
                logger.info(f"✅ X API Connection: Success")
                logger.info(f"✅ Target User: @glassnode (ID: {user.data.id})")
                return True
            else:
                logger.error("❌ X API: Cannot fetch user data")
                return False
                
        except Exception as e:
            logger.error(f"❌ X API connection failed: {str(e)}")
            return False
            
    async def check_openai_connection(self):
        """ตรวจสอบการเชื่อมต่อ OpenAI API"""
        try:
            headers = {
                'Authorization': f'Bearer {self.openai_api_key}',
                'Content-Type': 'application/json'
            }
            
            payload = {
                'model': 'gpt-4o-mini',
                'messages': [
                    {'role': 'user', 'content': 'Test connection'}
                ],
                'max_tokens': 10
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    'https://api.openai.com/v1/chat/completions',
                    headers=headers,
                    json=payload,
                    timeout=10
                ) as response:
                    if response.status == 200:
                        logger.info("✅ OpenAI API Connection: Success")
                        return True
                    else:
                        logger.error(f"❌ OpenAI API failed: Status {response.status}")
                        return False
                        
        except Exception as e:
            logger.error(f"❌ OpenAI API connection failed: {str(e)}")
            return False
            
    def check_environment_variables(self):
        """ตรวจสอบ Environment Variables"""
        required_vars = [
            'TELEGRAM_BOT_TOKEN',
            'TELEGRAM_CHAT_ID',
            'OPENAI_API_KEY',
            'X_BEARER_TOKEN_1',
            'X_CONSUMER_KEY_1',
            'X_CONSUMER_SECRET_1',
            'X_ACCESS_TOKEN_1',
            'X_ACCESS_TOKEN_SECRET_1'
        ]
        
        missing_vars = []
        for var in required_vars:
            if not os.getenv(var):
                missing_vars.append(var)
                
        if missing_vars:
            logger.error(f"❌ Missing environment variables: {missing_vars}")
            return False
        else:
            logger.info("✅ All environment variables are set")
            return True
            
    async def send_health_report(self, results):
        """ส่งรายงานสถานะไปยัง Telegram"""
        try:
            bot = Bot(token=self.telegram_token)
            
            report = "🔍 <b>Bot Health Check Report</b>\n\n"
            
            # Environment Variables
            if results['env_vars']:
                report += "✅ Environment Variables: OK\n"
            else:
                report += "❌ Environment Variables: Missing\n"
                
            # Telegram
            if results['telegram']:
                report += "✅ Telegram Connection: OK\n"
            else:
                report += "❌ Telegram Connection: Failed\n"
                
            # X API
            if results['x_api']:
                report += "✅ X API Connection: OK\n"
            else:
                report += "❌ X API Connection: Failed\n"
                
            # OpenAI
            if results['openai']:
                report += "✅ OpenAI API Connection: OK\n"
            else:
                report += "❌ OpenAI API Connection: Failed\n"
                
            # Overall Status
            all_good = all(results.values())
            if all_good:
                report += "\n🎉 <b>Overall Status: ALL SYSTEMS GO!</b>"
            else:
                report += "\n⚠️ <b>Overall Status: ISSUES DETECTED</b>"
                
            report += f"\n\n⏰ Check Time: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
            
            await bot.send_message(
                chat_id=self.telegram_chat_id,
                text=report,
                parse_mode='HTML'
            )
            
            logger.info("Health report sent to Telegram")
            
        except Exception as e:
            logger.error(f"Failed to send health report: {str(e)}")
            
    async def run_health_check(self):
        """รันการตรวจสอบสถานะ"""
        logger.info("🔍 Starting Bot Health Check...")
        
        results = {
            'env_vars': self.check_environment_variables(),
            'telegram': await self.check_telegram_connection(),
            'x_api': self.check_x_api_connection(),
            'openai': await self.check_openai_connection()
        }
        
        # ส่งรายงาน
        await self.send_health_report(results)
        
        # Summary
        passed = sum(results.values())
        total = len(results)
        
        logger.info(f"Health Check Completed: {passed}/{total} checks passed")
        
        return all(results.values())

async def main():
    """Main function"""
    checker = BotHealthChecker()
    success = await checker.run_health_check()
    
    if success:
        logger.info("🎉 All health checks passed!")
        exit(0)
    else:
        logger.error("❌ Some health checks failed!")
        exit(1)

if __name__ == "__main__":
    asyncio.run(main())
