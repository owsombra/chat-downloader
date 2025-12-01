import queue
from json.decoder import JSONDecodeError
from threading import Thread
from typing import Optional

import orjson
from requests.exceptions import RequestException
from websocket import WebSocketApp

from ..debugging import log
from ..errors import SiteError, UnexpectedError, UserNotFound, VideoUnavailable
from ..utils.core import attempts
from .common import BaseChatDownloader, Chat

# NOTE: https://github.com/kimcore/chzzk/blob/main/src/chat/chat.ts

class ChatCommands:
    # Define command codes as class attributes
    PING = 0
    PONG = 10000
    CONNECT = 100
    CONNECTED = 10100
    SEND_CHAT = 3101
    REQUEST_RECENT_CHAT = 5101
    RESPONSE_RECENT_CHAT = 15101
    EVENT = 93006
    CHAT = 93101
    DONATION = 93102
    KICK = 94005
    BLOCK = 94006
    BLIND = 94008
    NOTICE = 94010
    PENALTY = 94015


class ChatType:
    TEXT = 1
    IMAGE = 2
    STICKER = 3
    VIDEO = 4
    RICH = 5
    DONATION = 10
    SUBSCRIPTION = 11
    SYSTEM_MESSAGE = 30
    SYSTEM_ANNOUNCE = 121

    def __init__(self, code):
        self.code = code

    def __str__(self):
        if self.code == ChatType.TEXT:
            return 'text_message'
        elif self.code == ChatType.IMAGE:
            return 'image_message'
        elif self.code == ChatType.STICKER:
            return 'sticker_message'
        elif self.code == ChatType.RICH:
            return 'rich_message'
        elif self.code == ChatType.DONATION:
            return 'donation_message'
        elif self.code == ChatType.SUBSCRIPTION:
            return 'subscription_message'
        elif self.code == ChatType.SYSTEM_MESSAGE:
            return 'system_message'
        elif self.code == ChatType.SYSTEM_ANNOUNCE:
            return 'system_announce'
        else:
            return f'unknown_message_{self.code}'


