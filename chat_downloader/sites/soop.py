import asyncio
import queue
from datetime import datetime, timezone
from threading import Event, Thread

from ..debugging import (
    log,
)
from .afreeca import AfreecaTV, Donation, GuestCredential, UserCredential
from .afreeca import Chat as AfreecaChat
from .afreeca.exceptions import NotStreamingError
from .common import (
    BaseChatDownloader,
    Chat,
)


class SoopChatDownloader(BaseChatDownloader):
    _NAME = "sooplive.com"

    _SITE_DEFAULT_PARAMS = {
        "format": "default",
    }

    _VALID_URLS = {
        # e.g. 'https://play.sooplive.co.kr/username'
        # e.g. 'https://play.sooplive.co.kr/username/bno/'
        "_get_chat": r"https?://play\.(?:sooplive\.co\.kr|afreecatv\.com)/(?P<username>\w+)(?:/(?P<bno>:\d+))?",
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.queue = queue.Queue()
        self._stop_event = Event()

    async def _chat_callback(self, chat: AfreecaChat):
        timestamp = int(datetime.now(timezone.utc).timestamp() * 1e6)
        data = {
            "message_id": f"{chat.sender_id}-{timestamp}",
            "timestamp": timestamp,
            "message": chat.message,
            "flag": ",".join(chat.flags),
            "author": {
                "id": chat.sender_id,
                "display_name": chat.nickname,
            },
        }
        if chat.subscription_month:
            data["author"]["subscription_month"] = chat.subscription_month

        self.queue.put(data)

    async def _donation_callback(self, donation: Donation):
        timestamp = int(datetime.now(timezone.utc).timestamp() * 1e6)
        data = {
            "message_id": f"{donation.sender_id}-{timestamp}",
            "timestamp": timestamp,
            "message": "",  # 숲은 다음 일반 채팅 메시지 이벤트가 tts로 읽힘.
            "flag": "",
            "payAmount": donation.amount,
            "donationType": donation.type.value,
            "author": {
                "id": donation.sender_id,
                "display_name": donation.nickname,
            },
        }

        self.queue.put(data)

    def afreeca_chat_recv_loop(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._recv_loop_with_stop())
        except Exception as e:
            if not self._stop_event.is_set():
                log("error", f"Error in recv loop: {e}")
        finally:
            log("debug", "Async event loop finished.")

    async def _recv_loop_with_stop(self):
        """Wrapper around chat_loader.loop() that checks the stop event."""
        while not self._stop_event.is_set():
            if self.chat_loader.connection is None:
                break

            if self.chat_loader.connection.closed:
                if self._stop_event.is_set():
                    break
                try:
                    await self.chat_loader.connect()
                except Exception:
                    if self._stop_event.is_set():
                        break
                    raise
                continue

            try:
                msg = await asyncio.wait_for(
                    self.chat_loader.connection.receive(),
                    timeout=2.0,
                )

                from aiohttp import WSMsgType

                if msg.type == WSMsgType.CLOSED:
                    if self._stop_event.is_set():
                        break
                    await self.chat_loader.connect()
                    continue

                await self.chat_loader._process_message(msg)
            except asyncio.TimeoutError:
                continue
            except Exception:
                if self._stop_event.is_set():
                    break
                raise

    def _get_chat_messages(self, params):
        recv_thread = Thread(
            target=self.afreeca_chat_recv_loop,
            name="afreeca_chat_recv_loop",
            daemon=True,
        )
        try:
            recv_thread.start()
            message_count = 0
            while True:
                try:
                    data = self.queue.get(
                        timeout=params.get("message_receive_timeout", 1)
                    )
                except queue.Empty:
                    yield {}
                    continue

                message_count += 1
                yield data
                log("debug", f"Total number of messages: {message_count}")
        except Exception as e:
            log("error", e)
        finally:
            log("debug", "Cleanup afreeca chat downloader")
            self._stop_event.set()

            # Schedule cleanup on the event loop in a thread-safe manner
            try:
                future = asyncio.run_coroutine_threadsafe(self.cleanup(), self.loop)
                # Wait for cleanup to finish with a timeout
                future.result(timeout=10)
            except Exception as e:
                log("debug", f"Cleanup future exception: {e}")

            # Wait for the recv thread to finish
            recv_thread.join(timeout=5)

            # Now shut down the event loop cleanly
            try:
                self.loop.call_soon_threadsafe(self.loop.stop)
                # Give it a moment to stop
                recv_thread.join(timeout=2)
            except Exception:
                pass

            log("debug", "Session closed.")

    def _get_chat(self, match, params):
        self.loop = asyncio.new_event_loop()
        chat = self.loop.run_until_complete(
            self.get_chat_by_username(match.group("username"), params)
        )
        return chat

    async def cleanup(self):
        log("info", "Close all SoopChatDownloader connections")
        await self.chat_loader.close()

    async def get_chat_by_username(self, username, params):
        id = params.get("SOOP_ID")
        pw = params.get("SOOP_PW")
        if id and pw:
            cred = await UserCredential.login(id, pw)
        else:
            cred = GuestCredential()

        afreeca = AfreecaTV(credential=cred)
        self.chat_loader = await afreeca.create_chat(username)
        self.chat_loader.add_callback(event="chat", callback=self._chat_callback)
        self.chat_loader.add_callback(
            event="donation", callback=self._donation_callback
        )

        try:
            await self.chat_loader.connect()
            bj_info = self.chat_loader.info
            return Chat(
                self._get_chat_messages(params),
                title=bj_info.title,
                duration=None,
                status="live",
                video_type="video",
                id=bj_info.bno,
            )

        except NotStreamingError:
            await self.cleanup()
            raise
