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

def sanitize_input(user_input):
    """Sanitize IRC text while preserving standard IRC formatting codes."""
    sanitized = user_input.replace('\r', '').replace('\n', '').replace('\0', '')

    # Preserve common IRC formatting control codes:
    # \x02 bold, \x03 color, \x0F reset, \x16 reverse,
    # \x1D italic, \x1F underline.
    sanitized = re.sub(r'[\x00\x01\x04-\x0E\x10-\x15\x17-\x1C\x1E\x7F]', '', sanitized)

    sanitized = sanitized.strip()

    # Limit input length to prevent flooding
    if len(sanitized) > 400:
        sanitized = sanitized[:400]

    # Allow necessary punctuation, symbols, and preserved IRC formatting codes.
    sanitized = re.sub(r'[^\w\s,.\-:|°%/()"\x02\x03\x0F\x16\x1D\x1F]', '', sanitized)
    return sanitized

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

            with open(self.file_path, 'r') as file:
                self.responses = [line.strip() for line in file if line.strip()]

            self.last_modified_time = modified
            self.available_responses.clear()
            logger.info(f"Reloaded {self.label} from {self.file_path}.")
        except FileNotFoundError:
            logger.error(f"Response file {self.file_path} not found.")
            self.responses = [f"No {self.label} available."]
            self.available_responses.clear()
        except Exception as e:
            logger.error(f"Error loading {self.label}: {e}")
            self.responses = [f"No {self.label} available."]
            self.available_responses.clear()

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
        self.last_pong_time = time.time()
        self.reader = None
        self.writer = None
        self.lock = asyncio.Lock()
        self.writer_lock = asyncio.Lock()
        self.weather_cache = TTLCache(maxsize=100, ttl=300)
        self.running = True
        self.message_semaphore = asyncio.Semaphore(1)
        self.current_nick = USER
        self.reconnect_lock = asyncio.Lock()
        # Store channel users in lowercase to enable case-insensitive checks
        self.channel_users = defaultdict(set)
        self.http_session = None

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

        async with self.writer_lock:
            self.writer.write(f"{line}\r\n".encode('utf-8'))
            await self.writer.drain()

    async def register(self):
        """Begin IRC registration and request SASL capability."""
        if self.writer is None:
            logger.error("Cannot register, no active connection.")
            return

        self.current_nick = USER
        await self.send_raw("CAP LS 302")
        await self.send_raw(f"NICK {self.current_nick}")
        await self.send_raw(f"USER {USERNAME} 0 * :{USERNAME}")
        logger.info("Sent CAP LS, NICK, and USER commands.")

    async def authenticate_sasl(self):
        """Authenticate using IRCv3 SASL PLAIN over the existing TLS connection."""
        if self.writer is None:
            raise ReconnectNeeded("Cannot authenticate with SASL without an active connection.")

        sasl_requested = False
        sasl_started = False

        while True:
            line = await self.read_line_with_timeout(timeout=30)
            if line is None:
                raise ReconnectNeeded("Connection lost during SASL authentication.")

            logger.debug(f"Received line during SASL negotiation: {line}")
            prefix, command, params = self.parse_irc_message(line)

            if command == 'PING':
                await self.handle_ping(params)
                continue

            if command == 'CAP' and len(params) >= 2:
                subcommand = params[1].upper()

                if subcommand == 'LS':
                    capabilities = params[-1].lower().split()
                    if not any(cap == 'sasl' or cap.startswith('sasl=') for cap in capabilities):
                        raise RuntimeError("IRC server does not advertise SASL capability.")

                    await self.send_raw("CAP REQ :sasl")
                    sasl_requested = True
                    logger.info("Requested SASL capability.")
                    continue

                if subcommand == 'ACK' and sasl_requested:
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
        return line.decode('utf-8', errors='replace').strip()

    async def wait_for_registration(self):
        """Wait for registration to finish; return False if the nick is in use."""
        while True:
            line = await self.read_line_with_timeout()
            if line is None:
                raise ReconnectNeeded("Connection lost during registration.")

            logger.debug(f"Received line during registration: {line}")
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
        await self.send_raw(f"PONG :{params[0]}")
        self.last_pong_time = time.time()
        logger.debug("Responded to PING with PONG.")

    async def reconnect(self):
        """Reconnect with exponential backoff."""
        async with self.reconnect_lock:
            await self.close_connection()
            delay = 30

            for attempt in range(1, 11):
                if not self.running:
                    return

                logger.info(f"Retrying connection in {delay} seconds...")
                await asyncio.sleep(delay)

                try:
                    logger.info(f"Reconnect attempt {attempt}...")
                    await self.connect()
                    return
                except Exception:
                    logger.exception(f"Reconnect attempt {attempt} failed.")
                    delay = min(delay * 2, 300)

            logger.error("Exceeded maximum reconnect attempts. Exiting.")
            await self.cleanup()
            sys.exit(1)

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

    async def join_channels(self):
        """Send JOIN commands for all channels."""
        if self.writer is None:
            logger.error("Cannot join channels, no active connection.")
            return

        for channel in CHANNELS:
            logger.info(f"Joining channel {channel}")
            await self.send_raw(f"JOIN {channel}")
            await asyncio.sleep(1)

    async def handle_privmsg(self, prefix, params):
        """Handle PRIVMSG commands."""
        try:
            user = prefix.split('!')[0]
            channel = params[0]
            message = params[1].strip()
            message_lower = message.lower()

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

            if channel == self.current_nick:
                await self.handle_private_message(user, message)
            else:
                if message_lower.startswith(TRIGGER.lower()):
                    args = message[len(TRIGGER):].strip()
                    args = sanitize_input(args)
                    forecast = False
                    if '--forecast' in args:
                        args = args.replace('--forecast', '').strip()
                        forecast = True
                    await self.handle_weather_command(user, channel, args, forecast)
                elif WAREZ_TRIGGER.lower() in message_lower:
                    await self.handle_warez_command(channel)
                elif message_lower.startswith(STAB_TRIGGER.lower()):
                    # Extract username after the trigger
                    stab_target = message[len(STAB_TRIGGER):].strip()
                    if stab_target:
                        stab_target = sanitize_input(stab_target)
                        await self.handle_stab_command(channel, stab_target)
        except Exception as e:
            logger.exception(f"Exception in handle_privmsg: {e}")

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
        await self.send_raw(f"NOTICE {target} :{message}")
        logger.debug(f"Sent NOTICE to {target}: {message}")

    async def handle_stab_command(self, channel, target_user):
        """Respond to the stab trigger with the specified target user, case-insensitive."""
        # Convert target_user to lowercase for comparison
        target_user_lower = target_user.lower()
        # Check if the target_user is in the channel before responding
        if target_user_lower not in self.channel_users[channel]:
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
        current_time = time.time()
        async with self.lock:
            # Global rate limit
            while self.global_request_times and current_time - self.global_request_times[0] > GLOBAL_RATE_LIMIT_TIME:
                self.global_request_times.popleft()
            if len(self.global_request_times) >= GLOBAL_RATE_LIMIT:
                remaining_time = GLOBAL_RATE_LIMIT_TIME - (current_time - self.global_request_times[0])
                warning_msg = f"The bot is currently handling many requests. Please try again in {int(remaining_time)} seconds."
                await self.send_message(channel, warning_msg)
                return
            self.global_request_times.append(current_time)

            # Per-user rate limit
            request_times = self.last_requests[user]
            while request_times and current_time - request_times[0] > RATE_LIMIT_TIME:
                request_times.popleft()
            if len(request_times) >= RATE_LIMIT:
                remaining_time = RATE_LIMIT_TIME - (current_time - request_times[0])
                warning_msg = f"You are being rate-limited, {user}. Try again in {int(remaining_time)} seconds."
                await self.send_message(channel, warning_msg)
                return
            request_times.append(current_time)

        await self.fetch_and_send_weather(channel, location, user, forecast)

    async def fetch_and_send_weather(self, channel, location, user, forecast=False):
        """Fetch weather data and send it to the channel."""
        try:
            cache_key = f"{location.lower()}_{forecast}"
            if cache_key in self.weather_cache:
                data = self.weather_cache[cache_key]
                logger.info(f"Using cached weather data for {location}.")
            else:
                days = 2 if forecast else 1
                url = f"https://api.weatherapi.com/v1/forecast.json?key={API_KEY}&q={quote(location)}&days={days}&aqi=no&alerts=no"
                if self.http_session is None or self.http_session.closed:
                    timeout = aiohttp.ClientTimeout(total=10)
                    self.http_session = aiohttp.ClientSession(timeout=timeout)

                async with self.http_session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                    elif resp.status == 401:
                        logger.error("Unauthorized access. Check your API key.")
                        error_msg = "Unauthorized access to weather API. Please check the API key."
                        await self.send_message(channel, error_msg)
                        return
                    elif resp.status == 404:
                        logger.error(f"Location '{location}' not found.")
                        error_msg = f"Location '{location}' not found."
                        await self.send_message(channel, error_msg)
                        return
                    else:
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
            logger.error(f"Weather API request for {location} timed out.")
            error_msg = "Weather API request timed out."
            await self.send_message(channel, error_msg)
        except aiohttp.ClientError as e:
            logger.error(f"HTTP error when fetching weather data for {location}: {e}")
            error_msg = f"Error fetching weather information for {location}."
            await self.send_message(channel, error_msg)
        except KeyError as e:
            logger.error(f"Missing expected data in API response for {location}: {e}")
            error_msg = "Received unexpected data from weather API."
            await self.send_message(channel, error_msg)
        except Exception as e:
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
        """Send a message to the IRC channel, splitting if too long."""
        max_length = 512 - len(f"PRIVMSG {channel} :\r\n") - 2
        message = sanitize_input(message)
        logger.debug(f"Attempting to send message to {channel}: {message}")
        async with self.message_semaphore:
            try:
                while message:
                    part = message[:max_length]
                    await self.send_privmsg(channel, part)
                    logger.debug(f"Sent message chunk to {channel}: {part}")
                    message = message[max_length:]
                    await asyncio.sleep(1)
            except ConnectionResetError:
                logger.error(f"Connection reset while sending message to {channel}.")
                raise ReconnectNeeded()
            except Exception as e:
                logger.error(f"Failed to send message to {channel}: {e}")
                raise ReconnectNeeded()

    async def send_privmsg(self, target, message):
        """Send a PRIVMSG to the specified target."""
        if self.writer is None:
            logger.error("Cannot send message, no active connection.")
            return
        await self.send_raw(f"PRIVMSG {target} :{message}")

    async def run(self):
        """Run the bot, reconnecting when the connection fails."""
        while self.running:
            try:
                logger.info("Starting connection to IRC server...")
                await self.connect()
                self.last_pong_time = time.time()
                logger.info("Starting to handle messages...")
                await self.handle_messages()
            except asyncio.CancelledError:
                raise
            except ReconnectNeeded:
                logger.info("Reconnect needed, reconnecting...")
                await self.reconnect()
            except Exception as e:
                logger.exception(f"Unhandled exception in run: {e}")
                await self.reconnect()

    async def send_ping(self):
        """Send a PING message to the server."""
        if self.writer is None:
            logger.error("Cannot send PING, no active connection.")
            return
        await self.send_raw(f"PING :{self.current_nick}")
        logger.debug("Sent PING to server.")

    async def handle_messages(self):
        """Handle IRC traffic and connection liveness."""
        while self.running:
            try:
                line = await self.read_line_with_timeout(timeout=PING_INTERVAL)
            except asyncio.TimeoutError:
                if time.time() - self.last_pong_time > PING_TIMEOUT:
                    logger.warning("IRC connection timed out. Reconnecting...")
                    raise ReconnectNeeded()
                await self.send_ping()
                continue

            if line is None:
                raise ReconnectNeeded("IRC connection closed.")

            logger.debug(f"Received line: {line}")
            await self.process_line(line)

    async def process_line(self, line):
        """Process a single line from the IRC server."""
        try:
            prefix, command, params = self.parse_irc_message(line)
            logger.debug(f"Prefix: {prefix}, Command: {command}, Params: {params}")

            if command == 'PING':
                await self.handle_ping(params)
            elif command == 'PONG':
                await self.handle_pong(params)
            elif command == 'NOTICE':
                await self.handle_notice(prefix, params)
            elif command == 'PRIVMSG':
                await self.handle_privmsg(prefix, params)
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
            elif command == 'ERROR':
                error_message = ' '.join(params)
                logger.error(f"Server error: {error_message}")
                if "closing link" in error_message.lower():
                    logger.warning("Possible netsplit detected. Attempting to reconnect...")
                    raise ReconnectNeeded()
                else:
                    raise ReconnectNeeded()
            else:
                logger.debug(f"Unhandled message: {line}")
        except ReconnectNeeded:
            raise
        except Exception as e:
            logger.exception(f"Unhandled exception in process_line: {e}")

    async def handle_pong(self, params):
        """Handle PONG responses from the server."""
        logger.debug(f"Received PONG from {params[0]}")
        self.last_pong_time = time.time()

    async def handle_kick(self, prefix, params):
        """Handle being kicked from a channel."""
        channel = params[0]
        kicked_nick = params[1]
        if kicked_nick == self.current_nick:
            logger.warning(f"Kicked from {channel}. Attempting to rejoin...")
            await asyncio.sleep(5)
            await self.join_channels()

    async def handle_notice(self, prefix, params):
        """Handle NOTICE messages from the server."""
        sender_nick = prefix.split('!')[0]
        message = params[-1]
        logger.info(f"Received NOTICE from {sender_nick}: {message}")


    async def wait_for_nick_result(self, nick):
        """Wait for a NICK change to succeed or fail."""
        while True:
            line = await self.read_line_with_timeout(timeout=30)
            if line is None:
                raise ReconnectNeeded("Connection lost while changing nickname.")

            logger.debug(f"Received line during nickname change: {line}")
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

            logger.debug(f"Received line waiting for NickServ response: {line}")
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
            await self.send_raw(f"NICK {alternate}")
            self.current_nick = alternate

            if await self.wait_for_nick_result(alternate):
                break
        else:
            raise ReconnectNeeded("Unable to obtain an alternate nickname.")

        await self.send_privmsg("NickServ", f"GHOST {USER} {PASSWORD}")
        await self.wait_for_nickserv("has been ghosted")

        logger.info(f"Reclaiming nickname {USER}.")
        await self.send_raw(f"NICK {USER}")
        self.current_nick = USER

        if not await self.wait_for_nick_result(USER):
            raise ReconnectNeeded(f"Unable to reclaim nickname {USER}.")

    async def handle_join(self, prefix, params):
        """Handle JOIN events."""
        user = prefix.split('!')[0].lower()
        channel = params[0]
        self.channel_users[channel].add(user)
        logger.debug(f"{user} joined {channel}. Current users: {self.channel_users[channel]}")

    async def handle_part(self, prefix, params):
        """Handle PART events."""
        user = prefix.split('!')[0].lower()
        channel = params[0]
        if user in self.channel_users[channel]:
            self.channel_users[channel].remove(user)
            logger.debug(f"{user} parted {channel}. Current users: {self.channel_users[channel]}")

    async def handle_quit(self, prefix):
        """Handle QUIT events."""
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
        self.channel_users[channel].update(sanitized_users)
        logger.debug(f"Updated channel user list for {channel}: {self.channel_users[channel]}")

    async def cleanup(self):
        """Clean up resources on shutdown."""
        self.running = False
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

