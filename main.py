import os
import json
import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set
import aiohttp
import tweepy
from telegram import Bot, InputMediaPhoto, InputMediaVideo
from telegram.error import TelegramError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
import sqlite3
from threading import Lock
import pytz
from aiohttp import web
import hashlib

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class XTelegramBot:
    def __init__(self):
        self.telegram_token = os.getenv('TELEGRAM_BOT_TOKEN')
        self.telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID')
        self.openai_api_key = os.getenv('OPENAI_API_KEY')
        self.thai_tz = pytz.timezone('Asia/Bangkok')
        self.x_accounts = self._setup_x_accounts()
        self.current_account_index = 0
        self.account_stats = {}
        
        self.target_username = os.getenv('TARGET_USERNAME')
        self.cached_user_id = None
        
        self.db_lock = Lock()
        self.scheduler = AsyncIOScheduler()
        self.telegram_bot = Bot(token=self.telegram_token)
        self.processed_tweets: Set[str] = set()
        self.processed_content_hashes: Set[str] = set()
        self.since_id = None
        self.translation_cache = {}
        self.max_cache_size = 50
        self.fetch_interval = 20 
        self.ping_interval = 5
        self.cleanup_interval = 15
        self.max_tweets_per_fetch = 5
        self.api_timeout = 15
        
        self.init_database()
        self.load_processed_tweets()
        self._init_account_stats()
        self.startup_file = '/tmp/bot_startup_time.txt'
        self.is_genuine_startup = self._check_genuine_startup()

    def is_already_processing(self, tweet_id: str) -> bool:
        """ตรวจสอบว่า tweet นี้กำลังถูกประมวลผลอยู่หรือไม่"""
        return hasattr(self, '_processing_tweets') and tweet_id in getattr(self, '_processing_tweets', set())

    def mark_processing(self, tweet_id: str):
        """ทำเครื่องหมายว่า tweet นี้กำลังถูกประมวลผล"""
        if not hasattr(self, '_processing_tweets'):
            self._processing_tweets = set()
        self._processing_tweets.add(tweet_id)
        logger.info(f"🔄 Marked tweet {tweet_id} as processing")
    
    def unmark_processing(self, tweet_id: str):
        """ยกเลิกเครื่องหมายการประมวลผล"""
        if hasattr(self, '_processing_tweets'):
            self._processing_tweets.discard(tweet_id)
            logger.info(f"✅ Unmarked tweet {tweet_id} from processing")
    
    def _setup_x_accounts(self) -> List[Dict]:
        """Setup X accounts for rotation"""
        accounts = []
        for i in range(1, 4):
            bearer_token = os.getenv(f'X_BEARER_TOKEN_{i}')
            if bearer_token:
                accounts.append({
                    'id': f'account_{i}',
                    'bearer_token': bearer_token,
                    'consumer_key': os.getenv(f'X_CONSUMER_KEY_{i}'),
                    'consumer_secret': os.getenv(f'X_CONSUMER_SECRET_{i}'),
                    'access_token': os.getenv(f'X_ACCESS_TOKEN_{i}'),
                    'access_token_secret': os.getenv(f'X_ACCESS_TOKEN_SECRET_{i}')
                })
        return accounts
    
    def _init_account_stats(self):
        """Initialize account statistics"""
        for account in self.x_accounts:
            self.account_stats[account['id']] = {
                'api_calls': 0,
                'last_used': 0,
                'rate_limited_until': 0,
                'successful_calls': 0,
                'failed_calls': 0,
                'consecutive_failures': 0,
                'total_rotation_time': 0,
                'last_rotation': 0
            }

    def _check_genuine_startup(self) -> bool:
        """ตรวจสอบว่าเป็น startup จริงหรือ restart จาก Render"""
        try:
            current_time = time.time()
        
            try:
                with open(self.startup_file, 'r') as f:
                    last_startup = float(f.read().strip())
                    
                if current_time - last_startup < 600:  # 10 minutes
                    logger.info(f"Render restart detected (last start: {current_time - last_startup:.0f}s ago)")
                    return False
                    
            except FileNotFoundError:
                logger.info("First startup detected")
            
            with open(self.startup_file, 'w') as f:
                f.write(str(current_time))
                
            return True
            
        except Exception as e:
            logger.error(f"Startup check error: {e}")
            return True  
    
    def get_best_available_account(self) -> Dict:
        """Get best available account with rotation every 20 minutes"""
        current_time = time.time()
    
        time_slot = int(current_time // (20 * 60))  # 20 minutes = 1200 seconds
        preferred_index = time_slot % len(self.x_accounts)
        
        preferred_account = self.x_accounts[preferred_index]
        preferred_stats = self.account_stats[preferred_account['id']]
        
        if preferred_stats['rate_limited_until'] <= current_time:
            self.current_account_index = preferred_index
            logger.info(f"Using preferred account {preferred_index + 1} (time-based rotation)")
            return {'index': preferred_index, 'account': preferred_account}
        
        available_accounts = []
        for i, account in enumerate(self.x_accounts):
            stats = self.account_stats[account['id']]
            if stats['rate_limited_until'] <= current_time:
                available_accounts.append({'index': i, 'account': account})
    
        if available_accounts:
            next_account = available_accounts[0]
            self.current_account_index = next_account['index']
            logger.info(f"Preferred account {preferred_index + 1} unavailable, using account {next_account['index'] + 1}")
            return next_account
        
        logger.warning("No accounts available, falling back to account 1")
        self.current_account_index = 0
        return {'index': 0, 'account': self.x_accounts[0]}

    def get_account_health_report(self) -> str:
        """Get account health report"""
        current_time = time.time()
        report_lines = ["📊 <b>Account Status Report</b>\n"]
        
        for i, account in enumerate(self.x_accounts):
            stats = self.account_stats[account['id']]
            
            is_current = (i == self.current_account_index)
            is_rate_limited = stats['rate_limited_until'] > current_time
            
            success_rate = 0
            if stats['api_calls'] > 0:
                success_rate = (stats['successful_calls'] / stats['api_calls']) * 100
            
            status_emoji = "🟢"
            status_text = "พร้อมใช้"
            
            if is_rate_limited:
                status_emoji = "🔴"
                remaining_time = int(stats['rate_limited_until'] - current_time)
                status_text = f"Rate Limited ({remaining_time//60}m {remaining_time%60}s)"
            elif stats['consecutive_failures'] >= 2:
                status_emoji = "🟡"
                status_text = f"มีปัญหา (fail {stats['consecutive_failures']} ครั้ง)"
            
            current_indicator = "👈 <b>ใช้อยู่</b>" if is_current else ""
            
            report_lines.append(
                f"{status_emoji} <b>Account {i+1}</b> {current_indicator}\n"
                f"   • สถานะ: {status_text}\n"
                f"   • เรียกใช้: {stats['api_calls']} ครั้ง\n"
                f"   • สำเร็จ: {success_rate:.1f}%\n"
            )
        
        return "\n".join(report_lines)
    
    async def send_account_rotation_notification(self, old_account_index: int, new_account_index: int, reason: str):
        """Send notification when account rotation happens"""
        thai_time = self.get_thai_time()
        logger.info(f"Account Rotation: Account {old_account_index + 1} -> Account {new_account_index + 1}, Reason: {reason}, Time: {thai_time}")
        return
    
    def get_next_available_account(self, current_index: int) -> Dict:
        """Get next available account when current one fails"""
        current_time = time.time()
    
        for i in range(1, len(self.x_accounts)):
            next_index = (current_index + i) % len(self.x_accounts)
            account = self.x_accounts[next_index]
            stats = self.account_stats[account['id']]
            
            if stats['rate_limited_until'] <= current_time:
                logger.info(f"Switching from account {current_index + 1} to account {next_index + 1}")
                return {'index': next_index, 'account': account}
        
        logger.warning(f"No alternative accounts available, staying with account {current_index + 1}")
        return {'index': current_index, 'account': self.x_accounts[current_index]}
    
    def update_account_stats(self, account_id: str, success: bool, rate_limited: bool = False):
        """Update account statistics with proper error handling"""
        try:
            current_time = time.time()
            stats = self.account_stats[account_id]
            
            stats['api_calls'] += 1
            stats['last_used'] = current_time
            
            if success:
                stats['successful_calls'] += 1
                stats['consecutive_failures'] = 0  # รีเซ็ตเมื่อสำเร็จ
            else:
                stats['failed_calls'] += 1
                stats['consecutive_failures'] = stats.get('consecutive_failures', 0) + 1
                
            if rate_limited:
                stats['rate_limited_until'] = current_time + 1200
                logger.warning(f"Account {account_id} rate limited until {datetime.fromtimestamp(stats['rate_limited_until']).strftime('%H:%M:%S')}")
                
                current_index = self.current_account_index
                next_account = self.get_next_available_account(current_index)
                if next_account['index'] != current_index:
                    self.current_account_index = next_account['index']
                    logger.info(f"Switched to account {next_account['index'] + 1}")
            
            elif stats['consecutive_failures'] >= 3:
                logger.warning(f"Account {account_id} failed {stats['consecutive_failures']} times, switching")
                current_index = self.current_account_index
                next_account = self.get_next_available_account(current_index)
                if next_account['index'] != current_index:
                    self.current_account_index = next_account['index']
                    logger.info(f"Switched to account {next_account['index'] + 1}")
                    
        except Exception as e:
            logger.error(f"Error updating account stats: {e}")
    
    def get_thai_time(self, utc_dt: datetime = None) -> str:
        """Get Thai formatted time"""
        if utc_dt is None:
            utc_dt = datetime.now(pytz.utc)
        elif utc_dt.tzinfo is None:
            utc_dt = pytz.utc.localize(utc_dt)
        
        thai_time = utc_dt.astimezone(self.thai_tz)
        return thai_time.strftime('%d/%m %H:%M')
    
    def generate_content_hash(self, content: str, media_urls: List[str] = None) -> str:
        """Generate hash for content deduplication"""
        content_for_hash = content.strip().lower()
        if media_urls:
            content_for_hash += '|'.join(sorted(media_urls))
        return hashlib.md5(content_for_hash.encode()).hexdigest()
    
    def is_tweet_too_old(self, tweet_created_at: datetime) -> bool:
        """Check if tweet is older than"""
        if tweet_created_at.tzinfo is None:
            tweet_created_at = pytz.utc.localize(tweet_created_at)
        
        now = datetime.now(pytz.utc)
        time_diff = now - tweet_created_at
        return time_diff > timedelta(minutes=60) # ดึงข้อความไม่เก่ากว่า 60นาที

    def is_emoji_only_post(self, text: str) -> bool:
        """ตรวจสอบว่าโพสมี emoji อย่างเดียวหรือไม่"""
        import re
    
        try:
            # ลบ whitespace, newline ทุกประเภทออก
            clean_text = re.sub(r'\s+', '', text.strip())
            
            # ถ้าข้อความว่าง return False
            if not clean_text:
                return False
            
            # Emoji pattern - รวม Unicode emoji ranges ต่างๆ
            emoji_pattern = re.compile(
                "["
                "\U0001F600-\U0001F64F"  # emoticons
                "\U0001F300-\U0001F5FF"  # symbols & pictographs  
                "\U0001F680-\U0001F6FF"  # transport & map
                "\U0001F1E0-\U0001F1FF"  # flags (iOS)
                "\U0001F700-\U0001F77F"  # alchemical symbols
                "\U0001F780-\U0001F7FF"  # Geometric Shapes Extended
                "\U0001F800-\U0001F8FF"  # Supplemental Arrows-C
                "\U0001F900-\U0001F9FF"  # Supplemental Symbols and Pictographs
                "\U0001FA00-\U0001FA6F"  # Chess Symbols
                "\U0001FA70-\U0001FAFF"  # Symbols and Pictographs Extended-A
                "\U00002600-\U000026FF"  # Miscellaneous Symbols
                "\U00002700-\U000027BF"  # Dingbats
                "\U0000FE00-\U0000FE0F"  # Variation Selectors
                "\U0001F004"             # Mahjong Red Dragon
                "\U0001F0CF"             # Playing Card Black Joker
                "]+"
            )
            
            # ลบ emoji ทั้งหมดออกจากข้อความ
            text_without_emoji = emoji_pattern.sub('', clean_text)
        
            # ถ้าไม่เหลืออะไรเลย = มี emoji อย่างเดียว
            result = len(text_without_emoji.strip()) == 0 and len(clean_text) > 0
            
            if result:
                logger.info(f"🚫 Detected emoji-only post: '{text[:50]}...'")
                
            return result
            
        except Exception as e:
            logger.error(f"Error checking emoji-only post: {e}")
            return False
    
    def is_link_only_post(self, text: str) -> bool:
        """ตรวจสอบว่าโพสมี link อย่างเดียวหรือไม่"""
        import re
        
        try:
            # ลบ whitespace ออก
            clean_text = text.strip()
            
            # ถ้าข้อความว่าง return False
            if not clean_text:
                return False
            
            # URL patterns - รวมหลายรูปแบบ
            url_patterns = [
                r'https?://[^\s]+',           # http://... หรือ https://...
                r'www\.[^\s]+',               # www....
                r't\.co/[^\s]+',              # Twitter short links
                r'bit\.ly/[^\s]+',            # Bitly links
                r'tinyurl\.com/[^\s]+',       # TinyURL
                r'youtu\.be/[^\s]+',          # YouTube short links
                r'[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/?[^\s]*'  # domain.com/...
            ]
            
            # รวม patterns ทั้งหมด
            combined_pattern = '|'.join(f'({pattern})' for pattern in url_patterns)
            url_regex = re.compile(combined_pattern, re.IGNORECASE)
            
            # หา URLs ทั้งหมด
            urls = url_regex.findall(clean_text)
            
            # ถ้าไม่มี URL เลย return False
            if not urls:
                return False
            
            # ลบ URLs ทั้งหมดออก
            text_without_urls = url_regex.sub('', clean_text)
            
            # ลบ whitespace ที่เหลือ
            remaining_text = re.sub(r'\s+', '', text_without_urls).strip()
            
            # ถ้าไม่เหลืออะไรเลย = มี link อย่างเดียว
            result = len(remaining_text) == 0
            
            if result:
                logger.info(f"🚫 Detected link-only post: '{text[:50]}...'")
                
            return result
            
        except Exception as e:
            logger.error(f"Error checking link-only post: {e}")
            return False
    
    def should_skip_post(self, text: str) -> tuple:
        """
        ตรวจสอบว่าควรข้ามโพสนี้หรือไม่
        Returns: (should_skip: bool, reason: str)
        """
        try:
            # เพิ่มการตรวจสอบข้อความสั้นเกินไป (เพิ่มแค่ 4 บรรทัดนี้)
            import re
            clean_text = re.sub(r'[^\w]', '', text)
            if len(clean_text) < 10:
                return True, "too_short"

            # ตรวจสอบ emoji อย่างเดียว
            if self.is_emoji_only_post(text):
                return True, "emoji_only"
        
            # ตรวจสอบ link อย่างเดียว  
            if self.is_link_only_post(text):
                return True, "link_only"
            
            # โพสปกติ - ส่งได้
            return False, "normal"
            
        except Exception as e:
            logger.error(f"Error in should_skip_post: {e}")
            return False, "error"

    async def is_self_interaction(self, tweet, client, account_id) -> tuple:
        """ตรวจสอบว่าเป็นการโต้ตอบกับตัวเองหรือไม่ - ปรับปรุงแล้ว (Self-mention Priority)"""
        try:
            # ============= PRIORITY 1: ตรวจสอบ Self-Mention ก่อนเสมอ =============
            has_self_mention = False
            other_mentions = []
            
            if tweet.text.startswith('@') or '@' in tweet.text:
                try:
                    # Extract all mentions from tweet text
                    import re
                    mention_pattern = r'@(\w+)'
                    all_mentions = re.findall(mention_pattern, tweet.text.lower())
                    
                    for mention in all_mentions:
                        if mention == self.target_username.lower():
                            has_self_mention = True
                            logger.info(f"✅ Self-mention found in tweet {tweet.id}: @{mention}")
                        else:
                            other_mentions.append(mention)
                    
                    # ถ้ามี self-mention ให้ return ทันที (Priority สูงสุด)
                    if has_self_mention:
                        if other_mentions:
                            logger.info(f"✅ Mixed mention detected: self + others {other_mentions}")
                            return True, 'self_mention_mixed', f"{self.target_username}+{len(other_mentions)}others"
                        else:
                            logger.info(f"✅ Pure self-mention detected")
                            return True, 'self_mention_pure', self.target_username
                            
                except Exception as e:
                    logger.warning(f"Error checking mentions in {tweet.id}: {e}")
            
            # ============= PRIORITY 2: ตรวจสอบ Retweet =============
            if hasattr(tweet, 'referenced_tweets') and tweet.referenced_tweets:
                for ref in tweet.referenced_tweets:
                    if ref.type == 'retweeted':
                        try:
                            original_tweet = client.get_tweet(ref.id, expansions=['author_id'])
                            if (original_tweet.data and 
                                original_tweet.includes and 
                                'users' in original_tweet.includes and 
                                len(original_tweet.includes['users']) > 0):
                                
                                original_author = original_tweet.includes['users'][0]
                                if original_author.username.lower() == self.target_username.lower():
                                    logger.info(f"✅ Self-retweet detected: {tweet.id}")
                                    return True, 'self_retweet', original_author.username
                                else:
                                    logger.info(f"❌ Retweet of other user: {original_author.username}")
                                    return False, 'other_retweet', original_author.username
                        except Exception as e:
                            logger.warning(f"Error checking retweet {tweet.id}: {e}")
                            continue
            
            # ตรวจสอบ RT format ใน text (legacy retweets)
            if tweet.text.startswith('RT @'):
                try:
                    rt_username = tweet.text.split('RT @')[1].split(':')[0].split(' ')[0].lower()
                    if rt_username == self.target_username.lower():
                        logger.info(f"✅ Self-RT (legacy format) detected: {tweet.id}")
                        return True, 'self_retweet_legacy', rt_username
                    else:
                        logger.info(f"❌ RT of other user (legacy): {rt_username}")
                        return False, 'other_retweet_legacy', rt_username
                except Exception as e:
                    logger.warning(f"Error parsing legacy RT {tweet.id}: {e}")
            
            # ============= PRIORITY 3: ตรวจสอบ Reply =============
            if hasattr(tweet, 'in_reply_to_user_id') and tweet.in_reply_to_user_id:
                try:
                    replied_user = client.get_user(id=tweet.in_reply_to_user_id)
                    if replied_user.data:
                        if replied_user.data.username.lower() == self.target_username.lower():
                            logger.info(f"✅ Self-reply detected: {tweet.id}")
                            return True, 'self_reply', replied_user.data.username
                        else:
                            logger.info(f"❌ Reply to other user: {replied_user.data.username}")
                            return False, 'other_reply', replied_user.data.username
                except Exception as e:
                    logger.warning(f"Error checking reply target {tweet.id}: {e}")
                    pass
            
            # ============= DEFAULT: Normal Tweet =============
            logger.info(f"✅ Normal tweet (no self-interaction): {tweet.id}")
            return False, 'normal', None
            
        except Exception as e:
            logger.error(f"Error in is_self_interaction {tweet.id}: {e}")
            return False, 'error', str(e)
    
    
    def format_message_by_interaction_type(self, tweet, translated_content, thai_time, tweet_url, interaction_type, target_info):
        """จัดรูปแบบข้อความตามประเภท interaction - รองรับ mixed mentions"""
    
        # ✅ เพิ่มการตรวจสอบ truncated tweet ทุกประเภท
        too_long_note = ""
        if self.is_truncated_tweet(tweet.text):
            too_long_note = f"\n\n🔗 <a href='{tweet_url}'>(ยาวเกิน - อ่านต่อที่ X)</a>"
        
        if interaction_type == 'self_mention_pure':
            return f"💬 <b>@{self.target_username} กล่าวถึงตัวเอง</b>\n\n{translated_content}{too_long_note}\n\n⏰ {thai_time} | 𝕏 <a href='{tweet_url}'>ที่มา</a>"
        
        elif interaction_type == 'self_mention_mixed':
            return f"💬🔀 <b>@{self.target_username} กล่าวถึงตัวเองและผู้อื่น</b>\n\n{translated_content}{too_long_note}\n\n⏰ {thai_time} | 𝕏 <a href='{tweet_url}'>ที่มา</a>"
        
        elif interaction_type == 'self_retweet':
            return f"🔄 <b>@{self.target_username} รีทวีตตัวเอง</b>\n\n{translated_content}{too_long_note}\n\n⏰ {thai_time} | 𝕏 <a href='{tweet_url}'>ที่มา</a>"
        
        elif interaction_type == 'self_retweet_legacy':
            return f"🔄📜 <b>@{self.target_username} รีทวีตตัวเอง (แบบเก่า)</b>\n\n{translated_content}{too_long_note}\n\n⏰ {thai_time} | 𝕏 <a href='{tweet_url}'>ที่มา</a>"
        
        elif interaction_type == 'self_reply':
            return f"↩️ <b>@{self.target_username} ตอบตัวเอง</b>\n\n{translated_content}{too_long_note}\n\n⏰ {thai_time} | 𝕏 <a href='{tweet_url}'>ที่มา</a>"
        
        else:
            # Normal tweet หรือกรณีอื่นๆ
            return f"𝕏 @{self.target_username}\n\n{translated_content}{too_long_note}\n\n⏰ {thai_time} | 𝕏 <a href='{tweet_url}'>ที่มา</a>"
    
    def is_reply_tweet(self, tweet) -> bool:
        """ตรวจสอบว่าเป็นโพสตอบคนอื่นหรือไม่"""
        if hasattr(tweet, 'in_reply_to_user_id') and tweet.in_reply_to_user_id:
            return True
        
        if tweet.text.strip().startswith('@'):
            return True
        
        if hasattr(tweet, 'referenced_tweets') and tweet.referenced_tweets:
            for ref in tweet.referenced_tweets:
                if ref.type == 'replied_to':
                    return True
        
        return False

    def is_reply_to_others(self, tweet) -> bool:
        """ตรวจสอบว่าเป็นการตอบคนอื่นหรือไม่ (ไม่ใช่ตัวเอง)"""
        # ตรวจสอบ in_reply_to_user_id ว่าเป็นของตัวเองหรือไม่
        if hasattr(tweet, 'in_reply_to_user_id') and tweet.in_reply_to_user_id:
            # ถ้า reply_to_user_id ไม่ใช่ user_id ของเราเอง = ตอบคนอื่น
            if self.cached_user_id and str(tweet.in_reply_to_user_id) != str(self.cached_user_id):
                return True
        
        return False
    
    def is_mention_others_only(self, tweet) -> bool:
        """ตรวจสอบว่า mention เฉพาะคนอื่น (ไม่มีตัวเอง)"""
        if not tweet.text.startswith('@'):
            return False
        
        try:
            import re
            mention_pattern = r'@(\w+)'
            all_mentions = re.findall(mention_pattern, tweet.text.lower())
            
            # ถ้าไม่มี mention ตัวเอง แต่มี mention คนอื่น = mention คนอื่นอย่างเดียว
            has_self_mention = self.target_username.lower() in all_mentions
            has_other_mentions = len(all_mentions) > 0
            
            return has_other_mentions and not has_self_mention
            
        except Exception as e:
            logger.warning(f"Error checking mentions: {e}")
            return False
    
    async def is_self_mention_or_retweet(self, tweet, client, account_id):
        """ตรวจสอบว่าเป็นการ mention หรือ retweet ตัวเองหรือไม่"""
        try:
            if hasattr(tweet, 'referenced_tweets') and tweet.referenced_tweets:
                for ref in tweet.referenced_tweets:
                    if ref.type == 'retweeted':
                        try:
                            original_tweet = client.get_tweet(ref.id, expansions=['author_id'])
                            if original_tweet.data and original_tweet.includes:
                                original_author = original_tweet.includes['users'][0]
                                if original_author.username.lower() == self.target_username.lower():
                                    logger.info(f"Found self-retweet: {tweet.id}")
                                    return True
                        except Exception as e:
                            logger.error(f"Error checking retweet author: {e}")
            
            if tweet.text.startswith('@'):
                mentions = []
                words = tweet.text.split()
                for word in words:
                    if word.startswith('@'):
                        username = word[1:].strip('.,!?:;')
                        mentions.append(username.lower())
                
                if self.target_username.lower() in mentions:
                    logger.info(f"Found self-mention: {tweet.id}")
                    return True
            
            return False
            
        except Exception as e:
            logger.error(f"Error checking self mention/retweet: {e}")
            return False

    def format_retweet_message(self, tweet, translated_content, thai_time, tweet_url):
        """จัดรูปแบบข้อความสำหรับ retweet"""
        return f"🔄 <b>@{self.target_username} รีทวีตตัวเอง</b>\n\n{translated_content}\n\n⏰ {thai_time} | 𝕏 <a href='{tweet_url}'>ที่มา</a>"

    def format_self_mention_message(self, tweet, translated_content, thai_time, tweet_url):
        """จัดรูปแบบข้อความสำหรับ self-mention"""
        return f"💬 <b>@{self.target_username} กล่าวถึงตัวเอง</b>\n\n{translated_content}\n\n⏰ {thai_time} | 𝕏 <a href='{tweet_url}'>ที่มา</a>"

    def format_self_reply_message(self, tweet, translated_content, thai_time, tweet_url):
        """จัดรูปแบบข้อความสำหรับ self-reply"""
        return f"↩️ <b>@{self.target_username} ตอบตัวเอง</b>\n\n{translated_content}\n\n⏰ {thai_time} | 𝕏 <a href='{tweet_url}'>ที่มา</a>"

    def detect_tweet_type(self, tweet):
        """ตรวจสอบประเภทของทวีต"""
        if hasattr(tweet, 'referenced_tweets') and tweet.referenced_tweets:
            for ref in tweet.referenced_tweets:
                if ref.type == 'retweeted':
                    return 'retweet'
        
        if tweet.text.startswith('RT @'):
            return 'retweet'
        
        if hasattr(tweet, 'in_reply_to_user_id') and tweet.in_reply_to_user_id:
            return 'reply'
        
        if tweet.text.startswith('@'):
            return 'mention'
        
        return 'normal'
    
    def init_database(self):
        """Initialize SQLite database"""
        conn = sqlite3.connect('bot_data.db')
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS processed_tweets (
                tweet_id TEXT PRIMARY KEY,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                content TEXT,
                translated TEXT,
                created_at TIMESTAMP,
                url TEXT,
                account_used TEXT,
                content_hash TEXT,
                conversation_id TEXT,
                is_thread BOOLEAN DEFAULT 0
            )
        ''')
        
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_conversation ON processed_tweets(conversation_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_content_hash ON processed_tweets(content_hash)')
        
        conn.commit()
        conn.close()
    
    def load_processed_tweets(self):
        """Load recent processed tweets"""
        try:
            conn = sqlite3.connect('bot_data.db')
            cursor = conn.cursor()
            
            cutoff = datetime.now() - timedelta(hours=24)
            cursor.execute('''
                SELECT tweet_id, content_hash FROM processed_tweets 
                WHERE processed_at > ?
            ''', (cutoff,))
            
            results = cursor.fetchall()
            self.processed_tweets = {row[0] for row in results}
            self.processed_content_hashes = {row[1] for row in results if row[1]}
            
            cursor.execute('''
                SELECT tweet_id FROM processed_tweets 
                WHERE processed_at > ? 
                ORDER BY processed_at DESC LIMIT 1
            ''', (cutoff,))
            
            result = cursor.fetchone()
            self.since_id = result[0] if result else None
            
            conn.close()
            logger.info(f"Loaded {len(self.processed_tweets)} recent tweets")
        except Exception as e:
            logger.error(f"Load tweets error: {e}")
    
    def save_processed_tweet(self, tweet_id: str, content: str, translated: str, 
                           created_at: datetime, url: str, account_id: str, 
                           content_hash: str, conversation_id: str = None, is_thread: bool = False):
        """Save processed tweet"""
        try:
            with self.db_lock:
                conn = sqlite3.connect('bot_data.db')
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO processed_tweets 
                    (tweet_id, content, translated, created_at, url, account_used, 
                     content_hash, conversation_id, is_thread) 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (tweet_id, content, translated, created_at, url, account_id, 
                      content_hash, conversation_id, is_thread))
                conn.commit()
                conn.close()
                
                self.processed_tweets.add(tweet_id)
                self.processed_content_hashes.add(content_hash)
                if not is_thread:
                    self.since_id = tweet_id
        except Exception as e:
            logger.error(f"Save tweet error: {e}")

    def is_truncated_tweet(self, text: str) -> bool:
        """ตรวจสอบว่า tweet ถูกตัดหรือไม่ - แก้ไขให้แม่นยำ"""
        definite_truncation_signs = [
            text.endswith("…"),
            text.endswith("..."),
            text.endswith("…\n"),
            text.endswith("...\n"),
            "Show this thread" in text,
            "Show more" in text,
            "Read more" in text
        ]
    
        return any(definite_truncation_signs)
    
    async def get_note_tweet_content(self, client: tweepy.Client, tweet_id: str, account_id: str) -> Optional[str]:
        """ดึง full content - ปรับปรุงแล้ว"""
        try:
            tweet = client.get_tweet(
                id=tweet_id,
                tweet_fields=['text', 'note_tweet', 'context_annotations'],
                expansions=['author_id']
            )
            
            self.update_account_stats(account_id, True)
            
            if tweet.data:
                if hasattr(tweet.data, 'note_tweet') and tweet.data.note_tweet:
                    if hasattr(tweet.data.note_tweet, 'text'):
                        logger.info(f"Found note tweet full text for {tweet_id}")
                        return tweet.data.note_tweet.text
                
                if self.is_truncated_tweet(tweet.data.text):
                    logger.info(f"Tweet {tweet_id} is truncated, but will use available text")
            
                return tweet.data.text
        
            return None
            
        except tweepy.TooManyRequests:
            self.update_account_stats(account_id, False, rate_limited=True)
            logger.warning(f"Rate limited when getting full content for {tweet_id}")
            return None
        except Exception as e:
            logger.error(f"Get note tweet error for {tweet_id}: {e}")
            self.update_account_stats(account_id, False)
            return None
    
    def create_x_client(self, account: Dict) -> tweepy.Client:
        """Create X client with expanded tweet support"""
        return tweepy.Client(
            bearer_token=account['bearer_token'],
            consumer_key=account['consumer_key'],
            consumer_secret=account['consumer_secret'],
            access_token=account['access_token'],
            access_token_secret=account['access_token_secret'],
            wait_on_rate_limit=False
        )

    async def get_user_id_cached(self, client: tweepy.Client, account_id: str) -> Optional[str]:
        """Get user ID with caching"""
        if self.cached_user_id:
            return self.cached_user_id
        
        try:
            user = client.get_user(username=self.target_username)
            self.update_account_stats(account_id, True)
            
            if user.data:
                self.cached_user_id = user.data.id
                logger.info(f"Cached user ID: {self.cached_user_id}")
                return self.cached_user_id
            else:
                self.update_account_stats(account_id, False)
                return None
                
        except tweepy.TooManyRequests:
            self.update_account_stats(account_id, False, rate_limited=True)
            return None
        except Exception as e:
            logger.error(f"Get user error: {e}")
            self.update_account_stats(account_id, False)
            return None
    
    async def translate_text(self, text: str) -> str:
        """Translate text with caching"""
        text_hash = hash(text)
        if text_hash in self.translation_cache:
            return self.translation_cache[text_hash]
        
        try:
            headers = {
                'Authorization': f'Bearer {self.openai_api_key}',
                'Content-Type': 'application/json'
            }
            
            payload = {
                'model': 'gpt-4o-mini',
                'messages': [
                    {
                        'role': 'system',
                        'content': '''คุณเป็นนักแปลข่าวคริปโตและการเงินมืออาชีพ แปลเป็นภาษาไทยที่เข้าใจง่าย ใช้คำศัพท์ที่คนไทยคุ้นเคย 
                        กฎการแปล:
                        ชื่อบุคคล ชื่อบริษัท ชื่อแพลตฟอร์ม → เก็บภาษาอังกฤษ
                        ตัวเลข เปอร์เซ็นต์  สกุลเงิน → เก็บภาษาอังกฤษ
                        คำศัพท์เทคนิคด้านคริปโตและการเงิน → เก็บภาษาอังกฤษ
                        ถ้าไม่แน่ใจว่าควรแปลหรือไม่ → เก็บภาษาอังกฤษ
                        แปลเฉพาะความหมายและบริบท ไม่ต้องเพิ่มคำอธิบาย'''
                    },
                    {'role': 'user', 'content': text}
                ],
                'max_tokens': 2000,
                'temperature': 0.3
            }
            
            timeout = aiohttp.ClientTimeout(total=60)
            
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    'https://api.openai.com/v1/chat/completions',
                    headers=headers,
                    json=payload
                ) as response:
                    
                    if response.status == 200:
                        data = await response.json()
                        translated = data['choices'][0]['message']['content'].strip()
                        self.translation_cache[text_hash] = translated
                        return translated
        
        except Exception as e:
            logger.error(f"Translation error: {e}")
        
        self.translation_cache[text_hash] = text
        return text
    
    async def download_media(self, url: str) -> Optional[bytes]:
        """Download media with improved timeout and validation"""
        try:
            timeout = aiohttp.ClientTimeout(total=60)
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'image/*,video/*,*/*;q=0.8'
            }
            
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        # ตรวจสอบขนาดไฟล์ก่อนดาวน์โหลด
                        content_length = response.headers.get('content-length')
                        if content_length and int(content_length) > 100 * 1024 * 1024:  # 100MB limit
                            logger.warning(f"Media too large: {content_length} bytes")
                            return None
                        
                        content = await response.read()
                        if content and len(content) > 100:  # ต้องมีขนาดมากกว่า 100 bytes
                            logger.info(f"✅ Downloaded media: {len(content)} bytes from {url}")
                            return content
                        else:
                            logger.warning(f"Media too small or empty: {len(content) if content else 0} bytes")
                    else:
                        logger.warning(f"HTTP {response.status} for media URL: {url}")
        
        except asyncio.TimeoutError:
            logger.warning(f"Media download timeout: {url}")
        except Exception as e:
            logger.error(f"Media download error for {url}: {e}")
        
        return None
    
    async def send_telegram_message(self, content: str, media_urls: List[str] = None):
        """Send message to Telegram - แก้ไขปัญหาข้อความซ้ำ"""
        try:
            # ถ้ามี media URLs ให้ลองส่งเป็น media group ก่อน
            if media_urls:
                media_files = []
                
                for i, url in enumerate(media_urls[:5]):  # จำกัดไม่เกิน 5 ไฟล์
                    media_data = await self.download_media(url)
                    if media_data:
                        caption = content[:1024] if i == 0 else None
                        
                        try:
                            if any(ext in url.lower() for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']):
                                media_files.append(InputMediaPhoto(
                                    media=media_data,
                                    caption=caption,
                                    parse_mode='HTML' if caption else None
                                ))
                            elif any(ext in url.lower() for ext in ['.mp4', '.mov', '.avi']):
                                media_files.append(InputMediaVideo(
                                    media=media_data,
                                    caption=caption,
                                    parse_mode='HTML' if caption else None
                                ))
                        except Exception as e:
                            logger.error(f"Media object creation error: {e}")
                            continue
                    
                    await asyncio.sleep(2)  # หน่วงเวลาระหว่างการดาวน์โหลด
                
                # 🔥 จุดสำคัญ: ถ้ามี media files สำเร็จ ให้ส่งแล้ว return ทันที
                if media_files:
                    try:
                        await self.telegram_bot.send_media_group(
                            chat_id=self.telegram_chat_id,
                            media=media_files
                        )
                        logger.info(f"✅ Sent media group with {len(media_files)} items")
                        return  # 🔥 สำคัญมาก: return ที่นี่เพื่อไม่ให้ส่งข้อความซ้ำ
                    except Exception as media_group_error:
                        logger.error(f"Media group send error: {media_group_error}")
                        # ถ้าส่ง media group ไม่สำเร็จ ให้ fallback เป็นข้อความธรรมดา
                else:
                    logger.warning("No media files successfully processed, falling back to text message")
            
            # ส่งเป็นข้อความธรรมดา (กรณีไม่มี media หรือ media group ส่งไม่สำเร็จ)
            try:
                await self.telegram_bot.send_message(
                    chat_id=self.telegram_chat_id,
                    text=content[:4096],
                    parse_mode='HTML'
                )
                logger.info("✅ Sent text message successfully")
                
            except Exception as text_error:
                logger.error(f"Text message send error: {text_error}")
                # ลองส่งโดยไม่ใช้ HTML parsing
                try:
                    await self.telegram_bot.send_message(
                        chat_id=self.telegram_chat_id,
                        text=content[:4096]
                    )
                    logger.info("✅ Sent fallback text message (no HTML)")
                except Exception as fallback_error:
                    logger.error(f"❌ All message send attempts failed: {fallback_error}")
                    
        except Exception as e:
            logger.error(f"❌ Critical send message error: {e}")
    
    
    async def fetch_tweets(self):
        """Fetch latest tweets - ปรับปรุงแล้ว"""
        try:
            if hasattr(self, '_is_fetching') and self._is_fetching:
                logger.info("⏳ Already fetching tweets, skipping this cycle")
                return
        
            self._is_fetching = True
        
            try:
                old_account_index = self.current_account_index
            
                account_info = self.get_best_available_account()
                account = account_info['account']
                new_account_index = account_info['index']
            
                if old_account_index != new_account_index:
                    current_time = time.time()
                    time_slot = int(current_time // (20 * 60))
                    preferred_index = time_slot % len(self.x_accounts)
                    
                    if new_account_index == preferred_index:
                        reason = "ตามเวลา (Time-based rotation)"
                    else:
                        reason = f"Account {old_account_index + 1} ไม่พร้อมใช้งาน"
                    
                    await self.send_account_rotation_notification(old_account_index, new_account_index, reason)
                
                client = self.create_x_client(account)
                logger.info(f"Using Account {new_account_index + 1} for fetching tweets")
    
                user_id = await self.get_user_id_cached(client, account['id'])
                if not user_id:
                    logger.error("Cannot get user ID")
                    return
            
                params = {
                    'id': user_id,
                    'max_results': 10,
                    'tweet_fields': [
                        'created_at', 
                        'conversation_id', 
                        'in_reply_to_user_id', 
                        'attachments', 
                        'referenced_tweets', 
                        'text',
                        'note_tweet',
                        'context_annotations',
                        'public_metrics'
                    ],
                    'expansions': [
                        'attachments.media_keys', 
                        'author_id',
                        'referenced_tweets.id',
                        'referenced_tweets.id.author_id'
                    ],
                    'media_fields': ['url', 'type', 'preview_image_url', 'alt_text'],
                }
            
                start_time = datetime.now(pytz.utc) - timedelta(hours=1) #เช็กย้อนหลัง 1ชั่วโมง
                params['start_time'] = start_time.replace(microsecond=0).isoformat()
                
                if self.since_id:
                    params['since_id'] = self.since_id
                        
                try:
                    tweets = client.get_users_tweets(**params)
                    self.update_account_stats(account['id'], True)
                except tweepy.TooManyRequests:
                    self.update_account_stats(account['id'], False, rate_limited=True)
                    logger.warning("Rate limited, waiting...")
                    return
                except Exception as e:
                    logger.error(f"Get tweets error: {e}")
                    self.update_account_stats(account['id'], False)
                    return
                
                if not tweets.data:
                    logger.info("No new tweets found")
                    return
    
                sorted_tweets = sorted(tweets.data, key=lambda x: (x.created_at, int(x.id)))
                logger.info(f"📥 Raw tweets fetched: {len(sorted_tweets)}")
                
                filtered_tweets = []
                skipped_reasons = {
                    'already_processed': 0,
                    'too_old': 0,
                    'other_interaction': 0,
                    'emoji_only': 0,
                    'link_only': 0
                }
                
                for tweet in sorted_tweets:
                    if tweet.id in self.processed_tweets:
                        skipped_reasons['already_processed'] += 1
                        continue
                
                    if self.is_tweet_too_old(tweet.created_at):
                        skipped_reasons['too_old'] += 1
                        continue
    
                    is_self, interaction_type, target = await self.is_self_interaction(tweet, client, account['id'])
                
                    # ✅ เข้มงวดขึ้น - อนุญาตเฉพาะ self-interaction และ normal tweet เท่านั้น
                    if is_self:
                        if interaction_type in ['self_mention_pure', 'self_mention_mixed', 'self_retweet', 
                                             'self_retweet_legacy', 'self_reply']:
                            logger.info(f"✅ Self-interaction ({interaction_type}): {tweet.id}")
                            filtered_tweets.append(tweet)
                        else:
                            logger.info(f"⚠️ Unknown self-interaction type: {interaction_type}, skipping")
                            skipped_reasons['other_interaction'] += 1
                    elif interaction_type == 'normal':
                        # ✅ เพิ่มการตรวจสอบซ้อน - ต้องไม่เป็น reply หรือ mention คนอื่น
                        if not self.is_reply_to_others(tweet) and not self.is_mention_others_only(tweet):
                            logger.info(f"✅ Normal tweet: {tweet.id}")
                            filtered_tweets.append(tweet)
                        else:
                            logger.info(f"❌ Normal tweet but interacts with others: {tweet.id}")
                            skipped_reasons['other_interaction'] += 1
                    elif interaction_type in ['other_reply', 'other_retweet', 'other_retweet_legacy']:
                        logger.info(f"❌ Other-interaction ({interaction_type} -> {target}): {tweet.id}")
                        skipped_reasons['other_interaction'] += 1
                    else:
                        # ✅ ไม่อนุญาตโพสที่ไม่รู้จัก - เพื่อความปลอดภัย
                        logger.info(f"❌ Unknown type ({interaction_type}), rejecting for safety: {tweet.id}")
                        skipped_reasons['other_interaction'] += 1
        
                logger.info(f"📊 Filtering results:")
                logger.info(f"   Original: {len(sorted_tweets)}")
                logger.info(f"   Accepted: {len(filtered_tweets)}")
                logger.info(f"   Already processed: {skipped_reasons['already_processed']}")
                logger.info(f"   Too old: {skipped_reasons['too_old']}")
                logger.info(f"   Other interactions: {skipped_reasons['other_interaction']}")
        
                sorted_tweets = sorted(filtered_tweets, key=lambda x: (x.created_at, int(x.id)))
    
                logger.info(f"📝 Processing {len(sorted_tweets)} tweets individually")
    
                for tweet in sorted_tweets:
                    logger.info(f"📝 Processing individual tweet: {tweet.id}")
                    success = await self.process_tweet(tweet, tweets.includes, account['id'])
                    
                    if success:
                        await asyncio.sleep(5)
                    else:
                        await asyncio.sleep(2)
            
                logger.info(f"✅ Processed {len(sorted_tweets)} tweets individually")
    
                logger.info(f"✅ Completed processing {len(sorted_tweets)} tweets")
                
            finally:
                self._is_fetching = False
            
        except Exception as e:
            logger.error(f"Fetch tweets error: {e}")
            self._is_fetching = False
    
    async def process_tweet(self, tweet, includes=None, account_id=None) -> bool:
        """Process individual tweet - ปรับปรุงให้รองรับ interaction types ใหม่"""
        try:
            if tweet.id in self.processed_tweets:
                logger.info(f"⏭️ Tweet {tweet.id} already processed, skipping")
                return False
        
            if self.is_already_processing(tweet.id):
                logger.info(f"⏳ Tweet {tweet.id} is currently being processed, skipping")
                return False
        
            self.mark_processing(tweet.id)
        
            try:
                original_content = tweet.text
                content = original_content
                was_expanded = False  
            
                logger.info(f"Processing tweet {tweet.id}, original length: {len(content)}")
                
                # ตรวจสอบ interaction type ด้วย logic ใหม่
                account_info = self.get_best_available_account()
                temp_account = account_info['account']
                temp_client = self.create_x_client(temp_account)
                
                is_self, interaction_type, target = await self.is_self_interaction(tweet, temp_client, account_id)
                
                logger.info(f"Tweet {tweet.id}: is_self={is_self}, type={interaction_type}, target={target}")
                
                # ขยายเนื้อหาถ้าจำเป็น (เหมือนเดิม)
                if hasattr(tweet, 'note_tweet') and tweet.note_tweet:
                    if hasattr(tweet.note_tweet, 'text'):
                        content = tweet.note_tweet.text
                        was_expanded = True  
                        logger.info(f"✅ Used note_tweet full text for {tweet.id}: {len(content)} chars")
                
                elif self.is_truncated_tweet(content):
                    logger.info(f"🔍 Tweet {tweet.id} appears truncated, trying to get full content...")
                    account_info = self.get_best_available_account()
                    account = account_info['account']
                    client = self.create_x_client(account)
                
                    full_content = await self.get_note_tweet_content(client, tweet.id, account_id)
                
                    if full_content and len(full_content) > len(content):
                        content = full_content
                        was_expanded = True  
                        logger.info(f"✅ Retrieved full content: {len(content)} chars (was {len(original_content)})")
                    else:
                        logger.info(f"ℹ️ Using available text for tweet {tweet.id} ({len(content)} chars)")
                else:
                    logger.info(f"✅ Tweet {tweet.id} appears complete: {len(content)} chars")
                
                tweet_url = f"https://twitter.com/{self.target_username}/status/{tweet.id}"
            
                # จัดการ media (เหมือนเดิม)
                media_urls = []
                if includes and 'media' in includes and hasattr(tweet, 'attachments') and tweet.attachments:
                    if 'media_keys' in tweet.attachments:
                        for media_key in tweet.attachments['media_keys']:
                            for media in includes['media']:
                                if media.media_key == media_key:
                                    if media.type == 'photo' and hasattr(media, 'url'):
                                        media_urls.append(media.url)
                                    elif media.type == 'video' and hasattr(media, 'preview_image_url'):
                                        media_urls.append(media.preview_image_url)
                
                # ตรวจสอบ duplicate
                content_hash = self.generate_content_hash(content, media_urls)
                if content_hash in self.processed_content_hashes:
                    logger.info(f"Skipping duplicate content for tweet {tweet.id}")
                    return False

                should_skip, skip_reason = self.should_skip_post(content)
                if should_skip:
                    logger.info(f"🚫 Skipping tweet {tweet.id} - Reason: {skip_reason}")
                    logger.info(f"📝 Content preview: '{content[:100]}...'")
    
                    # บันทึกลงฐานข้อมูลว่าเราประมวลผลแล้ว (เพื่อไม่ให้เช็คซ้ำ) แต่ไม่ส่ง Telegram
                    self.save_processed_tweet(
                        tweet.id, content, f"[SKIPPED-{skip_reason.upper()}] {content[:100]}", 
                        tweet.created_at, tweet_url, account_id, content_hash, 
                        tweet.conversation_id, False
                    )
                    return True  # return True เพราะประมวลผลเสร็จแล้ว (แค่ไม่ส่ง)
                
                logger.info(f"✅ Tweet {tweet.id} passed content filter, proceeding to translate and send")
                
                # แปลภาษา
                translated = await self.translate_text(content)
                thai_time = self.get_thai_time(tweet.created_at)
                
                # จัดรูปแบบข้อความตาม interaction type ใหม่
                message = self.format_message_by_interaction_type(
                    tweet, translated, thai_time, tweet_url, interaction_type, target
                )
    
                # ส่งข้อความ
                await self.send_telegram_message(message, media_urls)
            
                # บันทึกลงฐานข้อมูล
                self.save_processed_tweet(
                    tweet.id, content, translated, tweet.created_at,
                    tweet_url, account_id, content_hash, tweet.conversation_id, False
                )
    
                logger.info(f"✅ Processed {interaction_type} tweet {tweet.id}")
                return True
            finally:
                self.unmark_processing(tweet.id)
        except Exception as e:
            logger.error(f"Process tweet error: {e}")
            self.unmark_processing(tweet.id)
            return False
    
    async def cleanup_db(self):
        """Clean old tweets"""
        try:
            with self.db_lock:
                conn = sqlite3.connect('bot_data.db')
                cursor = conn.cursor()
                
                cutoff = datetime.now() - timedelta(days=7)
                cursor.execute('DELETE FROM processed_tweets WHERE processed_at < ?', (cutoff,))
                
                deleted = cursor.rowcount
                conn.commit()
                conn.close()
                
                if deleted > 0:
                    logger.info(f"Cleaned {deleted} old tweets")
                
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

    async def keep_alive_ping(self):
        """Ping ตัวเองเพื่อไม่ให้ Render sleep (Free tier)"""
        try:
            port = int(os.getenv('PORT', 8080))
            service_url = os.getenv('RENDER_EXTERNAL_URL', f'http://localhost:{port}')
            
            async with aiohttp.ClientSession() as session:
                await session.get(f'{service_url}/health')
            logger.info("Keep-alive ping successful")
        except Exception as e:
            logger.error(f"Keep-alive ping failed: {e}")
    
    def cleanup_memory(self):
        """Cleanup memory periodically"""
        try:
            if len(self.translation_cache) > 100:
                sorted_items = sorted(self.translation_cache.items(), key=lambda x: hash(x[0]))
                self.translation_cache = dict(sorted_items[-50:])
                
            if len(self.processed_tweets) > 1000:
                sorted_tweets = sorted(self.processed_tweets)
                self.processed_tweets = set(sorted_tweets[-500:])
                
            if len(self.processed_content_hashes) > 2000:
                sorted_hashes = sorted(self.processed_content_hashes)
                self.processed_content_hashes = set(sorted_hashes[-1000:])
                
            logger.info("Memory cleanup completed")
        
        except Exception as e:
            logger.error(f"Memory cleanup error: {e}")
    
    async def health_check(self, request):
        """Health check endpoint with detailed account info"""
        current_time = time.time()

        try:
            with open(self.startup_file, 'r') as f:
                startup_time = float(f.read().strip())
            uptime_seconds = int(current_time - startup_time)
        except:
            uptime_seconds = 0
        
        time_slot = int(current_time // (20 * 60))
        preferred_index = time_slot % len(self.x_accounts)
        
        stats_summary = {}
        for i, account in enumerate(self.x_accounts):
            acc_id = account['id']
            stats = self.account_stats[acc_id]
            
            success_rate = 0
            if stats['api_calls'] > 0:
                success_rate = round((stats['successful_calls'] / stats['api_calls']) * 100, 1)
            
            is_rate_limited = stats['rate_limited_until'] > current_time
            rate_limit_remaining = max(0, int(stats['rate_limited_until'] - current_time))
            
            stats_summary[f"account_{i+1}"] = {
                'api_calls': stats['api_calls'],
                'success_rate': f"{success_rate}%",
                'consecutive_failures': stats['consecutive_failures'],
                'rate_limited': is_rate_limited,
                'rate_limit_remaining_seconds': rate_limit_remaining,
                'is_current': i == self.current_account_index,
                'is_preferred_by_time': i == preferred_index,
                'last_used': datetime.fromtimestamp(stats['last_used']).strftime('%H:%M:%S') if stats['last_used'] > 0 else 'Never'
            }
        
        return web.json_response({
            'status': 'OK',
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'uptime_seconds': uptime_seconds,
            'uptime_formatted': f"{uptime_seconds//3600}h {(uptime_seconds%3600)//60}m",
            'platform': 'Render.com',
            'genuine_startup': self.is_genuine_startup,
            'current_account': f"Account {self.current_account_index + 1}",
            'preferred_account_by_time': f"Account {preferred_index + 1}",
            'total_accounts': len(self.x_accounts),
            'processed_tweets': len(self.processed_tweets),
            'target_username': self.target_username,
            'rotation_interval': '20 minutes',
            'account_details': stats_summary
        })
    
    async def start_web_server(self):
        """Start web server"""
        app = web.Application()
        app.router.add_get('/', self.health_check)
        app.router.add_get('/health', self.health_check)
        
        port = int(os.getenv('PORT', 8080))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        
        logger.info(f"Web server started on port {port}")
    
    async def start(self):
        """Start bot"""
        try:
            await self.start_web_server()
        
            # if self.is_genuine_startup:
            #    await self.telegram_bot.send_message(
            #        chat_id=self.telegram_chat_id,
            #        text=f"🚀 <b>บอทเริ่มทำงาน</b>\n✅ ติดตาม @{self.target_username}\n\n⏰ {self.get_thai_time()}",
            #        parse_mode='HTML'
            #    )
            # else:
            #    await self.telegram_bot.send_message(
            #        chat_id=self.telegram_chat_id,
            #        text=f"🔄 <b>บอทกลับมาทำงาน</b> (Render restart)\n⏰ {self.get_thai_time()}",
            #        parse_mode='HTML'
            #    )
            
            self.scheduler.add_job(
                self.fetch_tweets,
                IntervalTrigger(minutes=self.fetch_interval),
                id='fetch_tweets'
            )
            
            self.scheduler.add_job(
                self.cleanup_db,
                IntervalTrigger(days=1),
                id='cleanup'
            )

            self.scheduler.add_job(
                self.cleanup_memory,
                IntervalTrigger(minutes=self.cleanup_interval),
                id='cleanup_memory'
            )

            if os.getenv('RENDER_SERVICE_TYPE', '').lower() == 'free':
                self.scheduler.add_job(
                    self.keep_alive_ping,
                    IntervalTrigger(minutes=self.ping_interval),
                    id='keep_alive'
                )
                logger.info("Keep-alive enabled for Render Free tier")
            
            self.scheduler.start()
            
            await self.fetch_tweets()
            
            logger.info("Bot started successfully")
            
            while True:
                await asyncio.sleep(60)
                
        except Exception as e:
            logger.error(f"Startup error: {e}")
    
    async def stop(self):
        """Stop bot"""
        try:
            self.scheduler.shutdown()
            # await self.telegram_bot.send_message(
            #    chat_id=self.telegram_chat_id,
            #    text=f"🛑 <b>บอทหยุดทำงาน</b>\n\n⏰ {self.get_thai_time()}",
            #    parse_mode='HTML'
            # )
            logger.info("Bot stopped")
        except Exception as e:
            logger.error(f"Stop error: {e}")

async def main():
    """Main function"""
    bot = XTelegramBot()
    
    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("Bot interrupted")
        await bot.stop()
    except Exception as e:
        logger.error(f"Main error: {e}")
        await bot.stop()

if __name__ == "__main__":
    asyncio.run(main())
