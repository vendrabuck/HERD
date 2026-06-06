import logging

from app.services import notification_service
from app.services.dispatchers.base import DispatchMessage

logger = logging.getLogger(__name__)


class InAppDispatcher:
    channel = "in_app"

    async def send(self, session_factory, message: DispatchMessage) -> None:
        async with session_factory() as session:
            notification = await notification_service.create(
                session,
                user_id=message.user_id,
                event_type=message.event_type,
                title=message.title,
                body=message.body,
                data=message.data,
                dedupe_key=message.dedupe_key,
            )
        if notification is None:
            # Duplicate of an already-delivered event (NATS redelivery). No-op.
            logger.info(
                "Suppressed duplicate in-app notification",
                extra={
                    "action": "notification_deduped",
                    "channel": self.channel,
                    "user_id": str(message.user_id),
                    "event_type": message.event_type,
                    "dedupe_key": message.dedupe_key,
                },
            )
            return
        logger.info(
            "Delivered in-app notification",
            extra={
                "action": "notification_delivered",
                "channel": self.channel,
                "notification_id": str(notification.id),
                "user_id": str(message.user_id),
                "event_type": message.event_type,
            },
        )
