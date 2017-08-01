"""
    Class Name : Discord Connector

    Description:
        Provides functionality for connecting to Discord chat server
        This class is a subclass of core/Connector.py and implements the required methods so the
        rest of the bot can talk to Discord

    Contributors:
        - Patrick Hennessy
        - Aleksandr Tihomirov

    License:
        Arcbot is free software: you can redistribute it and/or modify it under the terms of the GNU
        General Public License v3; as published by the Free Software Foundation
"""
from platform import system
from ssl import *
from enum import Enum, auto

import requests
import json
import logging
import threading
import time
import websocket
from datetime import timedelta

class Discord():
    def __init__(self, core, token):
        super(Discord, self).__init__()
        self.core = core
        self.logger = logging.getLogger(__name__)

        self.connected = False          # Boolean for handling connection state
        self.token = token              # Token used to authenticate
        self.socket = None              # Websocket connection
        self.ping = -1
        self.sequence = 0

        self.api = api(self.token)
        self.socket_url = f"{self.api.get_gateway_bot()['url']}?v=6&encoding=json"

        # Internal threads
        self.heartbeat_thread = None
        self.message_consumer_thread = None

    def connect(self):
        # Connect to Discord, post login credentials
        self.logger.info("Attempting connection to Discord servers")
        self.socket = websocket.create_connection(self.socket_url)

        # OP 10 Hello Payload
        hello_payload = self._read_socket()
        self.heartbeat_interval = hello_payload['d']['heartbeat_interval']

        # Get ping
        self.ping_start = time.monotonic()
        self._write_socket({"op":1,"d": self.sequence})
        ack = self._read_socket()
        ping = timedelta(seconds=time.monotonic()-self.ping_start)
        self.ping = round(ping.microseconds / 1000)

        # Identify self
        self._write_socket({
            "op": 2,
            "v": 6,
            "d": {
                "token": self.token,
                "properties": {
                    "$os": system(),
                    "$browser": "Arcbot",
                    "$device": "Arcbot"
                },
                "large_threshold": 50,
                "compress": False
            }
        })

        # Set websocket to nonblocking so we can exit a thread reading from the socket if we need to
        self.socket.sock.setblocking(0)
        self.connected = True

        self.logger.info("Succesful login to Discord")

        # Create and start threads
        self.heartbeat_thread = threading.Thread(target=self._heartbeat, name="heartbeat_thread")
        self.heartbeat_thread.daemon = True
        self.heartbeat_thread.start()

        self.message_consumer_thread = threading.Thread(target=self._message_consumer, name="message_consumer_thread")
        self.message_consumer_thread.daemon = True
        self.message_consumer_thread.start()

    def disconnect(self):
        self.connected = False

        # Join threads if they exist
        if isinstance(self.heartbeat_thread, threading.Thread):
            self.heartbeat_thread.join()

        if isinstance(self.message_consumer_thread, threading.Thread):
            self.message_consumer_thread.join()

        self.logger.debug('Joined message_consumer and heartbeat threads')

        # Close websocket if it is established
        if isinstance(self.socket, websocket.WebSocket):
            self.socket.close()

        self.logger.info("Disconnected from Discord")

    def say(self, channel_id, message="", embed={}, mentions=[]):
        self.logger.debug("Sending message to channel " + channel_id)

        for user in mentions:
            message = "<@{}> ".format(user) + message

        message_data = {
            "content": "{}".format(message),
            "embed": embed,
            "mentions": mentions
        }

        try:
            self.api.create_message(channel_id, json.dumps(message_data))
        except Exception as e:
            self.logger.warning('Send message to channel \'{}\' failed: {}'.format(channel_id, e))

    def whisper(self, user_id, message="", embed={}, mentions=[]):
        channel = self.api.create_dm(user_id)
        channel_id = channel['id']

        self.say(channel_id, message=message, embed=embed, mentions=mentions)

    def upload(self, channel, file):
        self.logger.debug('Uploading file to channel ' + channel)

        endpoint = self.base_url + "channels/{}/messages".format(channel)
        files = {'file': open(file, 'rb')}

        try:
            self.api.create_message(endpoint, files=files, headers=self.auth_headers)
        except Exception as e:
            self.logger.warning('Upload of {} to channel {} failed'.format(file, channel))

    def set_status(self, status):
        self.status = status

        self._write_socket({
            "op":3,
            "d":{
                "idle_since":None,
                "game": {
                    "name": self.status
                }
            }
        })

    # Thread Methods
    def _heartbeat(self):
        self.logger.debug(f"Spawning heartbeat thread at interval: {self.heartbeat_interval}")

        last_heartbeat = time.time()
        heartbeat_interval = self.heartbeat_interval / 1000

        while self.connected:
            now = time.time()

            if (now - last_heartbeat) >= heartbeat_interval - 1:
                self.ping_start = time.monotonic()
                self._write_socket({"op":1,"d": self.sequence})

                self.logger.debug("Heartbeat")
                last_heartbeat = time.time()

                self.set_status(f"Ping: {self.ping}ms")

            time.sleep(1)

    def _message_consumer(self):
        self.logger.debug("Spawning message_consumer thread")

        def handle_gateway_message(gateway_message):
            # New Event
            if gateway_message['op'] == 0:
                new_event = event.from_message(gateway_message)
                self.sequence = new_event.sequence

                # Queue all callbacks independently to not block a single thread
                for callback in new_event.subscriptions:
                    self.core.workers.queue(callback, new_event)

            # Invalid session
            elif gateway_message['op'] == 9:
                self.connected = False
                self.logger.warning("Connection terminated with Invalid Session ID")

            # Heartbeak ACK
            elif gateway_message['op'] == 11:
                ping = timedelta(seconds=time.monotonic()-self.ping_start)
                self.ping = round(ping.microseconds / 1000)

        while self.connected:
            time.sleep(0.05)

            gateway_message = self._read_socket()
            if not gateway_message:
                continue

            self.core.workers.queue(handle_gateway_message, gateway_message)

    # Socket Methods
    def _write_socket(self, data):
        try:
            self.socket.send(json.dumps(data))
        except socket_error as e:
            if e.errno == 104:
                if not self.connected:
                    return

                self.logger.warning("READ: Connection reset by peer.")
                self.connected = False
            else:
                raise
        except websocket.WebSocketConnectionClosedException:
            if not self.connected:
                return

            self.logger.warning("WRTIE: Websocket unexpectedly closed.")
            self.connected = False

    def _read_socket(self):
        data = ""
        while True:
            try:
                data += self.socket.recv()

                if data:
                    #d = json.loads(data.rstrip())
                    #print(f"SEQ: {d['s']} OP: {d['op']} NAME: {d['t']}")
                    return json.loads(data.rstrip())
                else:
                    return None

            except ValueError as e:
                continue
            except SSLError as e:
                # Raised when we can't read the entire buffer at once
                if e.errno == 2:
                    return None
                raise
            except socket_error as e:
                # Raised when connection reset by peer
                if e.errno == 104:
                    if not self.connected:
                        return

                    self.logger.warning("READ: Connection reset by peer.")
                    self.connected = False
                    return None

                # Raised when send buffer is full; we must try again
                if e.errno == 11:
                    return None
                raise
            except websocket.WebSocketConnectionClosedException:
                if not self.connected:
                    return

                self.logger.warning("READ: Websocket unexpectedly closed.")
                self.connected = False

