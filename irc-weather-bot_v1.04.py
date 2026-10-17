import asyncio
import aiohttp
import logging
import time
import random
import signal
import json
import sys
import os
import ssl
import base64
from collections import defaultdict, deque
from urllib.parse import quote
import argparse
import logging.handlers
from cachetools import TTLCache
import re
import contextlib

# Configure logging with adjustable levels and log rotation
parser = argparse.ArgumentParser(description='IRC Weather Bot')
parser.add_argument('--log-level', default='INFO', help='Set the logging level (DEBUG, INFO, WARNING, ERROR)')
parser.add_argument('--config', default='config.json', help='Path to the configuration file')
args = parser.parse_args()

logger = logging.getLogger('IrcBot')
logger.setLevel(getattr(logging, args.log_level.upper()))
handler = logging.handlers.TimedRotatingFileHandler('bot.log', when='midnight', backupCount=7)
formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# Load configuration from config.json or specified config file
try:
    with open(args.config, 'r') as config_file:
        config = json.load(config_file)
except FileNotFoundError:
    logger.error(f"Configuration file {args.config} not found.")
    sys.exit(1)
except json.JSONDecodeError as e:
    logger.error(f"Error parsing configuration file: {e}")
    sys.exit(1)

# Validate required configurations
REQUIRED_CONFIG_KEYS = [
    'HOST', 'PORT', 'USER', 'CHANNELS', 'API_KEY', 'USERNAME', 'PASSWORD',
    'TRIGGER', 'RATE_LIMIT', 'RATE_LIMIT_TIME', 'GLOBAL_RATE_LIMIT', 'GLOBAL_RATE_LIMIT_TIME',
    'WAREZ_TRIGGER', 'WAREZ_FILE', 'PING_INTERVAL', 'PING_TIMEOUT',
    'STAB_TRIGGER', 'STAB_FILE'
]
for key in REQUIRED_CONFIG_KEYS:
    if key not in config:
        logger.error(f"Missing required configuration: {key}")
        sys.exit(1)

HOST = config['HOST']
PORT = config['PORT']
USER = config['USER']
CHANNELS = config['CHANNELS']
TRIGGER = config['TRIGGER']
RATE_LIMIT = config['RATE_LIMIT']
RATE_LIMIT_TIME = config['RATE_LIMIT_TIME']
GLOBAL_RATE_LIMIT = config['GLOBAL_RATE_LIMIT']
GLOBAL_RATE_LIMIT_TIME = config['GLOBAL_RATE_LIMIT_TIME']
WAREZ_TRIGGER = config['WAREZ_TRIGGER']
WAREZ_FILE = config['WAREZ_FILE']
PING_INTERVAL = config['PING_INTERVAL']
PING_TIMEOUT = config['PING_TIMEOUT']

API_KEY = config['API_KEY']
USERNAME = config['USERNAME']
PASSWORD = config['PASSWORD']

STAB_TRIGGER = config['STAB_TRIGGER']
STAB_FILE = config['STAB_FILE']

# Optional abuse-prevention controls (per channel and sender).
ENTERTAINMENT_COOLDOWN = config.get('ENTERTAINMENT_COOLDOWN', 12)
WEATHER_CONCURRENCY = config.get('WEATHER_CONCURRENCY', 3)

