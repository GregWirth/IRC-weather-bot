Runs on FreeBSD 15.1-RELEASE-p3 and Linux Mint 21.x.

Written for Python 3.12+ but may work with earlier Python 3.x releases.

Requires additional Python modules which can be installed with pip.

## Basic Operation

The script connects to a selected IRC server and listens for commands.

You may name the Python script anything you like:

```
{filename}.py
```

Edit `config.json` with your IRC, API, channel, and trigger settings.

Run:

```
python3.12 {filename}.py --log-level DEBUG
```

Available logging levels:

```
DEBUG
INFO
WARNING
ERROR
CRITICAL
```

When installed in a Python virtual environment, use the Python interpreter from that environment instead.

Example:

```
.venv/bin/python {filename}.py --log-level DEBUG
```

The bot creates:

```
bot.log
```

To watch the log:

```
tail -f bot.log
```

The log rotates daily. Older logs will appear similar to:

```
bot.log.2026-09-23
```

## Weather API

This script uses the API from:

```
https://www.weatherapi.com/
```

A free account is available.

The default weather trigger is:

```
#zz
```

Examples:

```
#zz 99709
#zz Dallas
#zz Dallas --forecast
#zz 32.7831 -96.8065
```

The bot accepts locations supported by WeatherAPI, including:

* US ZIP codes
* UK postcodes
* Canadian postal codes
* IP addresses
* latitude/longitude in decimal degrees
* city names

Using `--forecast` returns a two-day forecast.

Weather API requests are cached for five minutes to reduce unnecessary API calls.

The bot also rate-limits weather requests. Users may make up to three requests within 60 seconds. Users exceeding the configured limit are temporarily throttled.

## Warez Trigger

The bot can select and send a random line from:

```
warez-trigger.txt
```

Default trigger:

```
!warez
```

The file is automatically reloaded when changed, so the bot does not need to be restarted after editing it.

## Stab Trigger

The bot can select a random line from:

```
stab-trigger.txt
```

Default usage:

```
#stab {username}
```

The selected line is used once per cycle before being repeated. After every available line has been used, the list is shuffled and a new cycle begins.

The response file is automatically reloaded when changed.

## Channel Administration Commands (v1.02)

Version 1.02 adds three administration commands. Enter them in an IRC channel; the bot replies in the same channel where the command was issued. They are not handled through private messages.

| Command | Response |
| --- | --- |
| `##status` | IRC connection status, current bot nickname, number of configured channels, and uptime. |
| `##uptime` | Elapsed time since the bot process started (days, hours, minutes, seconds). |
| `##stats` | Weather requests, WeatherAPI requests, cache hits, weather errors, and current weather-cache entries. |

Configure the administrator account(s) in `config.json` by adding an `ADMIN_ACCOUNTS` setting alongside the existing settings:

```json
"ADMIN_ACCOUNTS": ["YOUR_REGISTERED_IRC_ACCOUNT"]
```

For multiple administrators, use `"ADMIN_ACCOUNTS": ["account1", "account2"]`. Enter **registered IRC account names**, not visible nicknames or WHOIS hostmasks. The bot checks the server-provided IRCv3 `account-tag` identity. Unauthorized commands are ignored and logged.

If `ADMIN_ACCOUNTS` is missing or set to an empty list (`[]`), **all administrative commands are disabled**. Restart the bot after changing `config.json`; administrative account changes are not hot-reloaded. When adding the entry to an existing JSON object, remember to separate entries with commas. The GitHub example configuration should contain only a placeholder account, never your own identity or real passwords.

The IRC server must support both `sasl` and `account-tag` capabilities. The bot requests both during connection setup; this version will refuse to connect if `account-tag` is unavailable.

Statistics are kept in memory and reset when the bot process restarts. They do not persist between restarts.

## IRC Connection Features

The bot:

* connects using TLS
* uses IRC port 6697 by default
* authenticates using SASL PLAIN
* requests IRCv3 `account-tag` to authenticate channel administration commands
* responds to IRC server PING requests
* monitors connection liveness
* detects connection loss and timeouts
* automatically reconnects with exponential backoff
* can use NickServ GHOST to reclaim its configured nickname if necessary
* rejoins configured channels after reconnecting
* tracks channel users for commands that require a valid channel nickname
* performs input sanitation
* throttles excessive API requests
* caches WeatherAPI responses
* rotates logs daily