class api():
    def __init__(self, token):
        self.auth_headers = {
            "authorization": "Bot " + token,
            "Content-Type": 'application/json'
        }
        self.base_url = "https://discordapp.com/api"

    # Gateway
    def get_gateway(self):
        """
            API Docs: https://discordapp.com/developers/docs/topics/gateway#get-gateway
            Description:
                Returns an object with a single valid WSS URL, which the client can use as a basis
                for Connecting. Clients should cache this value and only call this endpoint to
                retrieve a new URL if they are unable to properly establish a connection using the
                cached version of the URL.
        """
        uri = f"{self.base_url}/gateway"
        headers = self.auth_headers

        response = requests.get(uri, headers=headers)
        response.raise_for_status()
        return response.json()

    def get_gateway_bot(self):
        """
            API Docs: https://discordapp.com/developers/docs/topics/gateway#get-gateway-bot
            Description:
                Returns an object with the same information as Get Gateway, plus a shards key,
                containing the recommended number of shards to connect with (as an integer). Bots
                that want to dynamically/automatically spawn shard processes should use this
                endpoint to determine the number of processes to run. This route should be called
                once when starting up numerous shards, with the response being cached and passed to
                all sub-shards/processes. Unlike the Get Gateway, this route should not be cached
                for extended periods of time as the value is not guaranteed to be the same per-call,
                and changes as the bot joins/leaves guilds.
        """
        uri = f"{self.base_url}/gateway/bot"
        headers = self.auth_headers

        response = requests.get(uri, headers=headers)
        response.raise_for_status()
        return response.json()

    # Channel : https://discordapp.com/developers/docs/resources/channel
    def get_channel(self, channel_id):
        """
            API Docs: https://discordapp.com/developers/docs/resources/channel#get-channel
            Description:
                Get a channel by ID. Returns a guild channel or dm channel object.
        """
        uri = f"{self.base_url}/channels/{channel_id}"
        headers = self.auth_headers

        response = requests.get(uri, headers=headers)
        response.raise_for_status()
        return response.json()

    def create_message(self, channel_id, message_data, files=None):
        """
            API Docs: https://discordapp.com/developers/docs/resources/channel#create-message
            Description:
                Post a message to a guild text or DM channel. If operating on a guild channel,
                this endpoint requires the 'SEND_MESSAGES' permission to be present on the current
                user. Returns a message object. Fires a Message Create Gateway event. See message
                formatting for more information on how to properly format messages.
        """
        uri = f"{self.base_url}/channels/{channel_id}/messages"
        headers = self.auth_headers

        if isinstance(message_data, dict):
            message_data = json.dumps(message_data)

        response = requests.post(uri, data=message_data, files=files, headers=headers)
        response.raise_for_status()

    # User : https://discordapp.com/developers/docs/resources/user
    def create_dm(self, user_id):
        """
            API Docs: https://discordapp.com/developers/docs/resources/user#create-dm
            Description:
                Create a new DM channel with a user. Returns a DM channel object.
        """
        uri = f"{self.base_url}/users/@me/channels"
        headers = self.auth_headers
        data={
            "recipient_id": f"{user_id}"
        }

        response = requests.post(uri, data=json.dumps(data), headers=headers)
        response.raise_for_status()

        return response.json()