class ChzzkChatDownloader(BaseChatDownloader):
    _NAME = 'chzzk.naver.com'

    _SITE_DEFAULT_PARAMS = {
        'format': 'default',
    }

    _VALID_URLS = {
        # e.g. 'https://chzzk.naver.com/live/channel_id'
        '_get_chat_by_channel_id': r"https?://chzzk\.naver\.com/live/(?P<channel_id>[^/?]+)",

        # e.g. 'https://chzzk.naver.com/video/video_id'
        '_get_chat_by_video_id': r"https?://chzzk\.naver\.com/video/(?P<video_id>[^/?]+)",
    }

    _ACCESS_TOKEN_URL = "https://comm-api.game.naver.com/nng_main/v1/chats/access-token?channelId={chat_channel_id}&chatType=STREAMING"
    _LIVE_DETAIL_URL = "https://api.chzzk.naver.com/service/v3.2/channels/{channel_id}/live-detail"
    _VOD_DETAIL_URL = "https://api.chzzk.naver.com/service/v3/videos/{vod_id}"
    _VOD_CHAT_URL = "https://api.chzzk.naver.com/service/v1/videos/{vod_id}/chats?playerMessageTime={player_message_time}"

    _DEFAULT_NID_AUT = "nVrU5HBws13iBYAnAa5D7bnZrUtp69cn6T+V7BHQIXhHrBexYt9yDBjPS2+YvWdb"
    _DEFAULT_NID_SES = "AAABoWkzOGZj+RIiu6C4Jakp+RdUsaMtRgLbMzO8kh5it7a34ADYVPTvZKtrw9hPNd88WgRjMbyB8+dYw00N+jJckHHo6Q9szDa7Gssw1B7jJF0KiwAi6REeaJa3sdQomN/mdrWEHqvlizYg8cKWaIgCc+evNveEoxcd8zwuRPlSorGWcg09gMPmGwhdFN+eT37sWkCY+gU3W0bbOMUsghZQ/ULUif5+Ghv2fq1gfEukHkbbdiEyRqKuhjjiFn1JNj2cb6Mc+cYBOsZOPFqJ5YuYUVYPKLxg5/jVaH++EmUWgEKonVIlL2f0mjEoIoXYEhwMT4b+iu/xo41IWA35am2RkTLu7rVwSIebVTGLL2W5DAapfUje02SZ+jyl6ynEuhHlHf5994/8IJFerfE2Nh9AhWbECzCRpSTDYaolysKQ/uvUtXxmcuUWCtrUAPZQuXWwE0jtpBUzqZjDFuTMG16EetA0b1K3RrlD2BXut1LlTyfXEyy6UgeoijDnR18X6WvamMT3LieM6Q+QOFI3lhrmYnqUEP+UoYpArIHDtAesgQuKwiai6q1ooIsvtVuAIp6Xdw=="

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.queue = queue.Queue()
        self.terminated = False
        self.chat_channel_id = None
        self.live_channel_id = None
        self.cookies = None
        self.websocket: Optional[WebSocketApp]
        self.websocket_thread: Optional[Thread] = None
        self.timeout = 5
        self.max_retries = 5
        self.proxy_host = None
        self.proxy_port = None

    def on_message(self, ws, message):
        if not message:
            return

        try:
            raw_msg = orjson.loads(message)
            cmd = raw_msg.get('cmd')
            if cmd == ChatCommands.CONNECTED:
                log('info', f'Connected to {self.chat_channel_id}...')
                self.sid = raw_msg['bdy']['sid']
                self.send_json({
                    "ver": "3",
                    "svcid": "game",
                    "cid": self.chat_channel_id,
                    "cmd": ChatCommands.REQUEST_RECENT_CHAT,
                    "tid": 2,
                    "sid": self.sid,
                    "bdy": {
                        'recentMessageCount': 50
                    }
                })
                return
            elif cmd == ChatCommands.PING:
                self.send_json({'ver': '3', 'cmd': ChatCommands.PONG})
                return
            elif cmd == ChatCommands.PONG:
                return

            if 'bdy' not in raw_msg:
                return

            raw_body = raw_msg['bdy']
            if isinstance(raw_body, list):
                chat_msgs = raw_body
            elif isinstance(raw_body, dict):
                chat_msgs = raw_body.get('messageList', [raw_body])
            else:
                log('error', f'Unknown format: {raw_body}')
                return

            for chat_msg in chat_msgs:
                data = self._parse_chat(chat_msg)
                self.queue.put(data)
        except Exception as e:
            log('error', f'Parsing message failed({e}): {message}')

    def on_error(self, ws, error):
        # maybe this can be change of cid, retrieve info once more to confirm
        log('error', f'Websocket Error: {error}')

    def on_close(self, ws, close_status_code, close_msg):
        # probably only when server requested to close
        log('info', '### Websocket closed ###')
        if not self.terminated:
            # not closed by request, try again
            is_live, _, _ = self.connect_websocket()
            if not is_live:
                # do not retry if not live
                self.terminate()

    def on_open(self, ws):
        log('info', 'Opened connection')
        self.send_json({
            "ver": "3",
            "svcid": "game",
            "cid": self.chat_channel_id,
            "cmd": ChatCommands.CONNECT,
            "tid": 1,
            "bdy": {
                "uid": None,
                "devName": "Google Chrome/129.0.0.0",
                "timezone": "Asia/Seoul",
                "devType": 2001,
                "libVer": "4.9.3",
                "locale": "ko",
                "osVer": "Windows/10",
                "accTkn": self.access_token,
                "auth": "READ"
            }
        })

    def send_json(self, data):
        self.websocket.send(orjson.dumps(data))

    def _parse_chat(self, chat):
        message_time = chat.get('messageTime') or chat.get('msgTime')
        if not message_time:
            return {}

        message = chat.get('content') or chat.get('msg')
        message_type = chat.get('messageTypeCode') or chat.get('msgTypeCode')
        user_id = chat.get('userIdHash') or chat.get('uid') or chat.get('userId')
        time_in_seconds = chat.get('playerMessageTime')
        member_count = chat.get('mbrCnt') or chat.get('memberCount')

        # Skip system messages
        if message_type in (ChatType.SYSTEM_MESSAGE, ChatType.SYSTEM_ANNOUNCE,):
            log('info', f'Skip Chzzk message_type {message_type}: {chat}')
            return {}
        if 'profile' not in chat and 'extras' not in chat:
            log('info', f'Skip Chzzk message: {chat}')
            return {}

        # NOTE: `profile` can be `None`, `"null"`
        raw_profile = chat.get('profile', '{}')
        if not raw_profile:
            display_name = ''
            subscription = None
        else:
            profile = orjson.loads(raw_profile)
            if type(profile) is not dict:
                display_name = ''
                subscription = None
            else:
                display_name = profile.get('nickname', '')
                streaming_property = profile.get('streamingProperty') or {}  # nullable
                subscription = streaming_property.get('subscription', None)

        extras = orjson.loads(chat.get('extras') or '{}')
        emotes = extras.get('emojis')
        pay_amount = extras.get('payAmount')

        data = {
            'timestamp': message_time * 1000,
            'message_id': f'{user_id}-{message_time}',
            'message': message,
            'message_type': str(ChatType(message_type)),
            'author': {
                'display_name': display_name,
                'id': user_id,
                'subscription': subscription,
            }
        }

        if time_in_seconds:
            data['time_in_seconds'] = time_in_seconds / 1000

        if member_count:
            data['member_count'] = member_count

        if extras:
            data['extras'] = extras

        if emotes:
            data['emotes'] = emotes

        if pay_amount:
            data['pay_amount'] = pay_amount

        return data

    def _get_chat_by_video_id(self, match, params):
        return self.get_chat_by_video_id(match.group('video_id'), params)

    def get_chat_by_video_id(self, vod_id, params):
        self.cookies = {
            "NID_AUT": params.get('NID_AUT', self._DEFAULT_NID_AUT),
            "NID_SES": params.get('NID_SES', self._DEFAULT_NID_SES)
        }

        for attempt_number in attempts(self.max_retries):
            try:
                resp = self._session_get_json(self._VOD_DETAIL_URL.format(vod_id=vod_id),
                                              cookies=self.cookies)
                is_available = resp.get('code') == 200
                video = resp.get('content', {})
                break
            except RequestException as e:
                self.retry(attempt_number, error=e, **params)

        if not is_available:
            raise VideoUnavailable('Not available Chzzk vod...')

        title = video.get('videoTitle')
        duration = video.get('duration')

        return Chat(
            self._get_chat_messages_by_vod_id(vod_id, params, duration),
            title=title,
            duration=duration,
            status='past',
            video_type='video',
            id=vod_id
        )

    def _get_chat_messages_by_vod_id(self, vod_id, params, max_duration):
        next_player_message_time = 0
        while next_player_message_time is not None:
            for attempt_number in attempts(self.max_retries):
                try:
                    resp = self._session_get_json(self._VOD_CHAT_URL.format(vod_id=vod_id, player_message_time=next_player_message_time),
                                                  cookies=self.cookies)
                    is_valid_resp = resp.get('code') == 200
                    if not is_valid_resp:
                        raise UnexpectedError(f'Chzzk VOD chat retrieve failed: {resp}')

                    content = resp.get('content', {})
                    next_player_message_time = content.get('nextPlayerMessageTime')
                    chats = content.get('videoChats', [])
                    for chat in chats:
                        data = self._parse_chat(chat)
                        yield data

                    break
                except RequestException as e:
                    self.retry(attempt_number, error=e, **params)
        return

    def connect_websocket(self):
        if self.websocket_thread and self.websocket_thread.is_alive():
            self.websocket.close()

        is_live, live_id, live_title, self.chat_channel_id = self.get_channel_detail()
        if is_live:
            self.server_id = sum([ord(c) for c in self.chat_channel_id]) % 9 + 1
            self.access_token = self.get_chat_access_token()
            self.websocket = WebSocketApp(
                url=f'wss://kr-ss{self.server_id}.chat.naver.com/chat',
                on_open=self.on_open,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close,
                on_reconnect=self.on_open,
            )
            self.websocket_thread = Thread(
                target=self.websocket.run_forever,
                kwargs=dict(
                    ping_interval=20,
                    ping_payload=orjson.dumps({'ver': '3', 'cmd': ChatCommands.PING}),
                    # http_proxy_host=self.proxy_host,
                    # http_proxy_port=self.proxy_port
                ),
                daemon=True
            )
            self.websocket_thread.start()
        return is_live, live_id, live_title

    def _get_chat_messages_by_channel_id(self):
        message_count = 0
        try:
            while True:
                try:
                    data = self.queue.get(timeout=self.timeout)
                except queue.Empty:
                    yield {}
                    continue

                message_count += 1
                yield data
                log('debug', f'Total number of messages: {message_count}')
        finally:
            self.terminate()

    def _get_chat_by_channel_id(self, match, params):
        return self.get_chat_by_channel_id(match.group('channel_id'), params)

    def get_channel_detail(self):
        for i in range(self.max_retries):
            try:
                live_info = self._session_get_json(self._LIVE_DETAIL_URL.format(channel_id=self.live_channel_id))['content']
                return live_info['status'] == "OPEN", live_info['liveId'], live_info['liveTitle'], live_info['chatChannelId']
            except (JSONDecodeError, RequestException):
                continue
        raise UserNotFound(f'Unable to find Chzzk channel: "{self.live_channel_id}"')

    def get_chat_access_token(self):
        for i in range(self.max_retries):
            try:
                return self._session_get_json(
                    self._ACCESS_TOKEN_URL.format(chat_channel_id=self.chat_channel_id),
                    cookies=self.cookies
                )['content']['accessToken']
            except (JSONDecodeError, RequestException):
                continue
        raise SiteError(f'Unable to get access token of Chzzk: "{self.chat_channel_id}"')

    def terminate(self):
        log('info', '###### Terminate ChzzkChatDownloader #######')
        self.terminated = True
        if self.websocket:
            self.websocket.close()
        if self.websocket_thread:
            self.websocket_thread.join(timeout=self.timeout)
            if self.websocket_thread.is_alive():
                log('error', 'Websocket thread not closed!')

    def get_chat_by_channel_id(self, channel_id, params):
        # First function to be called

        self.live_channel_id = channel_id
        self.timeout = params.get('message_receive_timeout')
        self.cookies = {
            "NID_AUT": params.get('NID_AUT', self._DEFAULT_NID_AUT),
            "NID_SES": params.get('NID_SES', self._DEFAULT_NID_SES)
        }
        if self.session.proxies:
            proxy_full = self.session.proxies.get('https')
            if proxy_full:
                proxy_splitted = proxy_full.split(':')
                self.proxy_host = proxy_splitted[0]
                self.proxy_port = proxy_splitted[1] if len(proxy_splitted) > 1 else None

        log('info', f'params: {params}')

        # connect websocket before
        is_live, live_id, live_title = self.connect_websocket()

        return Chat(
            self._get_chat_messages_by_channel_id(),
            title=live_title,
            duration=None,
            status='live' if is_live else 'upcoming',  # Always live or upcoming
            video_type='video',
            id=f"{live_id}:{self.chat_channel_id}"
        )