Despite the comprehensive refactoring and the meticulous integration of
a plethora of enhancements designed to optimize the code's stability and
reliability, there remains a non-negligible probability that you may encounter
anomalous behaviors or unexpected phenomena. This potentiality arises due to
the stochastic nature of environmental variables and the emergence of unforeseen
edge cases inherent in complex systems.

Factors such as hardware architecture variances, divergent operating system kernels,
discrepancies in library versions, or even quantum-level computational fluctuations
can introduce chaotic elements into the execution environment. The intricate interplay
between software algorithms and the underlying computational substrate can lead to
emergent properties that are not readily predictable through conventional deterministic
models.

Consequently, while the codebase has been engineered with rigorous adherence to best
practices in software development and systems engineering, it is imperative to
acknowledge that the multifaceted dynamics of real-world application could precipitate
idiosyncratic issues not previously elucidated during the testing phases.

## v1.03 security and stability updates

The bot retains the same user-facing weather, `!warez`, `#stab`, and authenticated channel administration commands (`##status`, `##uptime`, `##stats`). Weather output remains **one IRC message maximum**, capped at **400 characters** (and also respects IRC's 512-byte wire limit). Responses longer than this are truncated, not split.

Changes include:

- One connection lifecycle manager with retry backoff and cleanup to avoid duplicate connections.
- Concurrent, bounded weather processing so slow HTTP requests do not block server PING traffic.
- IRC PING/PONG matching with monotonic timers.
- Per-user/per-channel cooldowns for entertainment triggers; rejected weather requests no longer consume global quota.
- Channel membership updated on nickname changes, cleared after reconnect, and KICK rejoins only the affected channel.
- Last known-good trigger responses retained after temporary file errors.
- Validation of required config values, safer UTF-8 outgoing IRC message size handling, and additional runtime statistics.
- Reduced raw IRC debug logging to avoid accidentally recording credentials and personal message content.

Optional settings in `config.json` (within the top-level JSON object):

```json
"ENTERTAINMENT_COOLDOWN": 12,
"WEATHER_CONCURRENCY": 3
```

`ENTERTAINMENT_COOLDOWN` is the minimum number of seconds between the same sender's uses of the same entertainment trigger in the same channel. `WEATHER_CONCURRENCY` limits simultaneous weather lookups. Both have the defaults shown; you can omit them.

`ADMIN_ACCOUNTS` should list **registered, authenticated IRC account names**, not hostmasks or nicknames. For example, a published *sample* can use:

```json
"ADMIN_ACCOUNTS": ["YOUR_IRC_ACCOUNT"]
```

Put your real authenticated account name only in your private installation config. Empty or absent `ADMIN_ACCOUNTS` disables admin commands. The IRC server must provide `account-tag` for this implementation.

### Validate and test before deploying

From the directory containing the script and `config.json`:

```sh
python3 -m json.tool config.json >/dev/null
python3 -m py_compile irc-weather-bot_v1.03.py
```

The companion `test_irc_weather_v103.py` uses an isolated, placeholder configuration and requires `aiohttp` and `cachetools` in your Python environment. Place it alongside the v1.03 Python file and run:

```sh
python3 -m unittest -v test_irc_weather_v103.py
```

### FreeBSD upgrade (existing rc.d installation)

Your wrapper may still reference an older Python filename. Keep a backup, **leave your real config.json on the server**, then copy the new script to your installation directory. Update `/usr/local/sbin/irc_weather_run` to point at the new script name or replace the older filename after backing it up.

Restart with `service irc_weather restart`, check `service irc_weather status`, and inspect `bot.log`. Test `##status`, `##uptime`, `##stats`, a normal weather lookup, and a response trigger. Roll back by restoring the previous Python file and restarting the service.

**Limitations:** The included tests are offline. This release has not yet been exercised against a live IRC server or simulated netsplit. Legacy NickServ nickname recovery still uses a password-based GHOST request. Do not publish real `config.json` credentials or API keys; set `chmod 600 config.json` on the running installation.