class embed_intent():
    INFO = int("7289da", 16)
    WARNING = int("faa61a", 16)
    ERROR = int("f04747", 16)

class events(Enum):
    READY = auto()
    RESUMED = auto()
    CHANNEL_CREATE = auto()
    CHANNEL_UPDATE = auto()
    CHANNEL_DELETE = auto()
    CHANNEL_PINS_UPATE = auto()
    GUILD_CREATE = auto()
    GUILD_UPDATE = auto()
    GUILD_DELETE = auto()
    GUILD_BAN_ADD = auto()
    GUILD_BAN_REMOVE = auto()
    GUILD_EMOJIS_UPDATE = auto()
    GUILD_INTEGRATIONS_UPDATE = auto()
    GUILD_MEMBER_ADD = auto()
    GUILD_MEMBER_REMOVE = auto()
    GUILD_MEMBER_UPDATE = auto()
    GUILD_MEMBERS_CHUNK = auto()
    GUILD_ROLE_CREATE = auto()
    GUILD_ROLE_UPDATE = auto()
    GUILD_ROLE_DELETE = auto()
    MESSAGE_CREATE = auto()
    MESSAGE_UPDATE = auto()
    MESSAGE_DELETE = auto()
    MESSAGE_DELETE_BULK = auto()
    MESSAGE_REACTION_ADD = auto()
    MESSAGE_REACTION_REMOVE = auto()
    MESSAGE_REACTION_REMOVE_ALL = auto()
    PRESENCE_UPDATE = auto()
    TYPING_START = auto()
    USER_UPDATE = auto()
    VOICE_STATE_UPDATE = auto()
    VOICE_SERVER_UPDATE = auto()
    WEBHOOKS_UPDATE = auto()

class event():
    subscriptions = {}

    @classmethod
    def unsubscribe(cls, event_id, callback):
        if event_id in cls.subscriptions.keys():
            cls.subscriptions[event_id].remove(callback)

    @classmethod
    def subscribe(cls, event_id, callback):
        event_id = event_id

        if event_id not in cls.subscriptions.keys():
            cls.subscriptions[event_id] = [callback]
        else:
            cls.subscriptions[event_id].append(callback)

    @classmethod
    def from_message(cls, message):
        new_event = cls.to_object(message["d"])
        new_event.name = getattr(events, message["t"])
        new_event.sequence = message["s"]
        new_event.subscriptions = cls.subscriptions.get(new_event.name, [])

        return new_event

    @classmethod
    def to_object(cls, item):
        def convert(item):
            if isinstance(item, dict):
                return type('Event', (), {k: convert(v) for k, v in item.items()})
            if isinstance(item, list):
                def yield_convert(item):
                    for index, value in enumerate(item):
                        yield convert(value)
                return list(yield_convert(item))
            else:
                return item

        return convert(item)
