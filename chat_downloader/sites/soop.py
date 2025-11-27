from .common import (
    Chat,
    BaseChatDownloader,
)

from ..debugging import (
    log,
)
import asyncio
import queue

from .afreeca import AfreecaTV, Chat as AfreecaChat, UserCredential
from .afreeca.exceptions import NotStreamingError
from datetime import datetime, timezone
from threading import Thread


class SoopChatDownloader(BaseChatDownloader):
    _NAME = 'sooplive.com'

    _SITE_DEFAULT_PARAMS = {
        'format': 'default',
    }

    _VALID_URLS = {
        # e.g. 'https://play.sooplive.co.kr/username'
        # e.g. 'https://play.sooplive.co.kr/username/bno/'
        '_get_chat': r"https?://play\.(?:sooplive\.co\.kr|afreecatv\.com)/(?P<username>\w+)(?:/(?P<bno>:\d+))?",
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.queue = queue.Queue()

    async def _chat_callback(self, chat: AfreecaChat):
        timestamp = int(datetime.now(timezone.utc).timestamp() * 1e6)
        data = {
            'message_id': f'{chat.sender_id}-{timestamp}',
            'timestamp': timestamp,
            'message': chat.message,
            'flag': ','.join(chat.flags),
            'author': {
                'id': chat.sender_id,
                'display_name': chat.nickname,
            }
        }
        if chat.subscription_month:
            data['author']['subscription_month'] = chat.subscription_month

        self.queue.put(data)

    def afreeca_chat_recv_loop(self):
        self.loop.run_until_complete(self.chat_loader.loop())
        self.loop.close()
        log('debug', 'Async event loop closed...')

    def _get_chat_messages(self, params):
        try:
            Thread(target=self.afreeca_chat_recv_loop).start()
            message_count = 0
            while True:
                try:
                    data = self.queue.get(timeout=params.get('message_receive_timeout'))
                except queue.Empty:
                    yield {}
                    continue

                message_count += 1
                yield data
                log('debug', f'Total number of messages: {message_count}')
        except Exception as e:
            log('error', e)
        finally:
            log('debug', 'Cleanup afreeca chat downloader')
            self.chat_loader.remove_callback(self._chat_callback)
            self.loop.create_task(self.close_all_aiohttp_connections())

    def _get_empty_generator(self):
        yield {}
        return

    def _get_chat(self, match, params):
        self.loop = asyncio.new_event_loop()
        chat = self.loop.run_until_complete(self.get_chat_by_username(match.group('username'), params))
        return chat

    async def close_all_aiohttp_connections(self):
        log('info', 'Close all SoopChatDownloader connections')
        if self.chat_loader.credential._session:
            await self.chat_loader.credential._session.close()
        if self.chat_loader.session:
            await self.chat_loader.session.close()
        if self.chat_loader.keepalive_task:
            self.chat_loader.keepalive_task.cancel()
        if self.chat_loader.connection:
            client_websocket_response = self.chat_loader.connection
            self.chat_loader.connection = None
            await client_websocket_response.close()

    async def get_chat_by_username(self, username, params):
        id = params.get('afreeca-id', "")
        pw = params.get('afreeca-pw', "")

        assert id != "" and pw != "", "AfreecaTV login credentials are required (afreeca-id and afreeca-pw)"

        cred = await UserCredential.login(id, pw)
        afreeca = AfreecaTV(credential=cred)

        self.chat_loader = await afreeca.create_chat(username)
        self.chat_loader.add_callback(event='chat', callback=self._chat_callback)

        try:
            await self.chat_loader.connect()
            bj_info = self.chat_loader.info
            return Chat(
                self._get_chat_messages(params),
                title=bj_info.title,
                duration=None,
                status='live',
                video_type='video',
                id=bj_info.bno
            )

        except NotStreamingError:
            await self.close_all_aiohttp_connections()
            return Chat(
                self._get_empty_generator(),
                title='',
                duration=None,
                status='upcoming',
                video_type='video',
                id=''
            )