def validate_config():
    for key in ('HOST', 'USER', 'USERNAME', 'PASSWORD', 'API_KEY', 'TRIGGER',
                'WAREZ_TRIGGER', 'STAB_TRIGGER', 'WAREZ_FILE', 'STAB_FILE'):
        if not isinstance(config[key], str) or not config[key].strip():
            raise ValueError(f'{key} must be a non-empty string')
    if not isinstance(CHANNELS, list) or not CHANNELS or any(
        not isinstance(ch, str) or not ch.startswith(('#', '&')) or
        any(c in ch for c in ' \r\n,') for ch in CHANNELS
    ):
        raise ValueError('CHANNELS must contain valid channel names')
    for key, value, minimum in (
        ('PORT', PORT, 1), ('RATE_LIMIT', RATE_LIMIT, 1),
        ('RATE_LIMIT_TIME', RATE_LIMIT_TIME, 1),
        ('GLOBAL_RATE_LIMIT', GLOBAL_RATE_LIMIT, 1),
        ('GLOBAL_RATE_LIMIT_TIME', GLOBAL_RATE_LIMIT_TIME, 1),
        ('PING_INTERVAL', PING_INTERVAL, 1), ('PING_TIMEOUT', PING_TIMEOUT, 1),
        ('ENTERTAINMENT_COOLDOWN', ENTERTAINMENT_COOLDOWN, 0),
        ('WEATHER_CONCURRENCY', WEATHER_CONCURRENCY, 1),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
            raise ValueError(f'{key} must be a number >= {minimum}')
    if not isinstance(PORT, int) or PORT > 65535:
        raise ValueError('PORT must be an integer between 1 and 65535')
    if not isinstance(RATE_LIMIT, int) or not isinstance(GLOBAL_RATE_LIMIT, int):
        raise ValueError('Rate limit counts must be integers')
    if not isinstance(WEATHER_CONCURRENCY, int):
        raise ValueError('WEATHER_CONCURRENCY must be an integer')

try:
    validate_config()
except ValueError as exc:
    logger.error('Invalid configuration: %s', exc)
    sys.exit(1)

# IRC account names authorized to run channel administration commands.
# No admin access is granted unless explicitly configured.
admin_accounts = config.get('ADMIN_ACCOUNTS', [])
if not isinstance(admin_accounts, list) or any(
    not isinstance(account, str) or not account.strip() or account.strip() == '*'
    for account in admin_accounts
):
    logger.error("ADMIN_ACCOUNTS must be a list of non-empty IRC account names (wildcards are not allowed).")
    sys.exit(1)
ADMIN_ACCOUNTS = {account.casefold() for account in admin_accounts}
if not ADMIN_ACCOUNTS:
    logger.warning("ADMIN_ACCOUNTS is empty; all channel administration commands are disabled.")

def irc_protocol_safe(value):
    """Reject IRC line-breaking controls without changing allowed punctuation."""
    if not isinstance(value, str) or any(c in value for c in ('\r', '\n', '\x00')):
        raise ValueError('Invalid IRC protocol text')
    return value


def irc_target_safe(target):
    """Validate the complete IRC target token, not merely the message body."""
    irc_protocol_safe(target)
    if (not target or len(target.encode('utf-8')) > 100 or
            any(ord(c) < 33 or ord(c) == 127 or c in ',:' for c in target)):
        raise ValueError('Invalid IRC target')
    return target


def sanitize_input(user_input):
    """Apply the existing conservative display policy and 400-char limit."""
    sanitized = user_input.replace('\r', '').replace('\n', '').replace('\0', '')
    # Preserve ordinary IRC styling while filtering other controls.
    sanitized = re.sub(r'[\x00\x01\x04-\x0E\x10-\x15\x17-\x1C\x1E\x7F]', '', sanitized)
    sanitized = sanitized.strip()[:400]
    return re.sub(r'[^\w\s,.\-:|°%/()"\x02\x03\x0F\x16\x1D\x1F]', '', sanitized)

class ReconnectNeeded(Exception):
    """Custom exception to signal that a reconnection is needed."""
    pass

class ResponseFile:
    """Reload responses when the backing file changes."""

    def __init__(self, file_path, no_repeat=False, label="responses"):
        self.file_path = file_path
        self.no_repeat = no_repeat
        self.label = label
        self.last_modified_time = None
        self.responses = []
        self.available_responses = []
        self.load_responses()

    def load_responses(self):
        try:
            modified = os.path.getmtime(self.file_path)
            if modified == self.last_modified_time:
                return

            with open(self.file_path, 'r', encoding='utf-8') as file:
                updated = [line.strip() for line in file if line.strip()]
            if not updated:
                logger.warning('Empty %s file; preserving previous responses.', self.label)
                return
            self.responses = updated
            self.last_modified_time = modified
            self.available_responses.clear()
            logger.info(f"Reloaded {self.label} from {self.file_path}.")
        except FileNotFoundError:
            logger.error(f"Response file {self.file_path} not found.")
            # Keep the last known-good data if the file is temporarily unavailable.
        except Exception as e:
            logger.error(f"Error loading {self.label}: {e}")
            # Keep the last known-good data if the file cannot be decoded.

    def get_random_response(self):
        self.load_responses()
        if not self.responses:
            return f"No {self.label} available."

        if not self.no_repeat:
            return random.choice(self.responses)

        if not self.available_responses:
            self.available_responses = self.responses.copy()
            random.shuffle(self.available_responses)
            logger.debug(f"Shuffled {self.label} for a new cycle.")

        return self.available_responses.pop()


class IrcBot:
    """An IRC bot that provides weather information and responds to specific triggers."""

    def __init__(self):
        self.last_requests = defaultdict(lambda: deque(maxlen=RATE_LIMIT))
        self.global_request_times = deque(maxlen=GLOBAL_RATE_LIMIT)
        self.warez_responder = ResponseFile(WAREZ_FILE, label="warez responses")
        self.stab_responder = ResponseFile(STAB_FILE, no_repeat=True, label="stab responses")
        self.last_pong_time = time.monotonic()
        self.pending_ping = None
        self.ping_sent_at = None
        self.reader = None
        self.writer = None
        self.lock = asyncio.Lock()
        self.writer_lock = asyncio.Lock()
        self.weather_cache = TTLCache(maxsize=100, ttl=300)
        self.running = True
        self.message_semaphore = asyncio.Semaphore(1)
        self.current_nick = USER
        self.reconnect_lock = asyncio.Lock()
        self.weather_semaphore = asyncio.Semaphore(WEATHER_CONCURRENCY)
        self.weather_tasks = set()
        self.entertainment_last = {}
        self.next_rejoin = {}
        self.reconnect_count = 0
        self.last_api_success = None
        # Store channel users in lowercase to enable case-insensitive checks
        self.channel_users = defaultdict(set)
        self.http_session = None
        self.started_at = time.monotonic()
        self.stats = {"weather_requests": 0, "api_requests": 0,
                      "cache_hits": 0, "weather_errors": 0, "rate_limited": 0}
        self.account_tag_enabled = False

    def launch_weather(self, user, channel, location, forecast):
        if len(self.weather_tasks) >= WEATHER_CONCURRENCY * 4:
            self.stats['rate_limited'] += 1
            return
        task = asyncio.create_task(self.handle_weather_command(user, channel, location, forecast))
        self.weather_tasks.add(task)
        task.add_done_callback(self.weather_tasks.discard)

    def entertainment_allowed(self, user, channel, command):
        key = (user.casefold(), channel.casefold(), command)
        now = time.monotonic()
        if now - self.entertainment_last.get(key, float('-inf')) < ENTERTAINMENT_COOLDOWN:
            return False
        self.entertainment_last[key] = now
        return True

    async def connect(self):
        """Establish a TLS connection to the IRC server and authenticate with SASL."""
        try:
            ssl_context = ssl.create_default_context()
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(
                    HOST,
                    PORT,
                    ssl=ssl_context,
                    server_hostname=HOST
                ),
                timeout=30
            )
            await self.register()
            logger.info(f"Connected to IRC server as {self.current_nick}.")
            await self.authenticate_sasl()

            if not await self.wait_for_registration():
                await self.reclaim_nickname()

            logger.info("Authenticated with SASL successfully.")
            await self.join_channels()
        except Exception as e:
            logger.error(f"Failed to connect to IRC: {e}")
            raise

    async def send_raw(self, line):
        """Send one raw IRC protocol line."""
        if self.writer is None:
            raise ConnectionError("No active IRC connection.")

        irc_protocol_safe(line)
        if len((line + '\r\n').encode('utf-8')) > 512:
            raise ValueError('IRC protocol line exceeds 512 bytes')
        async with self.writer_lock:
            self.writer.write(f"{line}\r\n".encode('utf-8'))
            await self.writer.drain()

    async def register(self):
        """Begin IRC registration and request SASL capability."""
        if self.writer is None:
            logger.error("Cannot register, no active connection.")
            return

        self.current_nick = USER
        self.account_tag_enabled = False
        await self.send_raw("CAP LS 302")
        await self.send_raw(f"NICK {irc_target_safe(self.current_nick)}")
        irc_target_safe(USERNAME)
        await self.send_raw(f"USER {USERNAME} 0 * :{USERNAME}")
        logger.info("Sent CAP LS, NICK, and USER commands.")

    async def authenticate_sasl(self):
        """Authenticate using IRCv3 SASL PLAIN over the existing TLS connection."""
        if self.writer is None:
            raise ReconnectNeeded("Cannot authenticate with SASL without an active connection.")

        sasl_requested = False
        sasl_started = False
        advertised_capabilities = set()

        while True:
            line = await self.read_line_with_timeout(timeout=30)
            if line is None:
                raise ReconnectNeeded("Connection lost during SASL authentication.")

            logger.debug('Received IRC message during SASL negotiation')
            prefix, command, params = self.parse_irc_message(line)

            if command == 'PING':
                await self.handle_ping(params)
                continue

            if command == 'CAP' and len(params) >= 2:
                subcommand = params[1].upper()

                if subcommand == 'LS':
                    # IRCv3 CAP LS can span multiple replies, marked with '*'.
                    advertised_capabilities.update(params[-1].lower().split())
                    if len(params) >= 3 and params[-2] == '*':
                        continue
                    if not any(cap == 'sasl' or cap.startswith('sasl=') for cap in advertised_capabilities):
                        raise RuntimeError("IRC server does not advertise SASL capability.")
                    if 'account-tag' not in advertised_capabilities:
                        raise RuntimeError("IRC server lacks account-tag; admin commands require authenticated account tags.")

                    await self.send_raw("CAP REQ :sasl account-tag")
                    sasl_requested = True
                    logger.info("Requested SASL capability.")
                    continue

                if subcommand == 'ACK' and sasl_requested:
                    acknowledged = set(params[-1].lower().split())
                    if 'account-tag' not in acknowledged or 'sasl' not in acknowledged:
                        raise RuntimeError("IRC server did not acknowledge both sasl and account-tag.")
                    self.account_tag_enabled = True
                    await self.send_raw("AUTHENTICATE PLAIN")
                    sasl_started = True
                    logger.info("SASL capability acknowledged; starting PLAIN authentication.")
                    continue

                if subcommand == 'NAK':
                    raise RuntimeError("IRC server rejected SASL capability request.")

            if command == 'AUTHENTICATE' and sasl_started and params and params[0] == '+':
                auth_bytes = f"{USERNAME}\0{USERNAME}\0{PASSWORD}".encode('utf-8')
                payload = base64.b64encode(auth_bytes).decode('ascii')

                # IRC SASL AUTHENTICATE payloads are sent in chunks of at most
                # 400 bytes. If the encoded payload is an exact multiple of 400,
                # terminate it with an additional AUTHENTICATE +.
                chunks = [payload[i:i + 400] for i in range(0, len(payload), 400)]

                for chunk in chunks:
                    await self.send_raw(f"AUTHENTICATE {chunk}")
                if payload and len(payload) % 400 == 0:
                    await self.send_raw("AUTHENTICATE +")

                logger.info("Sent SASL PLAIN credentials.")
                continue

            if command == '903':
                await self.send_raw("CAP END")

                logger.info("SASL authentication successful.")
                return

            if command in {'904', '905', '906', '907'}:
                raise RuntimeError(f"SASL authentication failed with numeric {command}.")

    async def read_line_with_timeout(self, timeout=300):
        """Read one IRC line with a timeout."""
        line = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
        if not line:
            return None
        if len(line) > 8192:
            raise ReconnectNeeded('Oversized IRC input line')
        return line.decode('utf-8', errors='replace').rstrip('\r\n')

    async def wait_for_registration(self):
        """Wait for registration to finish; return False if the nick is in use."""
        while True:
            line = await self.read_line_with_timeout()
            if line is None:
                raise ReconnectNeeded("Connection lost during registration.")

            logger.debug('Received IRC registration message')
            _, command, params = self.parse_irc_message(line)

            if command == '001':
                logger.info("Received welcome message from server.")
            elif command in {'376', '422'}:
                logger.info("End of MOTD received.")
                return True
            elif command == '433':
                logger.warning(f"Nickname {self.current_nick} is already in use.")
                return False
            elif command == 'PING':
                await self.handle_ping(params)
            else:
                logger.debug(f"Ignoring message during registration: {line}")

    def parse_irc_message(self, message):
        """Parse an IRC message into its prefix, command, and parameters."""
        try:
            prefix = ''
            trailing = []
            if not message:
                return None, None, None
            if message.startswith(':'):
                prefix, message = message[1:].split(' ', 1)
            if ' :' in message:
                message, trailing = message.split(' :', 1)
                args = message.split()
                args.append(trailing)
            else:
                args = message.split()
            command = args.pop(0)
            return prefix, command, args
        except ValueError as e:
            logger.error(f"Failed to parse IRC message: {message} Error: {e}")
            return None, None, None

    async def handle_ping(self, params):
        """Respond to server PING messages."""
        if self.writer is None:
            logger.error("Cannot respond to PING, no active connection.")
            return
        if not params:
            return
        await self.send_raw(f"PONG :{params[0]}")
        logger.debug("Responded to PING with PONG.")

    async def reconnect(self):
        """Compatibility entry point; connection lifecycle is owned by run()."""
        raise ReconnectNeeded('Reconnect requested')

    async def close_connection(self):
        """Close the existing IRC connection."""
        if self.writer:
            try:
                await self.send_raw("QUIT :Reconnecting...")
            except Exception as e:
                logger.error(f"Error sending QUIT command: {e}")
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception as e:
                logger.error(f"Error closing writer: {e}")
        self.writer = None
        self.reader = None
        self.pending_ping = None
        self.ping_sent_at = None
        self.channel_users.clear()

    async def join_channels(self):
        """Send JOIN commands for all channels."""
        if self.writer is None:
            logger.error("Cannot join channels, no active connection.")
            return

        for channel in CHANNELS:
            logger.info(f"Joining channel {channel}")
            await self.send_raw(f"JOIN {irc_target_safe(channel)}")
            await asyncio.sleep(1)

    async def handle_privmsg(self, prefix, params, account=None):
        """Handle PRIVMSG commands."""
        try:
            if not prefix or not params or len(params) < 2:
                return
            user = prefix.split('!')[0]
            channel = params[0]
            message = params[1].strip()
            message_lower = message.lower()

            # Admin commands are channel-only and require an authenticated IRC
            # account listed in ADMIN_ACCOUNTS. Account tags come from the IRC
            # server, not the sender's nickname or hostmask.
            if message_lower in ('##status', '##uptime', '##stats'):
                if not channel.startswith(('#', '&')):
                    return
                if self.account_tag_enabled and account and account.casefold() in ADMIN_ACCOUNTS:
                    await self.handle_admin_command(channel, message_lower)
                else:
                    logger.warning('Rejected unauthorized admin command %s', message_lower)
                return

            if user.lower() == self.current_nick.lower():
                logger.debug("Received a message from self; ignoring to prevent loops.")
                return

            # Check for CTCP messages
            if message.startswith('\x01') and message.endswith('\x01'):
                ctcp_command = message.strip('\x01')
                if ctcp_command.upper() == 'VERSION':
                    await self.handle_ctcp_version(user)
                else:
                    logger.debug(f"Received unsupported CTCP command from {user}: {ctcp_command}")
                return

            if channel.casefold() == self.current_nick.casefold():
                await self.handle_private_message(user, message)
            else:
                if message_lower.startswith(TRIGGER.lower()):
                    args = message[len(TRIGGER):].strip()
                    args = sanitize_input(args)
                    forecast = False
                    if '--forecast' in args:
                        args = args.replace('--forecast', '').strip()
                        forecast = True
                    self.launch_weather(user, channel, args, forecast)
                elif WAREZ_TRIGGER.lower() in message_lower:
                    if self.entertainment_allowed(user, channel, 'warez'):
                        await self.handle_warez_command(channel)
                elif message_lower.startswith(STAB_TRIGGER.lower()):
                    # Extract username after the trigger
                    stab_target = message[len(STAB_TRIGGER):].strip()
                    if stab_target:
                        stab_target = sanitize_input(stab_target)
                        if self.entertainment_allowed(user, channel, 'stab'):
                            await self.handle_stab_command(channel, stab_target)
        except Exception as e:
            logger.exception(f"Exception in handle_privmsg: {e}")

    @staticmethod
    def format_uptime(seconds):
        total = int(seconds)
        days, remainder = divmod(total, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{days}d {hours}h {minutes}m {seconds}s"

    async def handle_admin_command(self, channel, command):
        uptime = self.format_uptime(time.monotonic() - self.started_at)
        if command == '##uptime':
            reply = f"hftb uptime: {uptime}"
        elif command == '##status':
            connected = self.writer is not None and not self.writer.is_closing()
            reply = (f"hftb status: {'connected' if connected else 'disconnected'} | "
                     f"Nick: {self.current_nick} | Configured channels: {len(CHANNELS)} | "
                     f"Uptime: {uptime}")
        else:
            reply = (f"hftb stats: Weather requests: {self.stats['weather_requests']} | "
                     f"API requests: {self.stats['api_requests']} | "
                     f"Cache hits: {self.stats['cache_hits']} | "
                     f"Weather errors: {self.stats['weather_errors']} | "
                     f"Cache entries: {len(self.weather_cache)} | "
                     f"Throttled: {self.stats['rate_limited']} | "
                     f"Reconnects: {self.reconnect_count}")
        await self.send_message(channel, reply)
        logger.info("Admin command %s executed in %s", command, channel)

    async def handle_ctcp_version(self, user):
        """Respond to a CTCP VERSION request."""
        version_reply = "Atari 800 MOS 6502 @ 1.8MHz"
        ctcp_response = f"\x01VERSION {version_reply}\x01"
        await self.send_notice(user, ctcp_response)
        logger.info(f"Responded to CTCP VERSION request from {user}.")

    async def send_notice(self, target, message):
        """Send a NOTICE to the specified target."""
        if self.writer is None:
            logger.error("Cannot send NOTICE, no active connection.")
            return
        irc_target_safe(target)
        irc_protocol_safe(message)
        await self.send_raw(f"NOTICE {target} :{message}")
        logger.debug(f"Sent NOTICE to {target}: {message}")

    async def handle_stab_command(self, channel, target_user):
        """Respond to the stab trigger with the specified target user, case-insensitive."""
        # Convert target_user to lowercase for comparison
        target_user_lower = target_user.lower()
        # Check if the target_user is in the channel before responding
        if target_user_lower not in self.channel_users[channel.casefold()]:
            message = f"{target_user} is not currently in {channel}."
            await self.send_message(channel, message)
            logger.info(f"User {target_user} not found in {channel}, no stab response sent.")
            return

        response_line = self.stab_responder.get_random_response()
        message = f"hftb stabs {target_user} {response_line}"
        await self.send_message(channel, message)
        logger.info(f"Sent stab response to {channel} targeting {target_user}.")

    async def handle_private_message(self, user, message):
        """Handle private messages sent to the bot."""
        response = "I'm currently not set up to handle private messages."
        await self.send_message(user, response)
        logger.info(f"Sent private message response to {user}.")

    async def handle_weather_command(self, user, channel, location, forecast=False):
        """Process the weather command and send weather information."""
        current_time = time.monotonic()
        async with self.lock:
            # Reject user requests before touching global counters.
            request_times = self.last_requests[user.casefold()]
            while request_times and current_time - request_times[0] > RATE_LIMIT_TIME:
                request_times.popleft()
            if len(request_times) >= RATE_LIMIT:
                self.stats['rate_limited'] += 1
                return
            while self.global_request_times and current_time - self.global_request_times[0] > GLOBAL_RATE_LIMIT_TIME:
                self.global_request_times.popleft()
            if len(self.global_request_times) >= GLOBAL_RATE_LIMIT:
                self.stats['rate_limited'] += 1
                return
            request_times.append(current_time)
            self.global_request_times.append(current_time)

        self.stats['weather_requests'] += 1
        async with self.weather_semaphore:
            await self.fetch_and_send_weather(channel, location, user, forecast)

    async def fetch_and_send_weather(self, channel, location, user, forecast=False):
        """Fetch weather data and send it to the channel."""
        try:
            cache_key = f"{location.lower()}_{forecast}"
            if cache_key in self.weather_cache:
                data = self.weather_cache[cache_key]
                self.stats['cache_hits'] += 1
                logger.info(f"Using cached weather data for {location}.")
            else:
                days = 2 if forecast else 1
                url = f"https://api.weatherapi.com/v1/forecast.json?key={API_KEY}&q={quote(location)}&days={days}&aqi=no&alerts=no"
                if self.http_session is None or self.http_session.closed:
                    timeout = aiohttp.ClientTimeout(total=10)
                    self.http_session = aiohttp.ClientSession(timeout=timeout)

                self.stats['api_requests'] += 1
                async with self.http_session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self.last_api_success = time.monotonic()
                    elif resp.status == 401:
                        self.stats['weather_errors'] += 1
                        logger.error("Unauthorized access. Check your API key.")
                        error_msg = "Unauthorized access to weather API. Please check the API key."
                        await self.send_message(channel, error_msg)
                        return
                    elif resp.status == 404:
                        self.stats['weather_errors'] += 1
                        logger.error(f"Location '{location}' not found.")
                        error_msg = f"Location '{location}' not found."
                        await self.send_message(channel, error_msg)
                        return
                    else:
                        self.stats['weather_errors'] += 1
                        logger.error(f"HTTP error {resp.status} when fetching weather data for {location}.")
                        error_msg = f"Error fetching weather information for {location}."
                        await self.send_message(channel, error_msg)
                        return
                self.weather_cache[cache_key] = data

            # Extract and format the weather data
            weather_message = self.format_weather_data(data, forecast)
            await self.send_message(channel, weather_message)
            logger.info(f"Sent weather info to {channel} for location '{location}' requested by user '{user}'.")
        except asyncio.TimeoutError:
            self.stats['weather_errors'] += 1
            logger.error(f"Weather API request for {location} timed out.")
            error_msg = "Weather API request timed out."
            await self.send_message(channel, error_msg)
        except aiohttp.ClientError as e:
            self.stats['weather_errors'] += 1
            logger.error(f"HTTP error when fetching weather data for {location}: {e}")
            error_msg = f"Error fetching weather information for {location}."
            await self.send_message(channel, error_msg)
        except KeyError as e:
            self.stats['weather_errors'] += 1
            logger.error(f"Missing expected data in API response for {location}: {e}")
            error_msg = "Received unexpected data from weather API."
            await self.send_message(channel, error_msg)
        except Exception as e:
            self.stats['weather_errors'] += 1
            logger.exception(f"Exception in fetch_and_send_weather: {e}")
            error_msg = f"Error processing weather information for {location}."
            await self.send_message(channel, error_msg)

    def format_weather_data(self, data, forecast=False):
        """Format the weather data into a message string."""
        try:
            location_info = data.get('location', {})
            current = data.get('current', {})
            forecast_days = data.get('forecast', {}).get('forecastday', [])

            # Extract location details
            name = location_info.get('name', 'Unknown')
            region = location_info.get('region', '')
            country = location_info.get('country', '').replace("United States of America", "USA")

            if forecast:
                # Format forecast for each day
                messages = []
                for day_data in forecast_days:
                    date = day_data.get('date', 'N/A')
                    day = day_data.get('day', {})
                    condition = day.get('condition', {}).get('text', 'N/A')
                    avgtemp_f = day.get('avgtemp_f', 'N/A')
                    avgtemp_c = day.get('avgtemp_c', 'N/A')
                    maxtemp_f = day.get('maxtemp_f', 'N/A')
                    maxtemp_c = day.get('maxtemp_c', 'N/A')
                    mintemp_f = day.get('mintemp_f', 'N/A')
                    mintemp_c = day.get('mintemp_c', 'N/A')
                    daily_chance_of_rain = day.get('daily_chance_of_rain', 'N/A')
                    daily_chance_of_snow = day.get('daily_chance_of_snow', 'N/A')
                    totalsnow_cm = day.get('totalsnow_cm', 0.0)
                    try:
                        totalsnow_cm = float(totalsnow_cm)
                    except (ValueError, TypeError):
                        totalsnow_cm = 0.0
                    totalsnow_in = totalsnow_cm / 2.54 if totalsnow_cm > 0 else 0.0
                    snow_message = f"Total Snow: {totalsnow_cm} cm / {totalsnow_in:.2f} in"

                    message = (
                        f"Forecast for {date} | "
                        f"Condition: {condition} | "
                        f"Avg Temp: {avgtemp_f}°F / {avgtemp_c}°C | "
                        f"Min Temp: {mintemp_f}°F / {mintemp_c}°C | "
                        f"Max Temp: {maxtemp_f}°F / {maxtemp_c}°C | "
                        f"Chance of Rain: {daily_chance_of_rain}% | "
                        f"Chance of Snow: {daily_chance_of_snow}% | "
                        f"{snow_message}"
                    )
                    messages.append(message)
                weather_message = f"{name}, {region}, {country} | " + ' | '.join(messages)
            else:
                # Extract current weather details
                temp_f = current.get('temp_f', 'N/A')
                temp_c = current.get('temp_c', 'N/A')
                condition_text = current.get('condition', {}).get('text', 'N/A')
                wind_mph = current.get('wind_mph', 'N/A')
                wind_kph = current.get('wind_kph', 'N/A')
                wind_degree = current.get('wind_degree', 'N/A')
                wind_dir = current.get('wind_dir', 'N/A')
                gust_mph = current.get('gust_mph', 'N/A')
                gust_kph = current.get('gust_kph', 'N/A')
                precip_mm = current.get('precip_mm', 'N/A')
                precip_in = current.get('precip_in', 'N/A')
                humidity = current.get('humidity', 'N/A')

                # Use the first forecast day for additional data
                forecast_day = forecast_days[0] if forecast_days else {}
                forecast_data = forecast_day.get('day', {})
                astro = forecast_day.get('astro', {})

                mintemp_f = forecast_data.get('mintemp_f', 'N/A')
                mintemp_c = forecast_data.get('mintemp_c', 'N/A')
                maxtemp_f = forecast_data.get('maxtemp_f', 'N/A')
                maxtemp_c = forecast_data.get('maxtemp_c', 'N/A')
                daily_chance_of_rain = forecast_data.get('daily_chance_of_rain', 0)
                daily_chance_of_snow = forecast_data.get('daily_chance_of_snow', 0)
                totalsnow_cm = forecast_data.get('totalsnow_cm', 0.0)
                try:
                    totalsnow_cm = float(totalsnow_cm)
                except (ValueError, TypeError):
                    totalsnow_cm = 0.0
                totalsnow_in = totalsnow_cm / 2.54 if totalsnow_cm > 0 else 0.0
                snow_message = f"Total Snow: {totalsnow_cm} cm / {totalsnow_in:.2f} in"

                moon_phase = astro.get('moon_phase', 'N/A')
                sunrise = astro.get('sunrise', 'N/A')
                sunset = astro.get('sunset', 'N/A')

                weather_message = (
                    f"{name}, {region}, {country} | "
                    f"Current Temp: {temp_f}°F / {temp_c}°C | "
                    f"Min Temp: {mintemp_f}°F / {mintemp_c}°C | "
                    f"Max Temp: {maxtemp_f}°F / {maxtemp_c}°C | "
                    f"Condition: {condition_text} | "
                    f"Humidity: {humidity}% | "
                    f"Wind: {wind_mph} mph / {wind_kph} kph "
                    f"({wind_degree}°, {wind_dir}) | "
                    f"Gusts: {gust_mph} mph / {gust_kph} kph | "
                    f"Precipitation: {precip_in} in / {precip_mm} mm | "
                    f"Moon Phase: {moon_phase} | "
                    f"Sunrise: {sunrise} | Sunset: {sunset} | "
                    f"Chance of Rain: {daily_chance_of_rain}% | "
                    f"Chance of Snow: {daily_chance_of_snow}% | "
                    f"{snow_message}"
                )
            return weather_message
        except Exception as e:
            logger.exception(f"Exception in format_weather_data: {e}")
            return "Error formatting weather data."

    async def handle_warez_command(self, channel):
        """Respond to the warez trigger."""
        response = self.warez_responder.get_random_response()
        await self.send_message(channel, response)
        logger.info(f"Sent warez response to {channel}.")

    async def send_message(self, channel, message):
        """Exactly one IRC message, <=400 characters and within IRC's byte limit."""
        message = sanitize_input(message)
        max_bytes = 512 - len(f"PRIVMSG {channel} :\r\n".encode('utf-8'))
        available = max_bytes
        encoded = message.encode('utf-8')
        if len(encoded) > available:
            message = encoded[:available].decode('utf-8', errors='ignore')
        message = message[:400]
        if not message:
            return
        async with self.message_semaphore:
            await self.send_privmsg(channel, message)

    async def send_privmsg(self, target, message):
        """Send a PRIVMSG to the specified target."""
        if self.writer is None:
            logger.error("Cannot send message, no active connection.")
            return
        irc_target_safe(target)
        irc_protocol_safe(message)
        await self.send_raw(f"PRIVMSG {target} :{message}")

    async def run(self):
        """A single owner of the connection, retrying indefinitely with backoff."""
        delay = 30
        try:
            while self.running:
                try:
                    await self.connect()
                    delay = 30
                    self.last_pong_time = time.monotonic()
                    self.pending_ping = None
                    await self.handle_messages()
                except asyncio.CancelledError:
                    raise
                except (ReconnectNeeded, OSError, asyncio.TimeoutError) as exc:
                    logger.warning('Connection interrupted: %s', exc)
                except Exception:
                    logger.exception('Connection failure')
                finally:
                    for task in list(self.weather_tasks):
                        task.cancel()
                    if self.weather_tasks:
                        await asyncio.gather(*self.weather_tasks, return_exceptions=True)
                    await self.close_connection()
                if self.running:
                    self.reconnect_count += 1
                    pause = min(300, delay + random.uniform(0, delay * 0.2))
                    logger.info('Reconnecting in %.1f seconds', pause)
                    await asyncio.sleep(pause)
                    delay = min(300, delay * 2)
        finally:
            await self.cleanup()

    async def send_ping(self):
        """Track a client-generated PING until its matching PONG arrives."""
        token = f"hftb-{int(time.monotonic() * 1000)}"
        await self.send_raw(f"PING :{token}")
        self.pending_ping = token
        self.ping_sent_at = time.monotonic()

    async def handle_messages(self):
        while self.running:
            try:
                line = await self.read_line_with_timeout(timeout=PING_INTERVAL)
            except asyncio.TimeoutError:
                if self.pending_ping is not None:
                    if time.monotonic() - self.ping_sent_at >= PING_TIMEOUT:
                        raise ReconnectNeeded('No matching PONG within timeout')
                else:
                    await self.send_ping()
                continue
            if line is None:
                raise ReconnectNeeded('IRC connection closed')
            logger.debug('Received IRC line')
            await self.process_line(line)

    @staticmethod
    def extract_message_tags(line):
        """Separate server-provided IRCv3 message tags from an IRC line."""
        if not line.startswith('@'):
            return {}, line
        tag_block, separator, rest = line.partition(' ')
        if not separator:
            return {}, line
        tags = {}
        for entry in tag_block[1:].split(';'):
            name, _, value = entry.partition('=')
            tags[name] = value
        return tags, rest

    async def process_line(self, line):
        """Process a single line from the IRC server."""
        try:
            tags, line = self.extract_message_tags(line)
            prefix, command, params = self.parse_irc_message(line)
            logger.debug('Received IRC command: %s', command)

            if command == 'PING':
                await self.handle_ping(params)
            elif command == 'PONG':
                await self.handle_pong(params)
            elif command == 'NOTICE':
                await self.handle_notice(prefix, params)
            elif command == 'PRIVMSG':
                await self.handle_privmsg(prefix, params, tags.get('account'))
            elif command == 'JOIN':
                await self.handle_join(prefix, params)
            elif command == 'PART':
                await self.handle_part(prefix, params)
            elif command == 'QUIT':
                await self.handle_quit(prefix)
            elif command == '353':
                await self.handle_namereply(params)
            elif command == '366':
                # RPL_ENDOFNAMES, we can ignore or just log
                logger.debug("End of NAMES list received.")
            elif command == 'KICK':
                await self.handle_kick(prefix, params)
            elif command == 'NICK':
                await self.handle_nick(prefix, params)
            elif command == 'ERROR':
                error_message = ' '.join(params)
                logger.error(f"Server error: {error_message}")
                if "closing link" in error_message.lower():
                    logger.warning("Possible netsplit detected. Attempting to reconnect...")
                    raise ReconnectNeeded()
                else:
                    raise ReconnectNeeded()
            else:
                logger.debug('Unhandled IRC command: %s', command)
        except ReconnectNeeded:
            raise
        except Exception as e:
            logger.exception(f"Unhandled exception in process_line: {e}")

    async def handle_pong(self, params):
        """Handle PONG responses from the server."""
        logger.debug('Received PONG')
        if params and self.pending_ping and params[-1] == self.pending_ping:
            self.last_pong_time = time.monotonic()
            self.pending_ping = None
            self.ping_sent_at = None

    async def handle_kick(self, prefix, params):
        if len(params) < 2:
            return
        channel, kicked_nick = params[:2]
        self.channel_users[channel.casefold()].discard(kicked_nick.casefold())
        if kicked_nick.casefold() != self.current_nick.casefold():
            return
        logger.warning('Kicked from %s; scheduling a rejoin', channel)
        now = time.monotonic()
        if now < self.next_rejoin.get(channel, 0):
            return
        self.next_rejoin[channel] = now + 30
        async def rejoin():
            await asyncio.sleep(5)
            if self.writer and not self.writer.is_closing():
                await self.send_raw(f"JOIN {irc_target_safe(channel)}")
        task = asyncio.create_task(rejoin())
        self.weather_tasks.add(task)
        task.add_done_callback(self.weather_tasks.discard)

    async def handle_nick(self, prefix, params):
        if not prefix or not params:
            return
        before = prefix.split('!')[0].casefold()
        after = params[0].lstrip(':').casefold()
        for users in self.channel_users.values():
            if before in users:
                users.discard(before)
                users.add(after)
        if before == self.current_nick.casefold():
            self.current_nick = params[0].lstrip(':')

    async def handle_notice(self, prefix, params):
        """Handle NOTICE messages from the server."""
        sender_nick = (prefix or '').split('!')[0]
        message = params[-1]
        logger.info(f"Received NOTICE from {sender_nick}: {message}")


    async def wait_for_nick_result(self, nick):
        """Wait for a NICK change to succeed or fail."""
        while True:
            line = await self.read_line_with_timeout(timeout=30)
            if line is None:
                raise ReconnectNeeded("Connection lost while changing nickname.")

            logger.debug('Received IRC nickname-change message')
            _, command, params = self.parse_irc_message(line)

            if command == 'NICK':
                return True
            if command == '433':
                return False
            if command == 'PING':
                await self.handle_ping(params)
                continue
            if command == '001' and self.current_nick.lower() == nick.lower():
                return True

    async def wait_for_nickserv(self, expected_message):
        """Wait for a matching NickServ NOTICE."""
        while True:
            line = await self.read_line_with_timeout(timeout=30)
            if line is None:
                raise ReconnectNeeded("Connection lost while waiting for NickServ.")

            logger.debug('Received IRC line while waiting for NickServ')
            prefix, command, params = self.parse_irc_message(line)

            if command == 'PING':
                await self.handle_ping(params)
                continue

            if command != 'NOTICE' or not prefix:
                continue

            sender = prefix.split('!')[0].lower()
            message = params[-1] if params else ""
            if sender == 'nickserv' and expected_message.lower() in message.lower():
                logger.info(f"Received expected NickServ message: {message}")
                return True

    async def reclaim_nickname(self):
        """Use an alternate nick, GHOST the preferred nick, then switch back."""
        for suffix in range(5):
            alternate = f"{USER}_{suffix}"
            logger.info(f"Trying alternate nickname {alternate}.")
            await self.send_raw(f"NICK {irc_target_safe(alternate)}")
            self.current_nick = alternate

            if await self.wait_for_nick_result(alternate):
                break
        else:
            raise ReconnectNeeded("Unable to obtain an alternate nickname.")

        await self.send_privmsg("NickServ", f"GHOST {USER} {PASSWORD}")
        await self.wait_for_nickserv("has been ghosted")

        logger.info(f"Reclaiming nickname {USER}.")
        await self.send_raw(f"NICK {irc_target_safe(USER)}")
        self.current_nick = USER

        if not await self.wait_for_nick_result(USER):
            raise ReconnectNeeded(f"Unable to reclaim nickname {USER}.")

    async def handle_join(self, prefix, params):
        """Handle JOIN events."""
        if not params or not prefix:
            return
        user = prefix.split('!')[0].lower()
        channel = params[0]
        self.channel_users[channel.casefold()].add(user)
        logger.debug('User joined %s', channel)

    async def handle_part(self, prefix, params):
        """Handle PART events."""
        if not prefix or not params:
            return
        user = prefix.split('!')[0].lower()
        channel = params[0]
        channel = channel.casefold()
        if user in self.channel_users[channel]:
            self.channel_users[channel].remove(user)
            logger.debug(f"{user} parted {channel}. Current users: {self.channel_users[channel]}")

    async def handle_quit(self, prefix):
        """Handle QUIT events."""
        if not prefix:
            return
        user = prefix.split('!')[0].lower()
        for ch, users in self.channel_users.items():
            if user in users:
                users.remove(user)
                logger.debug(f"{user} quit. Removed from {ch}. Current users in {ch}: {users}")

    async def handle_namereply(self, params):
        """Handle RPL_NAMREPLY (353) which contains a list of users in a channel."""
        # Expected form: params = [<my_nick>, <symbol>, <channel>, "<names...>"]
        if len(params) < 4:
            return
        channel = params[2]
        names_list = params[3].strip()
        if names_list.startswith(':'):
            names_list = names_list[1:]
        users = names_list.split()
        # Strip status symbols (@, +, etc.) and convert to lowercase
        sanitized_users = {u.lstrip('@+%&~').lower() for u in users}
        self.channel_users[channel.casefold()].update(sanitized_users)
        logger.debug(f"Updated channel user list for {channel}: {self.channel_users[channel.casefold()]}")

    async def cleanup(self):
        """Clean up resources on shutdown."""
        self.running = False
        for task in list(self.weather_tasks):
            task.cancel()
        if self.weather_tasks:
            await asyncio.gather(*self.weather_tasks, return_exceptions=True)
        await self.close_connection()
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        logger.info("Cleaned up resources.")

if __name__ == "__main__":
    bot = IrcBot()

    async def main():
        try:
            await bot.run()
        except asyncio.CancelledError:
            logger.info("Main task cancelled. Shutting down...")
            if bot.running:
                await bot.cleanup()
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received. Shutting down...")
            if bot.running:
                await bot.cleanup()
        except Exception as e:
            logger.exception(f"Unhandled exception in main: {e}")
            if bot.running:
                await bot.cleanup()

    async def runner():
        loop = asyncio.get_running_loop()
        main_task = asyncio.create_task(main())

        if hasattr(signal, 'SIGTERM'):
            def handle_sigterm():
                logger.info("SIGTERM received. Shutting down...")
                main_task.cancel()

            loop.add_signal_handler(signal.SIGTERM, handle_sigterm)

        await main_task

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        logger.info("Bot shut down gracefully.")
        sys.exit(0)

