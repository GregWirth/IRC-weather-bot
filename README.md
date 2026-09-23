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

## IRC Connection Features

The bot:

* connects using TLS
* uses IRC port 6697 by default
* authenticates using SASL PLAIN
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
