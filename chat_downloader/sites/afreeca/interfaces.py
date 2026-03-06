from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from .types.packet import ChatPacket
from .utils import get_color, get_flags


@dataclass
class BroadcastInfo:
    bno: int
    title: str
    viewer: int
    is_password: bool
    is_subscription: bool


@dataclass
class BJInfo:
    bj_id: str
    bj_nick: str

    bno: str
    title: str

    chat_url: str
    chatno: str
    ftk: str
    tk: Optional[str]

    pcon: dict[int, str]
    bpwd: bool


@dataclass
class Chat:
    sender_id: str
    nickname: str
    message: str

    chat_lang: int
    subscription_month: Optional[int]
    flags: list[str]

    def __init__(self, packet: list[str]) -> None:
        data = self._parse_packet(packet)["data"]

        self.sender_id = data["senderID"]
        self.nickname = data["nickname"]
        self.message = data["message"]
        self.chat_lang = data["chatLang"]
        self.subscription_month = data["subscription_month"]
        self.flags = data["flags"]

    def _parse_packet(self, packet: list[str]) -> ChatPacket:
        message = packet[0].replace("\r", "")
        senderID = packet[1]
        permission = int(packet[3])
        chatLang = int(packet[4])
        nickname = packet[5]
        flag = packet[6]
        subscription_month = None if packet[7] == "-1" else int(packet[7])

        color = None

        if len(packet) > 8:
            color = get_color(packet[8])

        if permission == 3 or permission == 0:
            flag1, flag2 = flag.split("|")

            return {
                "cmd": "msg",
                "data": {
                    "senderID": senderID,
                    "nickname": nickname,
                    "message": message,
                    "chatLang": chatLang,
                    "color": color,
                    "subscription_month": subscription_month,
                    "flags": get_flags(flag1, flag2),
                },
            }

        return {"cmd": "unknown", "data": {}}


class DonationType(str, Enum):
    BALLOON = "balloon"
    AD_BALLOON = "ad_balloon"
    VIDEO_BALLOON = "video_balloon"


@dataclass
class Donation:
    """후원 이벤트 (별풍선 / 애드벌룬 / 영상풍선)"""

    type: DonationType
    sender_id: str
    nickname: str
    amount: int

    @classmethod
    def balloon(cls, packet: list[str]) -> Donation:
        """별풍선 (SVC_SENDBALLOON / SVC_SENDBALLOONSUB)"""
        return cls(
            type=DonationType.BALLOON,
            sender_id=packet[1],
            nickname=packet[2],
            amount=int(packet[3]),
        )

    @classmethod
    def ad_balloon(cls, packet: list[str]) -> Donation:
        """애드벌룬 (SVC_ADCON_EFFECT)"""
        return cls(
            type=DonationType.AD_BALLOON,
            sender_id=packet[2],
            nickname=packet[3],
            amount=int(packet[9]),
        )

    @classmethod
    def video_balloon(cls, packet: list[str]) -> Donation:
        """영상풍선 (SVC_VIDEOBALLOON)"""
        return cls(
            type=DonationType.VIDEO_BALLOON,
            sender_id=packet[2],
            nickname=packet[3],
            amount=int(packet[4]),
        )


@dataclass
class OGQEmoticon:
    """OGQ 이모티콘 이벤트 (SVC_OGQ_EMOTICON)"""

    sticker_dir: str
    image_name_prefix: str
    sender_id: str
    nickname: str
    subscription_month: Optional[int]
    flags: list[str]
    image_url: str

    def __init__(self, packet: list[str]) -> None:
        self.sticker_dir = packet[2]
        self.image_name_prefix = packet[3]
        self.sender_id = packet[5]
        self.nickname = packet[6]
        flag = packet[7]
        self.subscription_month = None if packet[8] == "-1" else int(packet[8])

        flag1, flag2 = flag.split("|")
        self.flags = get_flags(flag1, flag2)

        self.image_url = (
            f"https://ogq-sticker-global-cdn-z01.sooplive.co.kr/sticker/"
            f"{self.sticker_dir}/{self.image_name_prefix}_80.png?ver=1"
        )


@dataclass
class Mission:
    """미션 이벤트 (SVC_MISSION) - 대결 미션 등"""

    type: str
    data: dict[str, Any]
    settle_count: Optional[int]

    def __init__(self, packet: list[str]) -> None:
        self.data = json.loads(packet[0])
        self.type = self.data["type"]
        self.settle_count = self.data.get("settle_count")


@dataclass
class MissionSettle:
    """미션 정산 이벤트 (SVC_MISSION_SETTLE) - 도전 미션 정산"""

    data: dict[str, Any]
    total_amount: int
    items: list[list[Any]]

    def __init__(self, packet: list[str]) -> None:
        self.data = json.loads(packet[0])
        self.items = self.data.get("list", [])
        self.total_amount = sum(item[2] for item in self.items)
